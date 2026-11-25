from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from agent.config import DEFAULT_ZONE, AgentConfig, load_config, zone_named


def test_load_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MAX_STEPS", raising=False)
    monkeypatch.delenv("AGENT_HISTORY_TURNS", raising=False)
    assert load_config() == AgentConfig()
    assert AgentConfig().max_steps == 5
    assert AgentConfig().history_turns == 4


def test_load_config_env_overrides_only_the_two_fields(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAX_STEPS", "2")
    monkeypatch.setenv("AGENT_HISTORY_TURNS", "2")
    cfg = load_config()
    assert cfg.max_steps == 2
    assert cfg.history_turns == 2
    assert cfg.max_calls_per_step == AgentConfig().max_calls_per_step


def test_load_config_blank_env_keeps_default(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAX_STEPS", "  ")
    assert load_config().max_steps == AgentConfig().max_steps


@pytest.mark.parametrize("value", ["0", "7", "abc"])
def test_load_config_rejects_invalid_max_steps(monkeypatch, value) -> None:
    monkeypatch.setenv("AGENT_MAX_STEPS", value)
    with pytest.raises(ValueError):
        load_config()


@pytest.mark.parametrize("value", ["1", "6"])
def test_load_config_accepts_max_steps_bounds(monkeypatch, value) -> None:
    monkeypatch.setenv("AGENT_MAX_STEPS", value)
    assert load_config().max_steps == int(value)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("America/New_York", ZoneInfo("America/New_York")),
        (None, DEFAULT_ZONE),
        ("", DEFAULT_ZONE),
        ("Mars/Base", DEFAULT_ZONE),
        # a directory of the zone database
        ("America", DEFAULT_ZONE),
        ("../etc/passwd", DEFAULT_ZONE),
    ],
)
def test_zone_named_falls_back_to_the_default(name, expected) -> None:
    assert zone_named(name) is expected
