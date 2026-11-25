"""Tests for the analyze stage."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI

from common import prompt_text
from ingest.analyze import (
    AnalyzeFailure,
    InvalidJson,
    SchemaViolation,
    analyze,
    build_messages,
    parse_analysis,
    run_analyze,
)
from ingest.config import IngestConfig
from ingest.store import AnalyzeItem

_VALID = {
    "summary": "Swedish AI app builder Lovable reached $400 million in annual recurring revenue in February.",
    "topics": ["ai", "business"],
    "kind": "news",
}
_ITEM = AnalyzeItem(article_id=1, title="Title", publisher="TechCrunch", body="body", body_level="full")


def _raw(**changes: object) -> str:
    return json.dumps({**_VALID, **changes}, ensure_ascii=False)


def _chat_model(handler) -> ChatOpenAI:
    return ChatOpenAI(
        model="m",
        api_key="k",
        base_url="https://api.example.com",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_parse_analysis_accepts_valid_output():
    analysis = parse_analysis(_raw())
    assert analysis.model_dump() == _VALID


@pytest.mark.parametrize("raw", ["Here is the result: {}", '```json\n{"kind": "news"}\n```', "[]"])
def test_parse_analysis_rejects_non_object(raw):
    with pytest.raises(InvalidJson):
        parse_analysis(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"topics": ["ai", "crypto"]},
        {"kind": "announcement"},
        {"summary": "word " * 160 + "end."},
        {"summary": "Too short."},
        {"summary": "Swedish AI app builder Lovable reached $400 million in annual recurring revenue"},
        {"summary": "Swedish AI app builder Lovable reached $400 million in annual recurring revenue。"},
    ],
)
def test_parse_analysis_rejects_schema_violations(changes):
    with pytest.raises(SchemaViolation):
        parse_analysis(_raw(**changes))


def test_build_messages_truncates_body():
    system, user = build_messages("Title", "TechCrunch", "a" * 10 + "b" * 5, "full", body_chars=10)
    assert system["role"] == "system" and user["role"] == "user"
    assert user["content"].endswith("Title: Title\nPublisher: TechCrunch\nBody:\n" + "a" * 10 + "\n</article>")


@pytest.mark.parametrize(("body_level", "noted"), [("full", False), ("excerpt", True), ("none", True)])
def test_build_messages_body_note(body_level, noted):
    _, user = build_messages("t", "p", "body", body_level, body_chars=100)
    note = prompt_text.text("analyze.no_body_note")
    expected = f"complete sentences.\n{note}\n\n2. topics" if noted else "complete sentences.\n\n\n2. topics"
    assert expected in user["content"]


@pytest.mark.parametrize("json_mode", [True, False])
def test_analyze_sends_max_tokens_as_is_and_json_mode_from_models_toml(models_file, json_mode):
    models_file(f'[analyze]\nprovider = "deepseek"\nmodel = "m"\nmax_tokens = 123\njson_mode = {str(json_mode).lower()}\n')
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        message = {"role": "assistant", "content": _raw()}
        choice = {"index": 0, "finish_reason": "stop", "message": message}
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [choice]})

    analyze(_chat_model(handler), _ITEM, IngestConfig())
    assert bodies[0]["max_tokens"] == 123 and "max_completion_tokens" not in bodies[0]
    assert ("response_format" in bodies[0]) is json_mode


def test_analyze_sends_no_extra_body_to_other_chat_models():
    class Model:
        def invoke(self, messages, response_format=None):
            return SimpleNamespace(text=_raw())

    assert analyze(Model(), _ITEM, IngestConfig()).model_dump() == _VALID


def test_run_analyze_builds_the_model_of_models_toml(models_file, monkeypatch):
    models_file('[analyze]\nprovider = "vertex"\nmodel = "m"\ntemperature = 0.7\nmax_tokens = 1\njson_mode = false\n')
    calls = []
    monkeypatch.setattr("ingest.analyze.build_chat_model", lambda **kwargs: calls.append(kwargs))

    @contextmanager
    def transaction():
        yield SimpleNamespace(execute=lambda *args: None, fetchall=lambda: [])

    run_analyze(IngestConfig(), transaction=transaction)
    assert calls == [{"provider": "vertex", "model_name": "m", "temperature": 0.7}]


def test_analyze_retries_once_with_the_validation_error():
    sent = []
    replies = iter(["not json", _raw()])

    class Model:
        def invoke(self, messages, response_format=None):
            sent.append(messages[1]["content"])
            return SimpleNamespace(text=next(replies))

    assert analyze(Model(), _ITEM, IngestConfig()).model_dump() == _VALID
    error = "the output is not valid JSON: Expecting value: line 1 column 1 (char 0)"
    assert sent[1] == sent[0] + "\n\n" + prompt_text.render("analyze.retry_note", error=error)


@pytest.mark.parametrize(
    ("error", "kind"),
    [(httpx.ReadTimeout("read timed out"), "timeout"), (httpx.ConnectError("connection refused"), "network")],
)
def test_analyze_transport_failure_detail_names_the_cause(error, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(AnalyzeFailure) as failure:
        analyze(_chat_model(handler), _ITEM, IngestConfig())
    assert failure.value.kind == kind
    assert failure.value.detail.endswith(f"({type(error).__name__}: {error})")
