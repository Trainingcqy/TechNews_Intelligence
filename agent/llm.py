from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import cache
from typing import Any, Callable

from langchain_core.messages import AIMessage

from agent.trace import set_model_io, trace_span, usage_from_message
from common.llm_provider import (
    DEFAULT_DEEPSEEK_FLASH_MODEL,
    DEFAULT_VERTEX_MODEL,
    build_chat_model,
    normalize_provider,
    resolve_model_config,
)


class ModelCallError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ModelHandle:
    role: str  # gather | final
    provider: str
    model: str
    client: Any
    temperature: float | None = None


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _temperature() -> float:
    raw = _clean(os.getenv("AGENT_TEMPERATURE", "0.1"))
    try:
        return float(raw)
    except Exception:
        return 0.1


@cache
def build_models() -> tuple[ModelHandle, ModelHandle]:
    return (
        _build_role_model(role="gather", default_provider="deepseek", default_model=DEFAULT_DEEPSEEK_FLASH_MODEL),
        _build_role_model(role="final", default_provider="vertex", default_model=DEFAULT_VERTEX_MODEL),
    )


def _build_role_model(*, role: str, default_provider: str, default_model: str) -> ModelHandle:
    prefix = f"AGENT_{role.upper()}"
    try:
        provider = normalize_provider(_clean(os.getenv(f"{prefix}_PROVIDER", "")) or default_provider)
        # A role's default model belongs to its default provider; any other provider gets its own default model.
        model_name = _clean(os.getenv(f"{prefix}_MODEL", "")) or (default_model if provider == default_provider else None)
        config = resolve_model_config(provider=provider, model_name=model_name)
        temperature = _temperature()
        client = build_chat_model(provider=config.provider, model_name=config.model, temperature=temperature)
    except Exception as exc:
        raise ModelCallError(f"model_build_{type(exc).__name__.lower()}", f"{role}: {exc}") from exc
    return ModelHandle(role=role, provider=config.provider, model=config.model, client=client,
                       temperature=temperature)


def call_with_tools(handle: ModelHandle, messages: list, tools: list) -> AIMessage:
    return _invoke(handle, messages, lambda: handle.client.bind_tools(tools).invoke(messages))


def call_text(handle: ModelHandle, messages: list) -> str:
    result = _invoke(handle, messages, lambda: handle.client.invoke(messages))
    return coerce_to_text(getattr(result, "content", result)).strip()


def _invoke(handle: ModelHandle, messages: list, invoke: Callable[[], Any]) -> Any:
    failure: Exception | None = None
    attributes = {"provider": handle.provider, "model": handle.model, "temperature": handle.temperature}
    with trace_span("llm", handle.role, attributes=attributes) as span:
        try:
            result = invoke()
        except Exception as exc:  # noqa: BLE001
            failure = exc
            span.fail(exc, code=_error_code(exc))
            set_model_io(span, messages, {"error": str(exc), "exception_type": type(exc).__name__})
        else:
            metadata = getattr(result, "response_metadata", None) or {}
            usage, consistent = usage_from_message(result)
            reported = {"response_model": metadata.get("model_name"), "finish_reason": metadata.get("finish_reason"),
                        "usage": usage}
            span.attributes.update({key: value for key, value in reported.items() if value is not None})
            if not consistent:
                span.warn("usage_inconsistent")
            text = coerce_to_text(getattr(result, "content", result)).strip()
            span.output = {"chars": len(text), "tool_calls": len(getattr(result, "tool_calls", None) or [])}
            set_model_io(span, messages, result)
    # Raised outside the span so trace_span does not overwrite the error code recorded above.
    if failure is not None:
        raise ModelCallError(_error_code(failure), str(failure)) from failure
    return result


def _error_code(exc: Exception) -> str:
    return f"model_{type(exc).__name__.lower()}"


def coerce_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and part.get("text"):
                chunks.append(str(part.get("text")))
        return "\n".join(chunks)
    return "" if content is None else str(content)


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    candidates = [raw]
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return None
