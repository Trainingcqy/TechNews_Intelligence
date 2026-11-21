from __future__ import annotations

from agent.retrieval.recall_profile import PROFILE_BASE, PROFILE_WIDE, resolve_recall_profile


def test_recall_profile_defaults_to_base(monkeypatch) -> None:
    monkeypatch.delenv("EVAL_RECALL_PROFILE", raising=False)
    profile = resolve_recall_profile()
    assert profile.profile == PROFILE_BASE
    assert profile.sim_floor == 0.20
    assert profile.query_cand_multiplier == 6
    assert profile.query_cand_max == 72


def test_recall_profile_wide_defaults(monkeypatch) -> None:
    monkeypatch.setenv("EVAL_RECALL_PROFILE", PROFILE_WIDE)
    profile = resolve_recall_profile()
    assert profile.profile == PROFILE_WIDE
    assert profile.sim_floor == 0.14
    assert profile.query_cand_multiplier == 10
    assert profile.query_cand_max == 120


def test_recall_profile_prefers_new_env_over_legacy(monkeypatch) -> None:
    monkeypatch.setenv("EVAL_RECALL_PROFILE", PROFILE_BASE)
    monkeypatch.setenv("SEMANTIC_POOL_SIM_FLOOR", "0.33")
    monkeypatch.setenv("EVAL_RECALL_SIM_FLOOR", "0.17")
    monkeypatch.setenv("EVAL_RECALL_QUERY_CAND_MULTIPLIER", "9")
    monkeypatch.setenv("EVAL_RECALL_QUERY_CAND_MAX", "111")
    profile = resolve_recall_profile()
    assert profile.sim_floor == 0.17
    assert profile.query_cand_multiplier == 9
    assert profile.query_cand_max == 111


def test_recall_profile_unknown_value_falls_back_to_base(monkeypatch) -> None:
    monkeypatch.setenv("EVAL_RECALL_PROFILE", "unknown_profile")
    profile = resolve_recall_profile()
    assert profile.profile == PROFILE_BASE
