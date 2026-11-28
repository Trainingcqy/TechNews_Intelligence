"""Tests for the request trace: span tree, warnings, token usage and model I/O."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import trace_store
from agent.trace import request_trace, set_model_io, start_span, trace_span, usage_from_message, warn, within


def _usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {"input_tokens": input_tokens, "cache_read_tokens": 0, "output_tokens": output_tokens,
            "reasoning_tokens": 0, "total_tokens": input_tokens + output_tokens}


@pytest.fixture
def stored(monkeypatch):
    calls: dict[str, list] = {"insert": [], "persist": []}
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: calls["insert"].append(trace.request_id))
    monkeypatch.setattr(trace_store, "persist", lambda trace: calls["persist"].append(trace))
    return calls


def test_usage_follows_otel_semantics() -> None:
    gemini = SimpleNamespace(usage_metadata={"input_tokens": 6758, "output_tokens": 708, "total_tokens": 9178,
                                             "output_token_details": {"reasoning": 1712}})
    assert usage_from_message(gemini) == ({"input_tokens": 6758, "cache_read_tokens": 0, "output_tokens": 2420,
                                           "reasoning_tokens": 1712, "total_tokens": 9178}, True)
    deepseek = SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
                                               "input_token_details": {"cache_read": 64},
                                               "output_token_details": {"reasoning": 12}})
    assert usage_from_message(deepseek) == ({"input_tokens": 100, "cache_read_tokens": 64, "output_tokens": 30,
                                             "reasoning_tokens": 12, "total_tokens": 130}, True)
    assert usage_from_message(SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5,
                                                              "total_tokens": 99}))[1] is False
    assert usage_from_message(SimpleNamespace()) == (None, True)


def test_request_trace_builds_the_tree_usage_and_warnings(stored) -> None:
    with request_trace(question="q", channel="web", thread_id="t1", request_id="r1") as trace:
        with trace_span("context"):
            warn("history_sources_unavailable")
        step = start_span("step", attributes={"step": 1})
        with within(step):
            with trace_span("llm", "gather", attributes={"model": "flash"}) as gather:
                gather.attributes["usage"] = _usage(100, 10)
        with within(step):
            with trace_span("tool", "search_news"):
                warn("low_relevance")
        step.finish()
        with trace_span("llm", "final", attributes={"model": "pro"}) as final:
            final.attributes["usage"] = _usage(50, 5)
        warn("evidence_truncated")
        trace.finish(status="answered")
        trace.finish(status="error")

    assert stored == {"insert": ["r1"], "persist": [trace]}
    assert (trace.status, trace.channel, trace.thread_id) == ("answered", "web", "t1")
    context, step, gather, tool, final = trace.spans
    assert [span.parent_id for span in trace.spans] == [None, None, step.span_id, step.span_id, None]
    assert all(span.status == "ok" for span in trace.spans)
    assert trace.token_usage == {**_usage(150, 15), "by_model": {"flash": _usage(100, 10), "pro": _usage(50, 5)}}
    assert trace.all_warnings() == ["evidence_truncated", "history_sources_unavailable", "low_relevance"]


def test_unfinished_spans_are_aborted_and_errors_keep_the_traceback(stored) -> None:
    with request_trace(question="q", channel="cli") as trace:
        start_span("step")
        with pytest.raises(ValueError):
            with trace_span("llm", "gather"):
                raise ValueError("bad reply")
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            trace.fail(exc)
        trace.finish(status="error", error_code="runtime_runtimeerror")

    step, llm = trace.spans
    assert (step.status, step.finished_at_ms) == ("aborted", trace.finished_at_ms)
    assert (llm.status, llm.error_code, llm.error_message) == ("error", "llm_valueerror", "bad reply")
    assert "ValueError: bad reply" in llm.exception
    assert (trace.error_code, trace.error_message) == ("runtime_runtimeerror", "boom")
    assert "test_unfinished_spans_are_aborted" in trace.exception
    assert trace.exception.rstrip().endswith("RuntimeError: boom")


def test_model_io_is_redacted_and_keeps_tool_call_ids(stored) -> None:
    with request_trace(question="q", channel="web") as trace:
        with trace_span("llm", "gather") as span:
            set_model_io(
                span,
                [HumanMessage("prompt GEMINI_API_KEY=placeholder-value-123"),
                 ToolMessage("result", tool_call_id="c1")],
                AIMessage(content=[{"type": "text", "text": "answer Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
                                    "thought_signature": "abc123def456"}]),
            )
        trace.finish(status="answered")

    io = trace.model_io[span.span_id]
    human, tool = io["input_messages"]
    assert human["content"] == "prompt GEMINI_API_KEY=[REDACTED]"
    assert (tool["class"], tool["tool_call_id"]) == ("ToolMessage", "c1")
    part = io["output"]["content"][0]
    assert part["text"] == "answer Authorization: Bearer [REDACTED]"
    assert part["thought_signature"] == "[provider_internal_thought_signature_omitted chars=12]"


def test_tracing_outside_a_request_records_nothing() -> None:
    with trace_span("tool", "search_news") as span:
        warn("low_relevance")
        set_model_io(span, [HumanMessage("x")], AIMessage("y"))
    assert span.warnings == []
    assert span.finished_at_ms is not None


def test_store_failures_do_not_fail_the_turn(monkeypatch) -> None:
    def broken(trace):
        raise RuntimeError("db down")

    monkeypatch.setattr(trace_store, "insert_running", broken)
    monkeypatch.setattr(trace_store, "persist", broken)
    with request_trace(question="q", channel="web") as trace:
        trace.finish(status="direct")
    assert trace.status == "direct"
