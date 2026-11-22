"""Unit tests for rerank integration on search/retrieval paths."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import requests

from agent.retrieval import rerank as rerank_mod
from agent.retrieval import search as retrieval_mod


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, _params=()):
        return None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def rollback(self):
        return None


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return dict(self._payload)


def _row(article_id: int, title: str, title_zh: str, publisher: str, *, score: float, match_score: float = 0.5) -> tuple:
    return (
        article_id, f"https://{article_id}.com", title, title_zh, f"Summary {article_id}", ["ai"], "news",
        publisher, "media", "en", datetime(2026, 4, 10, 10, 0, 0), "full", None, None, None, 100 + article_id,
        score, 0.1, 0.2, 0.0, match_score,
    )


ROWS = [
    _row(1, "Headline A", "标题A", "TechCrunch", score=1.11),
    _row(2, "Headline B", "", "The Verge", score=1.01),
]


def test_resolve_rerank_mode_prefers_explicit_then_env(monkeypatch) -> None:
    monkeypatch.delenv("NEWS_RERANK_MODE", raising=False)
    assert rerank_mod.resolve_rerank_mode() == rerank_mod.RERANK_MODE_LLM
    assert rerank_mod.resolve_rerank_mode("none") == rerank_mod.RERANK_MODE_NONE

    monkeypatch.setenv("NEWS_RERANK_MODE", "none")
    assert rerank_mod.resolve_rerank_mode() == rerank_mod.RERANK_MODE_NONE

    monkeypatch.setenv("NEWS_RERANK_MODE", "llm_rerank")
    assert rerank_mod.resolve_rerank_mode() == rerank_mod.RERANK_MODE_LLM
    assert rerank_mod.resolve_rerank_mode("none") == rerank_mod.RERANK_MODE_NONE

    monkeypatch.setenv("NEWS_RERANK_MODE", "unknown_mode")
    assert rerank_mod.resolve_rerank_mode() == rerank_mod.RERANK_MODE_NONE

    monkeypatch.setenv("NEWS_RERANK_MODE", "llm")
    assert rerank_mod.resolve_rerank_mode() == rerank_mod.RERANK_MODE_LLM
    assert rerank_mod.resolve_rerank_mode("unknown-mode") == rerank_mod.RERANK_MODE_NONE


def test_lookup_candidates_by_query_rerank_off_keeps_recall_order() -> None:
    fake_conn = _FakeConn(ROWS)

    with (
        patch.object(retrieval_mod, "get_conn", lambda: fake_conn),
        patch.object(retrieval_mod, "put_conn", lambda _conn: None),
        patch.object(retrieval_mod, "_get_query_embedding", lambda _q: None),
    ):
        candidates, meta = retrieval_mod.lookup_candidates_by_query(
            query="OpenAI",
            days=14,
            limit=2,
            rerank_mode="none",
        )

    assert [item["url"] for item in candidates] == ["https://1.com", "https://2.com"]
    assert meta["rerank_mode"] == "none"
    assert meta["candidate_count"] == 2
    assert meta["top_k"] == 2
    assert meta["fallback"] is False
    assert meta["retrieval_mode"] == "postgres_rrf_hybrid"
    assert meta["fusion"] == "rrf"
    assert meta["points_boost"] is False
    assert "lexical" in meta["channels_used"]


def test_lookup_candidates_by_query_keeps_sql_order_and_maps_columns() -> None:
    rows = [
        _row(1, "Headline A", "标题A", "TechCrunch", score=2.0, match_score=0.3),
        _row(2, "Headline B", "", "The Verge", score=1.5, match_score=0.9),
    ]
    fake_conn = _FakeConn(rows)

    with (
        patch.object(retrieval_mod, "get_conn", lambda: fake_conn),
        patch.object(retrieval_mod, "put_conn", lambda _conn: None),
        patch.object(retrieval_mod, "_get_query_embedding", lambda _q: None),
    ):
        candidates, meta = retrieval_mod.lookup_candidates_by_query(
            query="OpenAI",
            days=14,
            limit=2,
            rerank_mode="none",
        )

    assert [item["url"] for item in candidates] == ["https://1.com", "https://2.com"]
    assert [item["score"] for item in candidates] == [2.0, 1.5]
    assert [item["match_score"] for item in candidates] == [0.3, 0.9]
    assert [item["title"] for item in candidates] == ["标题A", "Headline B"]
    assert [item["source_type"] for item in candidates] == ["TechCrunch", "The Verge"]
    assert candidates[0]["article"].story_id == 101


def test_lookup_candidates_by_query_empty_query_returns_structured_meta() -> None:
    candidates, meta = retrieval_mod.lookup_candidates_by_query(
        query="   ",
        rerank_mode="llm_rerank",
    )
    assert candidates == []
    assert meta["candidate_count"] == 0
    assert meta["top_k"] == 0
    assert meta["fallback"] is False


def test_lookup_candidates_by_query_rerank_on_uses_reranker_order(monkeypatch) -> None:
    fake_conn = _FakeConn(ROWS)

    def _fake_post(_url, *, headers=None, json=None, timeout=None):
        assert headers is not None
        assert json is not None
        assert json["query"] == "OpenAI"
        assert json["documents"] == [
            "Title: 标题A\nSummary: Summary 1\nSource: TechCrunch\nURL: https://1.com",
            "Title: Headline B\nSummary: Summary 2\nSource: The Verge\nURL: https://2.com",
        ]
        assert timeout is not None
        return _FakeResponse(
            {
                "model": json["model"],
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.12},
                ],
            }
        )

    monkeypatch.setenv("JINA_API_KEY", "fake-key")
    with (
        patch.object(retrieval_mod, "get_conn", lambda: fake_conn),
        patch.object(retrieval_mod, "put_conn", lambda _conn: None),
        patch.object(retrieval_mod, "_get_query_embedding", lambda _q: None),
        patch.object(rerank_mod.requests, "post", _fake_post),
    ):
        candidates, meta = retrieval_mod.lookup_candidates_by_query(
            query="OpenAI",
            days=14,
            limit=2,
            rerank_mode="llm_rerank",
        )

    assert candidates[0]["url"] == "https://2.com"
    assert candidates[1]["url"] == "https://1.com"
    assert meta["rerank_mode"] == "llm_rerank"
    assert meta["candidate_count"] == 2
    assert meta["top_k"] == 2
    assert meta["fallback"] is False
    assert meta["retrieval_mode"] == "postgres_rrf_hybrid"


def test_lookup_candidates_by_query_rerank_failure_fallbacks_to_recall_order(monkeypatch) -> None:
    fake_conn = _FakeConn(ROWS)

    def _raise_timeout(*_args, **_kwargs):
        raise requests.Timeout("rerank timeout")

    monkeypatch.setenv("JINA_API_KEY", "fake-key")
    with (
        patch.object(retrieval_mod, "get_conn", lambda: fake_conn),
        patch.object(retrieval_mod, "put_conn", lambda _conn: None),
        patch.object(retrieval_mod, "_get_query_embedding", lambda _q: None),
        patch.object(rerank_mod.requests, "post", _raise_timeout),
    ):
        candidates, meta = retrieval_mod.lookup_candidates_by_query(
            query="OpenAI",
            days=14,
            limit=2,
            rerank_mode="llm_rerank",
        )

    assert [item["url"] for item in candidates] == ["https://1.com", "https://2.com"]
    assert meta["rerank_mode"] == "llm_rerank"
    assert meta["candidate_count"] == 2
    assert meta["top_k"] == 2
    assert meta["fallback"] is True
