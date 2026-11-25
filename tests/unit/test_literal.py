"""Tests for the literal matching shared by the exact leg and story browsing."""

from __future__ import annotations

import re

import pytest

from agent.retrieval import literal


@pytest.mark.parametrize(("query", "pattern"), [
    ("OpenAI", "(?<![a-z0-9])OpenAI(?![a-z0-9])"),
    ("GPT-5.5", "(?<![a-z0-9])GPT-5\\.5(?![a-z0-9])"),
    ("C++", "(?<![a-z0-9])C\\+\\+"),
    (".NET", "\\.NET(?![a-z0-9])"),
    ("英伟达", None),
    ("微软 Copilot", None),
])
def test_exact_pattern(query: str, pattern: str | None) -> None:
    assert literal.exact_pattern(query) == pattern


@pytest.mark.parametrize(("query", "text", "matches"), [
    ("AWS", "亚马逊发布AWS新服务", True),
    ("AWS", "AWS's new region", True),
    ("AWS", "new laws on AI", False),
    ("Claude Opus 4.6", "Anthropic发布Claude Opus 4.6模型", True),
    ("Claude Opus 4.6", "Claude Opus 4.65", False),
    ("GPT-5.5", "GPT-5x5", False),
])
def test_exact_pattern_boundaries(query: str, text: str, matches: bool) -> None:
    # Python's re reads the lookarounds and escapes the same way as a case-insensitive Postgres regex.
    assert bool(re.search(literal.exact_pattern(query), text, re.IGNORECASE)) is matches
