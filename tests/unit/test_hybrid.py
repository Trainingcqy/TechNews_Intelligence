"""Tests for the exact leg of hybrid retrieval."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from agent.retrieval import hybrid

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


@pytest.mark.parametrize(("query", "pattern"), [
    ("OpenAI", "(?<![a-z0-9])OpenAI(?![a-z0-9])"),
    ("GPT-5.5", "(?<![a-z0-9])GPT-5\\.5(?![a-z0-9])"),
    ("C++", "(?<![a-z0-9])C\\+\\+"),
    (".NET", "\\.NET(?![a-z0-9])"),
    ("英伟达", None),
    ("微软 Copilot", None),
])
def test_exact_pattern(query: str, pattern: str | None) -> None:
    assert hybrid._exact_pattern(query) == pattern


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
    assert bool(re.search(hybrid._exact_pattern(query), text, re.IGNORECASE)) is matches


class _CaptureCursor:
    def execute(self, sql: str, params: dict) -> None:
        self.sql, self.params = sql, params

    def fetchall(self) -> list:
        return []


def _captured(query: str) -> tuple[str, dict]:
    cur = _CaptureCursor()
    hybrid.fetch_hybrid_rows(cur, query=query, limit=10, query_vec=None,
                             start_at=NOW - timedelta(days=14), end_at=NOW)
    return cur.sql, cur.params


def test_ascii_query_uses_the_pattern_and_other_queries_a_substring() -> None:
    sql, params = _captured("OpenAI")
    assert params["exact_pattern"] == hybrid._exact_pattern("OpenAI")
    assert "a.title ~* %(exact_pattern)s" in sql
    assert "COALESCE(a.summary, '') ~* %(exact_pattern)s" in sql
    assert "ILIKE" not in sql

    sql, params = _captured("英伟达")
    assert params["exact_pattern"] is None
    assert "a.title ILIKE ('%%' || q.raw_query || '%%')" in sql
    assert "COALESCE(a.summary, '') ILIKE ('%%' || q.raw_query || '%%')" in sql
    assert "~*" not in sql
