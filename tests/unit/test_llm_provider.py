from __future__ import annotations

import sys
import types

import pytest

from common.llm_provider import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    ModelConfig,
    build_chat_model,
    normalize_provider,
    resolve_model_config,
)


class _FakeChatModel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _install_fake_module(monkeypatch: pytest.MonkeyPatch, name: str, attr: str) -> None:
    module = types.ModuleType(name)
    setattr(module, attr, _FakeChatModel)
    monkeypatch.setitem(sys.modules, name, module)


def test_normalize_provider_accepts_only_the_three_names() -> None:
    assert normalize_provider("deepseek") == "deepseek"
    assert normalize_provider(" Vertex ") == "vertex"
    with pytest.raises(ValueError, match="Unsupported provider"):
        normalize_provider("deepseek_api")


def test_deepseek_builder_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        build_chat_model(provider="deepseek", model_name="deepseek-v4-pro", temperature=0.0)


def test_deepseek_builder_uses_base_url_and_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "langchain_openai", "ChatOpenAI")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    model = build_chat_model(provider="deepseek", model_name="", temperature=0.2)

    assert isinstance(model, _FakeChatModel)
    assert model.kwargs["model"] == DEFAULT_DEEPSEEK_MODEL
    assert model.kwargs["api_key"] == "sk-test"
    assert model.kwargs["base_url"] == DEFAULT_DEEPSEEK_BASE_URL
    assert model.kwargs["temperature"] == 0.2


def test_gemini_and_vertex_builders_use_shared_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "langchain_google_genai", "ChatGoogleGenerativeAI")
    _install_fake_module(monkeypatch, "langchain_google_vertexai", "ChatVertexAI")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("VERTEX_PROJECT", "vertex-project")
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)

    gemini = build_chat_model(provider="gemini_api", model_name="gemini-test", temperature=0.1)
    vertex = build_chat_model(provider="vertex", model_name="gemini-vertex-test", temperature=0.0)

    assert gemini.kwargs["model"] == "gemini-test"
    assert gemini.kwargs["google_api_key"] == "gemini-key"
    assert vertex.kwargs["model"] == "gemini-vertex-test"
    assert vertex.kwargs["project"] == "vertex-project"
    assert vertex.kwargs["location"] == "global"

    monkeypatch.setenv("VERTEX_LOCATION", "us-central1")
    assert build_chat_model(provider="vertex", model_name="m", temperature=0.0).kwargs["location"] == "us-central1"


def test_vertex_reads_only_vertex_project(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "langchain_google_vertexai", "ChatVertexAI")
    monkeypatch.delenv("VERTEX_PROJECT", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "other-project")

    with pytest.raises(ValueError, match="VERTEX_PROJECT"):
        build_chat_model(provider="vertex", model_name="m", temperature=0.0)


def test_missing_model_takes_the_provider_default_not_an_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DEEPSEEK_MODEL", "VERTEX_GENERATION_MODEL", "VERTEX_MODEL", "GEMINI_MODEL"):
        monkeypatch.setenv(name, "from-env")

    assert resolve_model_config(provider="deepseek", model_name=None) == ModelConfig("deepseek", "deepseek-v4-pro")
    assert resolve_model_config(provider="vertex", model_name="") == ModelConfig("vertex", "gemini-3.1-pro-preview")
    assert resolve_model_config(provider="gemini_api", model_name=None) == ModelConfig("gemini_api", "gemini-2.5-pro")
