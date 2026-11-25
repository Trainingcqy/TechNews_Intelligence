"""Tests for the exact leg of hybrid retrieval."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.retrieval import hybrid, literal

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


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
    assert params["exact_pattern"] == literal.exact_pattern("OpenAI")
    assert "a.title ~* %(exact_pattern)s" in sql
    assert "COALESCE(a.summary, '') ~* %(exact_pattern)s" in sql
    assert "ILIKE" not in sql

    sql, params = _captured("英伟达")
    assert params["exact_pattern"] is None
    assert "a.title ILIKE ('%%' || q.raw_query || '%%')" in sql
    assert "COALESCE(a.summary, '') ILIKE ('%%' || q.raw_query || '%%')" in sql
    assert "~*" not in sql
