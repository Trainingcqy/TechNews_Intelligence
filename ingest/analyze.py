"""analyze stage: English summary, topics and kind from one model call per article."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import openai
from langchain_openai import ChatOpenAI

from ingest import store
from ingest.config import IngestConfig
from ingest.models import Analysis
from common import model_settings, prompt_text
from common.llm_provider import build_chat_model
from common.taxonomy import KIND_SCOPES, TOPIC_SCOPES, validate_kind, validate_topics

logger = logging.getLogger(__name__)

_DETAIL_OUTPUT_CHARS = 2000
_SUMMARY_ENDINGS = (".", "?", "!")


class InvalidJson(ValueError):
    pass


class SchemaViolation(ValueError):
    pass


class AnalyzeFailure(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _table(scopes: dict[str, str]) -> str:
    return "\n".join(f"- {key}: {scope}" for key, scope in scopes.items())


def build_messages(title: str, publisher: str, body: str, body_level: str, *, body_chars: int) -> list[dict]:
    user = prompt_text.render(
        "analyze.user",
        body_note="" if body_level == "full" else prompt_text.text("analyze.no_body_note"),
        topic_table=_table(TOPIC_SCOPES),
        kind_table=_table(KIND_SCOPES),
        title=title,
        publisher=publisher,
        body=body[:body_chars],
    )
    return [{"role": "system", "content": prompt_text.text("analyze.system")}, {"role": "user", "content": user}]


def parse_analysis(raw: str) -> Analysis:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidJson(f"the output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidJson("the output is not a JSON object")
    try:
        analysis = Analysis.model_validate(data)
        validate_topics(analysis.topics)
        validate_kind(analysis.kind)
    except ValueError as exc:
        raise SchemaViolation(str(exc)) from exc
    if not analysis.summary.endswith(_SUMMARY_ENDINGS):
        raise SchemaViolation("summary does not end with a period, a question mark or an exclamation mark")
    return analysis


def _with_cause(exc: BaseException) -> str:
    cause = exc.__cause__
    # langchain-openai wraps the SDK error, which wraps the transport error; name the innermost one.
    while cause is not None and cause.__cause__ is not None:
        cause = cause.__cause__
    return f"{exc} ({type(cause).__name__}: {cause})" if cause else str(exc)


def _invoke(model: Any, messages: list[dict], spec: model_settings.AnalyzeModel) -> str:
    kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}} if spec.json_mode else {}
    if isinstance(model, ChatOpenAI):
        # langchain-openai renames a max_tokens argument to max_completion_tokens; extra_body sends max_tokens as is.
        kwargs["extra_body"] = {"max_tokens": spec.max_tokens}
    try:
        return model.invoke(messages, **kwargs).text
    except openai.LengthFinishReasonError as exc:
        # With response_format the client raises on a truncated output instead of returning it; return the
        # partial text so that both modes go through the same parsing.
        return exc.completion.choices[0].message.content or ""
    # APITimeoutError is a subclass of APIConnectionError and must be caught first.
    except openai.APITimeoutError as exc:
        raise AnalyzeFailure("timeout", _with_cause(exc)) from exc
    except openai.APIConnectionError as exc:
        raise AnalyzeFailure("network", _with_cause(exc)) from exc
    except openai.APIStatusError as exc:
        raise AnalyzeFailure("llm_http", str(exc)) from exc


def analyze(model: Any, item: store.AnalyzeItem, config: IngestConfig) -> Analysis:
    """One attempt: a call, and one more with the validation error appended when the output fails validation."""
    spec = model_settings.get("analyze")
    system, user = build_messages(
        item.title, item.publisher, item.body, item.body_level, body_chars=config.analyze_body_chars
    )
    try:
        return parse_analysis(_invoke(model, [system, user], spec))
    except (InvalidJson, SchemaViolation) as exc:
        retry = prompt_text.render("analyze.retry_note", error=str(exc))
        retry_user = {"role": "user", "content": user["content"] + "\n\n" + retry}
    raw = _invoke(model, [system, retry_user], spec)
    try:
        return parse_analysis(raw)
    except InvalidJson as exc:
        raise AnalyzeFailure("invalid_json", raw[:_DETAIL_OUTPUT_CHARS].replace("\x00", "")) from exc
    except SchemaViolation as exc:
        raise AnalyzeFailure("schema_violation", raw[:_DETAIL_OUTPUT_CHARS].replace("\x00", "")) from exc


def analyze_article(item: store.AnalyzeItem, *, model: Any, transaction: store.Transaction, config: IngestConfig) -> None:
    try:
        try:
            analysis = analyze(model, item, config)
        except AnalyzeFailure as exc:
            with transaction() as cur:
                store.record_failure(cur, item.article_id, "analyze", exc.kind, exc.detail, config)
            return
        with transaction() as cur:
            store.record_analyze_success(cur, item.article_id, analysis)
    except Exception as exc:
        logger.exception("analyze %s failed", item.article_id)
        with transaction() as cur:
            store.record_failure(cur, item.article_id, "analyze", "unexpected", f"{type(exc).__name__}: {exc}", config)


def run_analyze(config: IngestConfig, *, transaction: store.Transaction) -> None:
    spec = model_settings.get("analyze")
    model = build_chat_model(provider=spec.provider, model_name=spec.model, temperature=spec.temperature)
    with transaction() as cur:
        items = store.take_analyze(cur, limit=config.analyze_limit)
    with ThreadPoolExecutor(config.analyze_workers) as pool:
        futures = {
            pool.submit(analyze_article, item, model=model, transaction=transaction, config=config): item
            for item in items
        }
    for future, item in futures.items():
        if future.exception() is not None:
            logger.error("analyze %s: recording the failure failed", item.article_id, exc_info=future.exception())
