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


_ANALYSIS = Analysis(summary="Acme released a model that answers questions twice as fast.", topics=["ai"], kind="news")


@pytest.mark.parametrize(
    ("record", "status"),
    [
        (
            lambda cur: store.record_fetch_success(
                cur, 7, body="b" * 500, body_level="full", published_at=datetime(2026, 11, 17, tzinfo=UTC), title=None
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


@pytest.mark.parametrize("body_level", ["full", "excerpt"])
def test_fetch_success_records_the_body_level(body_level) -> None:
    cur = FakeCursor()
    store.record_fetch_success(cur, 7, body="b" * 500, body_level=body_level, published_at=None, title=None)
    sql, params = _article_update(cur)
    assert "body_level = %(body_level)s" in sql and params["body_level"] == body_level


def _channel(**config) -> store.DueChannel:
    return store.DueChannel(1, "c", "feed", "https://example.com/feed", "corpus", "en", config, None, None)


def _candidate(feed_text: str | None) -> store.Candidate:
    return store.Candidate("https://example.com/a", "t", None, None, feed_text)


@pytest.mark.parametrize(
    ("config", "feed_text", "expected"),
    [
        # A paywalled source never goes to Reader; any feed summary is the excerpt.
        ({"excerpt_only": True}, "A short dek.", ("fetched", "excerpt", "feed")),
        ({"excerpt_only": True}, None, ("fetched", "none", None)),
        ({"body_from_feed": True}, "x" * 1500, ("fetched", "full", "feed")),
        ({"body_from_feed": True}, "x" * 1499, ("discovered", "excerpt", "feed")),
        ({}, "x" * 1500, ("discovered", "excerpt", "feed")),
        ({}, "x" * 199, ("discovered", "none", None)),
    ],
)
def test_initial_state(config, feed_text, expected) -> None:
    assert store._initial_state(_channel(**config), _candidate(feed_text), first_poll=False) == expected


_DATE = datetime(2026, 11, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("first_poll", "published_at", "lastmod", "expected"),
    [
        # The channel's first poll returns its backlog; an entry without any date is kept out of the corpus.
        (True, None, None, ("excluded", "none", None)),
        (True, _DATE, None, ("discovered", "excerpt", "feed")),
        (True, None, _DATE, ("discovered", "excerpt", "feed")),
        (False, None, None, ("discovered", "excerpt", "feed")),
    ],
)
def test_undated_entry_of_a_first_poll_is_excluded(first_poll, published_at, lastmod, expected) -> None:
    candidate = store.Candidate("https://example.com/a", "t", published_at, None, "x" * 1500, lastmod)
    assert store._initial_state(_channel(), candidate, first_poll=first_poll) == expected


def test_promotional_analysis_is_excluded() -> None:
    cur = FakeCursor()
    store.record_analyze_success(cur, 7, _ANALYSIS.model_copy(update={"kind": "promo"}))
    assert _state(cur) == ("excluded", 0, None, 0)
    assert _article_update(cur)[1]["kind"] == "promo"


@pytest.mark.parametrize(
    ("stage", "attempts_before", "config", "expected"),
    [
        ("fetch", 0, IngestConfig(), ("discovered", 1, "timeout", 5)),
        ("fetch", 1, IngestConfig(), ("discovered", 2, "timeout", 30)),
        ("fetch", 2, IngestConfig(), ("discovered", 3, "timeout", 180)),
        ("fetch", 3, IngestConfig(), ("discovered", 4, "timeout", 720)),
        ("analyze", 1, IngestConfig(), ("fetched", 2, "timeout", 30)),
        ("embed", 5, IngestConfig(embed_max_attempts=7), ("analyzed", 6, "timeout", 720)),
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
