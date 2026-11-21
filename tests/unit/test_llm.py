from __future__ import annotations

import pytest

import agent.llm as llm
from agent.llm import ModelCallError, ModelHandle, build_models, call_text, call_with_tools, coerce_to_text, extract_json_object

_ENV = (
    "AGENT_GATHER_PROVIDER",
    "AGENT_GATHER_MODEL",
    "AGENT_FINAL_PROVIDER",
    "AGENT_FINAL_MODEL",
    "AGENT_TEMPERATURE",
)


@pytest.fixture
def fake_build(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(llm, "build_chat_model", fake)
    build_models.cache_clear()
    yield calls
    build_models.cache_clear()


def test_build_models_defaults(fake_build) -> None:
    gather, final = build_models()
    assert (gather.role, gather.provider, gather.model) == ("gather", "deepseek", "deepseek-flash")
    assert (final.role, final.provider, final.model) == ("final", "vertex", "gemini-3.1-pro-preview")
    assert [c["temperature"] for c in fake_build] == [0.1, 0.1]


def test_build_models_env_overrides(fake_build, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_GATHER_PROVIDER", "vertex")
    monkeypatch.setenv("AGENT_GATHER_MODEL", "g-model")
    monkeypatch.setenv("AGENT_FINAL_PROVIDER", "deepseek")
    monkeypatch.setenv("AGENT_FINAL_MODEL", "f-model")
    monkeypatch.setenv("AGENT_TEMPERATURE", "0.4")
    gather, final = build_models()
    assert (gather.provider, gather.model) == ("vertex", "g-model")
    assert (final.provider, final.model) == ("deepseek", "f-model")
    assert [c["temperature"] for c in fake_build] == [0.4, 0.4]


def test_provider_override_without_model_uses_provider_default(fake_build, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_GATHER_PROVIDER", "vertex")
    gather, _ = build_models()
    assert (gather.provider, gather.model) == ("vertex", "gemini-3.1-pro-preview")


def test_build_models_is_cached(fake_build) -> None:
    assert build_models() is build_models()
    assert len(fake_build) == 2


def test_build_failure_raises_and_is_not_cached(fake_build, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_FINAL_PROVIDER", "nope")
    with pytest.raises(ModelCallError) as info:
        build_models()
    assert info.value.code == "model_build_valueerror"
    monkeypatch.delenv("AGENT_FINAL_PROVIDER")
    assert build_models()[1].provider == "vertex"


class _FailingClient:
    def invoke(self, messages):
        raise TimeoutError("slow")

    def bind_tools(self, tools):
        return self


@pytest.mark.parametrize("call", [lambda h: call_text(h, [], node="final_synthesizer"),
                                  lambda h: call_with_tools(h, [], [], node="gather")])
def test_call_failure_raises_model_call_error(call) -> None:
    handle = ModelHandle(role="final", provider="vertex", model="m", client=_FailingClient())
    with pytest.raises(ModelCallError) as info:
        call(handle)
    assert info.value.code == "model_timeouterror"
    assert isinstance(info.value.__cause__, TimeoutError)


def test_coerce_to_text() -> None:
    assert coerce_to_text(["a", {"text": "b"}, {"type": "image"}, 3]) == "a\nb"
    assert coerce_to_text(None) == ""
    assert coerce_to_text(12) == "12"


def test_extract_json_object() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('结果如下 {"a": {"b": 2}} 以上') == {"a": {"b": 2}}
    assert extract_json_object("[1, 2]") is None
    assert extract_json_object("not json") is None
