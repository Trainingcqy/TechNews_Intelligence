from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone

# Date filters and the dates shown to users are in Beijing time; the database session is UTC.
BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int = 3  # AGENT_MAX_STEPS，取值 1–6
    max_calls_per_step: int = 4
    max_read_articles: int = 8  # 单次请求累计读取篇数
    history_turns: int = 4  # AGENT_HISTORY_TURNS
    history_answer_chars: int = 1500
    synthesis_evidence_chars: int = 60000
    gather_read_preview_chars: int = 800
    max_follows_in_prompt: int = 10


def load_config() -> AgentConfig:
    overrides: dict[str, int] = {}
    raw = os.environ.get("AGENT_MAX_STEPS", "").strip()
    if raw:
        steps = int(raw)
        if not 1 <= steps <= 6:
            raise ValueError(f"AGENT_MAX_STEPS must be between 1 and 6, got {raw!r}")
        overrides["max_steps"] = steps
    raw = os.environ.get("AGENT_HISTORY_TURNS", "").strip()
    if raw:
        overrides["history_turns"] = int(raw)
    return AgentConfig(**overrides)
