from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import agent.llm as llm
from agent import trace_store
from agent.llm import ModelCallError, ModelHandle, build_models, call_text, call_with_tools, coerce_to_text, extract_json_object
from agent.trace import request_trace

_GATHER = '[gather]\nprovider = "deepseek"\nmodel = "g-model"\n'


@pytest.fixture
def fake_build(monkeypatch):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(llm, "build_chat_model", fake)
    build_models.cache_clear()
    yield calls
    build_models.cache_clear()


def test_build_models_reads_each_use(fake_build, models_file) -> None:
    models_file('[gather]\nprovider = "vertex"\nmodel = "g-model"\ntemperature = 0.4\n'
                '[final]\nprovider = "DeepSeek"\nmodel = "f-model"\n')
    gather, final = build_models()
    assert (gather.role, gather.provider, gather.model, gather.temperature) == ("gather", "vertex", "g-model", 0.4)
    assert (final.role, final.provider, final.model, final.temperature) == ("final", "deepseek", "f-model", None)
    assert fake_build == [{"provider": "vertex", "model_name": "g-model", "temperature": 0.4},
                          {"provider": "deepseek", "model_name": "f-model", "temperature": None}]


def test_build_models_is_cached(fake_build) -> None:
    assert build_models() is build_models()
    assert len(fake_build) == 2


def test_build_failure_raises_and_is_not_cached(fake_build, models_file) -> None:
    models_file(_GATHER + '[final]\nprovider = "nope"\nmodel = "f-model"\n')
    with pytest.raises(ModelCallError) as info:
        build_models()
    assert info.value.code == "model_build_valueerror"
    models_file(_GATHER + '[final]\nprovider = "vertex"\nmodel = "f-model"\n')
    assert build_models()[1].provider == "vertex"


class _FailingClient:
    def invoke(self, messages):
        raise TimeoutError("slow")

    def bind_tools(self, tools):
        return self


@pytest.mark.parametrize("call", [lambda h: call_text(h, []), lambda h: call_with_tools(h, [], [])])
def test_call_failure_raises_model_call_error(call) -> None:
    handle = ModelHandle(role="final", provider="vertex", model="m", client=_FailingClient())
    with pytest.raises(ModelCallError) as info:
        call(handle)
    assert info.value.code == "model_timeouterror"
    assert isinstance(info.value.__cause__, TimeoutError)


class _Client:
    def __init__(self, reply):
        self.reply = reply

    def invoke(self, messages):
        return self.reply


def test_call_records_the_llm_span(monkeypatch) -> None:
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    reply = AIMessage(content="答案", response_metadata={"model_name": "gemini-3.1-pro-preview-0101",
                                                        "finish_reason": "STOP"},
                      usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 99})
    handle = ModelHandle(role="final", provider="vertex", model="gemini-3.1-pro-preview", client=_Client(reply),
                         temperature=0.1)

    with request_trace(question="q", channel="web") as trace:
        assert call_text(handle, [HumanMessage("问题")]) == "答案"

    (span,) = trace.spans
    assert (span.kind, span.name, span.status, span.output) == ("llm", "final", "ok", {"chars": 2, "tool_calls": 0})
    assert span.attributes == {
        "provider": "vertex", "model": "gemini-3.1-pro-preview", "temperature": 0.1,
        "response_model": "gemini-3.1-pro-preview-0101", "finish_reason": "STOP",
        "usage": {"input_tokens": 100, "cache_read_tokens": 0, "output_tokens": 10, "reasoning_tokens": 0,
                  "total_tokens": 99},
    }
    assert span.warnings == ["usage_inconsistent"]
    assert trace.model_io[span.span_id]["input_messages"][0]["content"] == "问题"


def test_coerce_to_text() -> None:
    assert coerce_to_text(["a", {"text": "b"}, {"type": "image"}, 3]) == "a\nb"
    assert coerce_to_text(None) == ""
    assert coerce_to_text(12) == "12"


def test_extract_json_object() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('结果如下 {"a": {"b": 2}} 以上') == {"a": {"b": 2}}
    assert extract_json_object("[1, 2]") is None
    assert extract_json_object("not json") is None
