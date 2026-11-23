"""Request trace of one run_turn: spans, warnings, token usage and full model I/O.

Spans follow the turn's semantic units: context, step, model call, tool call and the retrieval stages.
The code that produces a span fills its input and output; this module knows nothing about evidence or tools.
"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SPAN_KINDS = frozenset({"context", "step", "llm", "tool", "embedding", "retrieval", "rerank"})
USAGE_KEYS = ("input_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
_MAX_MESSAGE_CHARS = 400
_MAX_TRACEBACK_CHARS = 8000
_MESSAGE_ATTRS = ("name", "id", "tool_call_id", "additional_kwargs", "response_metadata", "usage_metadata",
                  "tool_calls", "invalid_tool_calls")
_PROVIDER_INTERNAL_KEYS = {"thought_signature"}
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(
        r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|smtp[_-]?password|password))"
        r"\s*[:=]\s*['\"]?([^'\"\s,;]{8,})"
    ), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"<>]+"), "[REDACTED_DB_URL]"),
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def format_exception(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return text[-_MAX_TRACEBACK_CHARS:]


@dataclass
class Span:
    kind: str
    name: str | None = None
    parent_id: str | None = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at_ms: int = field(default_factory=_now_ms)
    finished_at_ms: int | None = None
    status: str = "ok"  # ok | error | aborted
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    exception: str | None = None

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def fail(self, exc: BaseException | None = None, *, code: str | None = None, message: str | None = None) -> None:
        self.status = "error"
        self.error_code = code or f"{self.kind}_{type(exc).__name__.lower() if exc else 'failed'}"
        text = message if message is not None else str(exc or "")
        self.error_message = text[:_MAX_MESSAGE_CHARS] or None
        if exc is not None:
            self.exception = format_exception(exc)

    def finish(self) -> None:
        if self.finished_at_ms is None:
            self.finished_at_ms = _now_ms()


@dataclass
class RequestTrace:
    request_id: str
    question: str
    channel: str
    thread_id: str | None = None
    started_at_ms: int = field(default_factory=_now_ms)
    finished_at_ms: int | None = None
    status: str = "running"
    error_code: str | None = None
    error_message: str | None = None
    exception: str | None = None
    token_usage: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)  # 记在请求上的警告；all_warnings() 另含各 span 的
    output: dict[str, Any] = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)
    model_io: dict[str, dict[str, Any]] = field(default_factory=dict)  # 键为 span_id

    @property
    def latency_ms(self) -> int | None:
        return None if self.finished_at_ms is None else self.finished_at_ms - self.started_at_ms

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def fail(self, exc: BaseException) -> None:
        self.error_message = str(exc)[:_MAX_MESSAGE_CHARS] or None
        self.exception = format_exception(exc)

    def all_warnings(self) -> list[str]:
        codes = list(self.warnings)
        for span in self.spans:
            codes.extend(code for code in span.warnings if code not in codes)
        return codes

    def finish(self, *, status: str, error_code: str | None = None, error_message: str | None = None) -> None:
        if self.finished_at_ms is not None:
            return
        self.finished_at_ms = _now_ms()
        self.status = status
        self.error_code = error_code
        if error_message is not None and self.error_message is None:
            self.error_message = error_message[:_MAX_MESSAGE_CHARS] or None
        for span in self.spans:
            if span.finished_at_ms is None:
                span.finished_at_ms = self.finished_at_ms
                span.status = "aborted"
        self.token_usage = _sum_usage(self.spans)
        _persist(self)


_REQUEST: ContextVar[RequestTrace | None] = ContextVar("agent_request_trace", default=None)
_STACK: ContextVar[tuple[Span, ...]] = ContextVar("agent_trace_stack", default=())


@contextmanager
def request_trace(*, question: str, channel: str, thread_id: str | None = None,
                  request_id: str | None = None) -> Iterator[RequestTrace]:
    """Bind a request trace to the current context; its row is inserted as running right away."""
    trace = RequestTrace(request_id=request_id or uuid.uuid4().hex, question=question, channel=channel,
                         thread_id=thread_id or None)
    _insert_running(trace)
    request_token = _REQUEST.set(trace)
    stack_token = _STACK.set(())
    try:
        yield trace
    finally:
        _STACK.reset(stack_token)
        _REQUEST.reset(request_token)


def start_span(kind: str, name: str | None = None, *, input: dict | None = None,
               attributes: dict | None = None) -> Span:
    """Open a span under the current one. Outside a request trace the span is not recorded."""
    if kind not in SPAN_KINDS:
        raise ValueError(f"unknown span kind: {kind}")
    stack = _STACK.get()
    span = Span(kind=kind, name=name, parent_id=stack[-1].span_id if stack else None,
                input=dict(input or {}), attributes=dict(attributes or {}))
    request = _REQUEST.get()
    if request is not None:
        request.spans.append(span)
    return span


@contextmanager
def within(span: Span | None) -> Iterator[None]:
    """Make an already open span the current one, e.g. a step that runs across several graph nodes."""
    if span is None or _REQUEST.get() is None:
        yield
        return
    token = _STACK.set((*_STACK.get(), span))
    try:
        yield
    finally:
        _STACK.reset(token)


@contextmanager
def trace_span(kind: str, name: str | None = None, *, input: dict | None = None,
               attributes: dict | None = None) -> Iterator[Span]:
    span = start_span(kind, name, input=input, attributes=attributes)
    with within(span):
        try:
            yield span
        except Exception as exc:
            span.fail(exc)
            raise
        finally:
            span.finish()


def warn(code: str) -> None:
    """Record a warning on the current span, or on the request when no span is open."""
    request = _REQUEST.get()
    if request is None:
        return
    stack = _STACK.get()
    (stack[-1] if stack else request).warn(code)


def set_model_io(span: Span, input_messages: Any, output: Any) -> None:
    request = _REQUEST.get()
    if request is None:
        return
    request.model_io[span.span_id] = {
        "input_messages": _redact(_serialize(input_messages)),
        "output": _redact(_serialize(output)),
    }


def usage_from_message(message: Any) -> tuple[dict[str, int] | None, bool]:
    """The five token counts with OTel semantics: input includes cache hits, output includes reasoning.

    Returns the counts and whether total_tokens agrees with them.
    """
    meta = getattr(message, "usage_metadata", None)
    if not isinstance(meta, dict):
        return None, True
    input_tokens = _count(meta.get("input_tokens"))
    output_tokens = _count(meta.get("output_tokens"))
    total_tokens = _count(meta.get("total_tokens"))
    cache_read = _count((meta.get("input_token_details") or {}).get("cache_read"))
    reasoning = _count((meta.get("output_token_details") or {}).get("reasoning"))
    consistent = True
    if reasoning and total_tokens == input_tokens + output_tokens + reasoning:
        # Gemini reports output without reasoning; DeepSeek already includes it.
        output_tokens += reasoning
    elif total_tokens != input_tokens + output_tokens:
        consistent = False
    usage = {"input_tokens": input_tokens, "cache_read_tokens": cache_read, "output_tokens": output_tokens,
             "reasoning_tokens": reasoning, "total_tokens": total_tokens}
    return usage, consistent


def _count(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _sum_usage(spans: list[Span]) -> dict[str, Any] | None:
    calls = [span for span in spans if span.kind == "llm" and span.attributes.get("usage")]
    if not calls:
        return None
    total = dict.fromkeys(USAGE_KEYS, 0)
    by_model: dict[str, dict[str, int]] = {}
    for span in calls:
        bucket = by_model.setdefault(str(span.attributes.get("model") or ""), dict.fromkeys(USAGE_KEYS, 0))
        for key in USAGE_KEYS:
            total[key] += span.attributes["usage"][key]
            bucket[key] += span.attributes["usage"][key]
    return {**total, "by_model": by_model}


def _serialize(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if hasattr(value, "content"):
        message = {"type": str(getattr(value, "type", type(value).__name__)), "class": type(value).__name__,
                   "content": _json_safe(value.content)}
        for attr in _MESSAGE_ATTRS:
            item = getattr(value, attr, None)
            if item not in (None, "", [], {}):
                message[attr] = _json_safe(item)
        return _strip_provider_internal(message)
    return _strip_provider_internal(_json_safe(value))


def _json_safe(value: Any, path: tuple[int, ...] = ()) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if id(value) in path:
        return f"<circular:{type(value).__name__}>"
    path = (*path, id(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item, path) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, path) for item in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _strip_provider_internal(value: Any) -> Any:
    """Replace provider-internal payloads such as Gemini thought signatures with a size note."""
    if isinstance(value, dict):
        return {
            key: (f"[provider_internal_{key}_omitted chars={len(str(item or ''))}]"
                  if key in _PROVIDER_INTERNAL_KEYS else _strip_provider_internal(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_provider_internal(item) for item in value]
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in _SECRET_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _insert_running(trace: RequestTrace) -> None:
    from agent import trace_store

    try:
        trace_store.insert_running(trace)
    except Exception:  # noqa: BLE001
        # Tracing must never fail a turn; the final write inserts the row if this one did not.
        logger.warning("trace insert failed: request_id=%s", trace.request_id, exc_info=True)


def _persist(trace: RequestTrace) -> None:
    from agent import trace_store

    try:
        trace_store.persist(trace)
    except Exception:  # noqa: BLE001
        logger.warning("trace persist failed: request_id=%s", trace.request_id, exc_info=True)
