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


def _captured(query: str, **kwargs) -> tuple[str, dict]:
    cur = _CaptureCursor()
    hybrid.fetch_hybrid_rows(cur, query=query, limit=10, query_vec=kwargs.pop("query_vec", None),
                             start_at=NOW - timedelta(days=14), end_at=NOW, **kwargs)
    return cur.sql, cur.params


SEEN_AFTER = "AND (%(seen_after)s::timestamptz IS NULL OR a.first_seen_at > %(seen_after)s)"
EXCLUDED = "AND (%(exclude_url_keys)s::text[] IS NULL OR a.url_key <> ALL(%(exclude_url_keys)s::text[]))"


def test_every_channel_applies_seen_after_and_excluded_urls_before_its_limit() -> None:
    sql, params = _captured("OpenAI", query_vec=[0.1, 0.2], seen_after=NOW - timedelta(days=3),
                            exclude_url_keys={"a.com/1", "b.com/2"})
    channels = {
        "lexical": sql.split("lexical_ranked AS (")[1].split("LIMIT %(lexical_limit)s")[0],
        "semantic": sql.split("semantic AS (")[1].split("LIMIT %(semantic_limit)s")[0],
        "exact": sql.split("exact_source AS (")[1].split("LIMIT %(lexical_limit)s")[0],
    }
    for name, body in channels.items():
        assert SEEN_AFTER in body, name
        assert EXCLUDED in body, name
    assert params["seen_after"] == NOW - timedelta(days=3)
    assert sorted(params["exclude_url_keys"]) == ["a.com/1", "b.com/2"]


def test_without_conditions_the_filter_parameters_are_null() -> None:
    _, params = _captured("OpenAI")
    assert (params["seen_after"], params["exclude_url_keys"]) == (None, None)


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
