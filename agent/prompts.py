"""The two models' messages, filled from config/prompts/gather.toml and final.toml."""

from __future__ import annotations

from datetime import datetime

from agent.evidence import EvidenceRegistry, render_for_synthesis
from common import prompt_text

# Read at call time, so that an evaluation run can replace it.
FINAL_SYSTEM_PROMPT = prompt_text.text("final.system")

_WEEKDAYS_ZH = "一二三四五六日"
_WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _zone_text(now: datetime, separator: str) -> str:
    """The zone's name and UTC offset; only the offset for a zone without a name, such as the CLI machine's."""
    offset = now.strftime("%z")
    utc = f"UTC{offset[:3]}:{offset[3:]}"
    name = getattr(now.tzinfo, "key", None)
    return f"{name}{separator}{utc}" if name else utc


def gather_clock(now: datetime) -> str:
    return prompt_text.render("gather.clock", now=f"{now:%Y-%m-%d} 星期{_WEEKDAYS_ZH[now.weekday()]} {now:%H:%M}",
                              zone=_zone_text(now, "，"))


def final_clock(now: datetime) -> str:
    return prompt_text.render("final.clock", now=f"{now:%Y-%m-%d} {_WEEKDAYS_EN[now.weekday()]} {now:%H:%M}",
                              zone=_zone_text(now, ", "))


def render_gather_system(now: datetime, corpus: str, follows: str) -> str:
    """follows is the follows block of agent.memory, empty when the user has none."""
    return prompt_text.render("gather.system", clock=gather_clock(now), corpus=corpus, follows=follows)


def render_final_user(question: str, registry: EvidenceRegistry, max_chars: int,
                      now: datetime) -> tuple[str, list[str]]:
    """The synthesis user message, and the evidence ids whose body was dropped from it."""
    brief, dropped = render_for_synthesis(registry, max_chars)
    return prompt_text.render("final.user", clock=final_clock(now), question=question, brief=brief), dropped
