"""Chat-model factory shared by the agent, the ingest pipeline and the evaluation scripts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

DEFAULT_GEMINI_API_MODEL = "gemini-2.5-pro"
DEFAULT_VERTEX_MODEL = "gemini-3.1-pro-preview"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_FLASH_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# The supported providers and the model used when a caller names none.
DEFAULT_MODELS = {
    "gemini_api": DEFAULT_GEMINI_API_MODEL,
    "vertex": DEFAULT_VERTEX_MODEL,
    "deepseek": DEFAULT_DEEPSEEK_MODEL,
}


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_provider(provider: str | None) -> str:
    normalized = _clean(provider).lower()
    if normalized not in DEFAULT_MODELS:
        raise ValueError(f"Unsupported provider: {provider}. Expected one of: {'|'.join(DEFAULT_MODELS)}.")
    return normalized


def resolve_model_config(*, provider: str | None, model_name: str | None) -> ModelConfig:
    normalized = normalize_provider(provider)
    return ModelConfig(provider=normalized, model=_clean(model_name) or DEFAULT_MODELS[normalized])


def _deepseek_timeout() -> float:
    raw = _clean(os.getenv("DEEPSEEK_TIMEOUT_SEC", "120"))
    try:
        return max(5.0, min(float(raw), 600.0))
    except Exception:
        return 120.0


def build_chat_model(*, provider: str | None, model_name: str | None, temperature: float) -> Any:
    """Create a LangChain chat model for the requested provider."""
    config = resolve_model_config(provider=provider, model_name=model_name)

    if config.provider == "gemini_api":
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = _clean(os.getenv("GEMINI_API_KEY", ""))
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for provider=gemini_api.")
        return ChatGoogleGenerativeAI(
            model=config.model,
            google_api_key=api_key,
            temperature=float(temperature),
        )

    if config.provider == "vertex":
        from langchain_google_vertexai import ChatVertexAI

        project = _clean(os.getenv("VERTEX_PROJECT", ""))
        if not project:
            raise ValueError("VERTEX_PROJECT is required for provider=vertex.")
        return ChatVertexAI(
            model=config.model,
            project=project,
            location=_clean(os.getenv("VERTEX_LOCATION", "")) or "global",
            temperature=float(temperature),
        )

    api_key = _clean(os.getenv("DEEPSEEK_API_KEY", ""))
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required for provider=deepseek.")
    base_url = (_clean(os.getenv("DEEPSEEK_BASE_URL", "")) or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=config.model,
        api_key=api_key,
        base_url=base_url,
        temperature=float(temperature),
        timeout=_deepseek_timeout(),
    )
