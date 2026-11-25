"""The two models' messages, filled from config/prompts/gather.toml and final.toml."""

from __future__ import annotations

from datetime import datetime

from agent.evidence import EvidenceRegistry, render_for_synthesis
from common import prompt_text

# Read at call time, so that an evaluation run can replace it.
FINAL_SYSTEM_PROMPT = prompt_text.text("final.system")

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _clock(key: str, now: datetime) -> str:
    offset = now.strftime("%z")
    utc = f"UTC{offset[:3]}:{offset[3:]}"
    # A fixed-offset zone, such as the CLI machine's, has no name.
    name = getattr(now.tzinfo, "key", None)
    return prompt_text.render(key, now=f"{now:%Y-%m-%d} {_WEEKDAYS[now.weekday()]} {now:%H:%M}",
                              zone=f"{name}, {utc}" if name else utc)


def gather_clock(now: datetime) -> str:
    return _clock("gather.clock", now)


def final_clock(now: datetime) -> str:
    return _clock("final.clock", now)


def render_gather_system(now: datetime, corpus: str, follows: str) -> str:
    """follows is the follows block of agent.memory, empty when the user has none."""
    return prompt_text.render("gather.system", clock=gather_clock(now), corpus=corpus, follows=follows)


def render_final_user(question: str, registry: EvidenceRegistry, max_chars: int,
                      now: datetime) -> tuple[str, list[str]]:
    """The synthesis user message, and the evidence ids whose body was dropped from it."""
    brief, dropped = render_for_synthesis(registry, max_chars)
    return prompt_text.render("final.user", clock=final_clock(now), question=question, brief=brief), dropped
