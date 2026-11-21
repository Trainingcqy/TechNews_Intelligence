"""Tests for the hybrid retrieval SQL latency script."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from eval.retrieval import sql_latency

NOW = datetime(2026, 11, 22, 4, 0, tzinfo=timezone.utc)


def test_hybrid_sql_is_the_query_fetch_hybrid_rows_sends_for_the_window() -> None:
    sql, params = sql_latency.hybrid_sql("OpenAI", [0.5] * 1024, days=365, limit=72, now=NOW)

    assert "%(query_vec)s::vector" in sql
    assert params["query"] == "OpenAI"
    assert (params["start_at"], params["end_at"]) == (NOW - timedelta(days=365), NOW)
    assert params["query_vec"].startswith("[0.5,")


class _ExplainCursor:
    def __init__(self, plan) -> None:  # noqa: ANN001
        self.plan = plan
        self.executed: list[tuple[str, dict]] = []

    def execute(self, sql: str, params: dict) -> None:
        self.executed.append((sql, params))

    def fetchone(self):  # noqa: ANN201
        return (self.plan,)


def test_explain_ms_adds_planning_and_execution_time_under_explain_analyze() -> None:
    cur = _ExplainCursor([{"Plan": {}, "Planning Time": 1.5, "Execution Time": 10.25}])

    assert sql_latency.explain_ms(cur, "SELECT 1", {"a": 1}) == 11.75
    assert cur.executed == [("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) SELECT 1", {"a": 1})]
    assert sql_latency.explain_ms(_ExplainCursor('[{"Planning Time": 2, "Execution Time": 3}]'), "SELECT 1", {}) == 5
