"""config/models.toml: the model each use runs and the parameters sent with its requests."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

MODELS_FILE = Path(__file__).resolve().parents[1] / "config" / "models.toml"


@dataclass(frozen=True, kw_only=True)
class ChatModel:
    provider: str
    model: str
    temperature: float | None = None  # None 时请求不带温度


@dataclass(frozen=True, kw_only=True)
class AnalyzeModel(ChatModel):
    max_tokens: int
    json_mode: bool


@dataclass(frozen=True, kw_only=True)
class JinaModel:
    model: str


USES = {"gather": ChatModel, "final": ChatModel, "analyze": AnalyzeModel, "embedding": JinaModel, "rerank": JinaModel}


@cache
def _settings() -> dict:
    with MODELS_FILE.open("rb") as file:
        return tomllib.load(file)


def get(use: str):
    """The section of one use; a key the use does not take, or a missing one, raises TypeError."""
    return USES[use](**_settings()[use])


def provider_default_models() -> dict[str, str]:
    return {name: entry["default_model"] for name, entry in _settings()["providers"].items()}
