from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The zone of the user's clock: the current time given to the models, date filters and the dates shown to users are in
# it. A web request names it; Telegram, the evaluation and a missing or unknown name use DEFAULT_ZONE. The database
# session is UTC.
DEFAULT_ZONE = ZoneInfo("Asia/Shanghai")
_USER_ZONE: ContextVar[tzinfo] = ContextVar("agent_user_zone", default=DEFAULT_ZONE)


def zone_named(name: str | None) -> tzinfo:
    if not name:
        return DEFAULT_ZONE
    try:
        return ZoneInfo(name)
    # A name such as "America" is a directory of the zone database and raises an OSError.
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return DEFAULT_ZONE


def user_zone() -> tzinfo:
    return _USER_ZONE.get()


@contextmanager
def using_zone(zone: tzinfo) -> Iterator[None]:
    token = _USER_ZONE.set(zone)
    try:
        yield
    finally:
        _USER_ZONE.reset(token)


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int = 5  # AGENT_MAX_STEPS, 1 to 6
    max_calls_per_step: int = 5
    max_read_articles: int = 10  # per request
    history_turns: int = 4  # AGENT_HISTORY_TURNS
    history_answer_chars: int = 6000
    synthesis_evidence_chars: int = 60000


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
