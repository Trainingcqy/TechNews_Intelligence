"""Recall-profile resolution for retrieval-related tools.

Centralizes all recall knobs so query retrieval and semantic pools
follow one consistent profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


PROFILE_BASE = "base"
PROFILE_WIDE = "wide"


@dataclass(frozen=True)
class RecallProfile:
    profile: str
    sim_floor: float
    query_cand_multiplier: int
    query_cand_max: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "sim_floor": self.sim_floor,
            "query_cand_multiplier": self.query_cand_multiplier,
            "query_cand_max": self.query_cand_max,
        }


# Hardcoded fallback values if profile/env parsing fails.
_HARDCODED_DEFAULTS = RecallProfile(
    profile=PROFILE_BASE,
    sim_floor=0.20,
    query_cand_multiplier=6,
    query_cand_max=72,
)


_PROFILE_TABLE: dict[str, RecallProfile] = {
    PROFILE_BASE: _HARDCODED_DEFAULTS,
    PROFILE_WIDE: RecallProfile(
        profile=PROFILE_WIDE,
        sim_floor=0.14,
        query_cand_multiplier=10,
        query_cand_max=120,
    ),
}


def _to_int(value: str | None, *, min_value: int, max_value: int, fallback: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return int(fallback)
    return max(min_value, min(max_value, parsed))


def _to_float(value: str | None, *, min_value: float, max_value: float, fallback: float) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return float(fallback)
    return max(min_value, min(max_value, parsed))


def _read_env(*keys: str) -> str:
    for key in keys:
        raw = str(os.getenv(key, "")).strip()
        if raw:
            return raw
    return ""


def _resolve_profile_name() -> str:
    raw = str(os.getenv("EVAL_RECALL_PROFILE", PROFILE_BASE)).strip().lower()
    alias = {
        "": PROFILE_BASE,
        "default": PROFILE_BASE,
        PROFILE_BASE: PROFILE_BASE,
        PROFILE_WIDE: PROFILE_WIDE,
    }
    resolved = alias.get(raw, PROFILE_BASE)
    if raw and raw not in alias:
        print(f"[Warn] unknown EVAL_RECALL_PROFILE='{raw}', fallback='{PROFILE_BASE}'.")
    return resolved


def resolve_recall_profile() -> RecallProfile:
    """Resolve effective recall profile.

    Priority (high -> low):
    1. New fine-grained env (EVAL_RECALL_*)
    2. Legacy env keys
    3. Profile defaults (EVAL_RECALL_PROFILE=base|wide)
    4. Hardcoded defaults
    """
    profile_name = _resolve_profile_name()
    defaults = _PROFILE_TABLE.get(profile_name, _HARDCODED_DEFAULTS)

    sim_floor = _to_float(
        _read_env("EVAL_RECALL_SIM_FLOOR", "SEMANTIC_POOL_SIM_FLOOR"),
        min_value=0.0,
        max_value=1.0,
        fallback=defaults.sim_floor,
    )
    query_cand_multiplier = _to_int(
        _read_env("EVAL_RECALL_QUERY_CAND_MULTIPLIER", "SEARCH_NEWS_CANDIDATE_MULTIPLIER"),
        min_value=1,
        max_value=30,
        fallback=defaults.query_cand_multiplier,
    )
    query_cand_max = _to_int(
        _read_env("EVAL_RECALL_QUERY_CAND_MAX", "SEARCH_NEWS_CANDIDATE_MAX"),
        min_value=1,
        max_value=500,
        fallback=defaults.query_cand_max,
    )

    return RecallProfile(
        profile=profile_name,
        sim_floor=sim_floor,
        query_cand_multiplier=query_cand_multiplier,
        query_cand_max=query_cand_max,
    )
