"""Tests for the analyze stage."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI

from ingest.analyze import (
    NO_BODY_NOTE,
    AnalyzeFailure,
    InvalidJson,
    SchemaViolation,
    analyze,
    build_messages,
    parse_analysis,
)
from ingest.config import IngestConfig
from ingest.store import AnalyzeItem

_VALID = {
    "title_zh": "Lovable 2 月 ARR 达 4 亿美元",
    "summary": "瑞典 AI 开发平台 Lovable 2 月 ARR 达 4 亿美元。",
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


@pytest.mark.parametrize("raw", ["以下是结果：{}", '```json\n{"kind": "news"}\n```', "[]"])
def test_parse_analysis_rejects_non_object(raw):
    with pytest.raises(InvalidJson):
        parse_analysis(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"topics": ["ai", "crypto"]},
        {"kind": "announcement"},
        {"summary": "长" * 220 + "。"},
        {"summary": "瑞典 AI 开发平台 Lovable 2 月 ARR 达 4 亿美元"},
        {"summary": "瑞典 AI 开发平台 Lovable 2 月 ARR 达 4 亿美元."},
    ],
)
def test_parse_analysis_rejects_schema_violations(changes):
    with pytest.raises(SchemaViolation):
        parse_analysis(_raw(**changes))


def test_build_messages_truncates_body():
    system, user = build_messages("Title", "TechCrunch", "a" * 10 + "b" * 5, "full", body_chars=10)
    assert system["role"] == "system" and user["role"] == "user"
    assert user["content"].endswith("标题：Title\n来源：TechCrunch\n正文：\n" + "a" * 10 + "\n</article>")


@pytest.mark.parametrize(("body_level", "noted"), [("full", False), ("excerpt", True), ("none", True)])
def test_build_messages_body_note(body_level, noted):
    _, user = build_messages("t", "p", "body", body_level, body_chars=100)
    expected = f"中文引号。\n{NO_BODY_NOTE}\n\n三、topics" if noted else "中文引号。\n\n\n三、topics"
    assert expected in user["content"]


def test_analyze_sends_max_tokens_as_is():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        message = {"role": "assistant", "content": _raw()}
        choice = {"index": 0, "finish_reason": "stop", "message": message}
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [choice]})

    analyze(_chat_model(handler), _ITEM, IngestConfig())
    assert bodies[0]["max_tokens"] == 10000 and "max_completion_tokens" not in bodies[0]


def test_analyze_sends_no_extra_body_to_other_chat_models():
    class Model:
        def invoke(self, messages, response_format=None):
            return SimpleNamespace(text=_raw())

    assert analyze(Model(), _ITEM, IngestConfig()).model_dump() == _VALID


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
