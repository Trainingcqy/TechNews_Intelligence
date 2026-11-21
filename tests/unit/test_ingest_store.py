"""Tests for article state transitions in ingest.store, using a fake cursor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ingest import store
from ingest.config import IngestConfig
from ingest.models import Analysis


class FakeCursor:
    def __init__(self, *rows: tuple, rowcount: int = 0) -> None:
        self.executed: list[tuple[str, object]] = []
        self._rows = list(rows)
        self.rowcount = rowcount

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple:
        return self._rows.pop(0)

    def statements(self, prefix: str) -> list[tuple[str, object]]:
        return [(sql, params) for sql, params in self.executed if sql.startswith(prefix)]


def _article_update(cur: FakeCursor) -> tuple[str, dict]:
    (update,) = cur.statements("UPDATE news.article")
    return update


def _state(cur: FakeCursor) -> tuple:
    sql, params = _article_update(cur)
    for column in ("status", "attempts", "error_kind"):
        assert f"{column} = %({column})s" in sql
    assert "next_attempt_at = now() + make_interval(mins => %(delay_minutes)s)" in sql
    return params["status"], params["attempts"], params["error_kind"], params["delay_minutes"]


_ANALYSIS = Analysis(title_zh="标题", summary="一句完整的摘要内容。", topics=["ai"], kind="news")


@pytest.mark.parametrize(
    ("record", "status"),
    [
        (
            lambda cur: store.record_fetch_success(
                cur, 7, body="b" * 500, published_at=datetime(2026, 11, 17, tzinfo=UTC), title=None
            ),
            "fetched",
        ),
        (lambda cur: store.record_analyze_success(cur, 7, _ANALYSIS), "analyzed"),
        (lambda cur: store.record_embed_success(cur, 7, [0.5, -1.0], "jina-embeddings-v3"), "embedded"),
    ],
)
def test_success_advances_and_clears_attempts(record, status) -> None:
    cur = FakeCursor()
    record(cur)
    assert _article_update(cur)[1]["article_id"] == 7
    assert _state(cur) == (status, 0, None, 0)


@pytest.mark.parametrize(
    ("stage", "attempts_before", "config", "expected"),
    [
        ("fetch", 0, IngestConfig(), ("discovered", 1, "timeout", 10)),
        ("fetch", 1, IngestConfig(), ("discovered", 2, "timeout", 60)),
        ("fetch", 2, IngestConfig(), ("discovered", 3, "timeout", 360)),
        ("fetch", 3, IngestConfig(), ("discovered", 4, "timeout", 1440)),
        ("analyze", 1, IngestConfig(), ("fetched", 2, "timeout", 60)),
        ("embed", 5, IngestConfig(embed_max_attempts=7), ("analyzed", 6, "timeout", 1440)),
    ],
)
def test_failure_counts_attempt_and_backs_off(stage, attempts_before, config, expected) -> None:
    cur = FakeCursor((attempts_before,))
    store.record_failure(cur, 7, stage, "timeout", "detail text", config)
    assert _state(cur) == expected
    assert cur.statements("INSERT INTO news.stage_failure")[0][1] == (7, stage, "timeout", "detail text")


def test_fetch_fifth_failure_degrades_and_advances() -> None:
    cur = FakeCursor((4,))
    store.record_failure(cur, 7, "fetch", "reader_blocked", "403", IngestConfig())
    assert _state(cur) == ("fetched", 0, "reader_blocked", 0)
    assert len(cur.statements("INSERT INTO news.stage_failure")) == 1


def test_analyze_exhausted_sets_failed() -> None:
    cur = FakeCursor((2,))
    store.record_failure(cur, 7, "analyze", "invalid_json", "raw output", IngestConfig())
    status, attempts, error_kind, _ = _state(cur)
    assert (status, attempts, error_kind) == ("failed", 3, "invalid_json")
    assert len(cur.statements("INSERT INTO news.stage_failure")) == 1


@pytest.mark.parametrize("error_kind", ["llm_http", None])
def test_requeue_restores_stage_status(error_kind) -> None:
    cur = FakeCursor(rowcount=4)
    assert store.requeue_failed(cur, error_kind) == 4
    sql, params = _article_update(cur)
    assert "attempts = 0, next_attempt_at = now()" in sql
    assert dict(zip(params["stages"], params["statuses"])) == {
        "fetch": "discovered",
        "analyze": "fetched",
        "embed": "analyzed",
    }
    assert params["error_kind"] == error_kind
