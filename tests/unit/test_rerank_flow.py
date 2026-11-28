"""Unit tests for rerank integration on search/retrieval paths."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import requests

from agent import trace_store
from agent.retrieval import embeddings as embeddings_mod
from agent.retrieval import rerank as rerank_mod
from agent.retrieval import search as retrieval_mod
from agent.trace import request_trace


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
        publisher, "media", "en", datetime(2026, 4, 10, 10, 0, 0), datetime(2026, 4, 10, 11, 0, 0), "full",
        100 + article_id,
        score, 0.1, 0.2, 0.0, match_score,
    )


ROWS = [
    _row(1, "Headline A", "标题A", "TechCrunch", score=1.11),
    _row(2, "Headline B", "", "The Verge", score=1.01),
]


@pytest.fixture
def traced(monkeypatch):
    """Run a call inside a request trace and return its result and spans."""
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)

    def run(fn):
        with request_trace(question="q", channel="eval") as trace:
            result = fn()
        return result, {span.kind: span for span in trace.spans}

    return run


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


def test_lookup_candidates_by_query_rerank_off_keeps_recall_order(traced) -> None:
    fake_conn = _FakeConn(ROWS)

    with (
        patch.object(retrieval_mod, "get_conn", lambda: fake_conn),
        patch.object(retrieval_mod, "put_conn", lambda _conn: None),
        patch.object(retrieval_mod, "_get_query_embedding", lambda _q: None),
    ):
        candidates, spans = traced(lambda: retrieval_mod.lookup_candidates_by_query(
            query="OpenAI",
            days=14,
            limit=2,
            rerank_mode="none",
        ))

    assert [item["url"] for item in candidates] == ["https://1.com", "https://2.com"]
    assert set(spans) == {"retrieval"}
    retrieval = spans["retrieval"]
    assert (retrieval.input["query"], retrieval.attributes["channels"], retrieval.attributes["pool_limit"]) == (
        "OpenAI", ["lexical", "exact"], 2)
    assert (retrieval.input["seen_after"], retrieval.input["excluded"]) == (None, 0)
    assert retrieval.output["candidates"][0] == {"article_id": 1, "score": 1.11, "text_score": 0.1,
                                                 "semantic_score": 0.2, "exact_score": 0.0, "match_score": 0.5}


def test_lookup_candidates_by_query_passes_filters_and_records_them(traced) -> None:
    seen_after = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)
    received = {}

    def fetch(cur, **kwargs):
        received.update(kwargs)
        return ROWS

    with (
        patch.object(retrieval_mod, "get_conn", lambda: _FakeConn(ROWS)),
        patch.object(retrieval_mod, "put_conn", lambda _conn: None),
        patch.object(retrieval_mod, "_get_query_embedding", lambda _q: None),
        patch.object(retrieval_mod, "fetch_hybrid_rows", fetch),
    ):
        _, spans = traced(lambda: retrieval_mod.lookup_candidates_by_query(
            query="OpenAI", limit=2, rerank_mode="none", seen_after=seen_after,
            exclude_url_keys={"a.com/1", "b.com/2"},
        ))

    assert (received["seen_after"], received["exclude_url_keys"]) == (seen_after, {"a.com/1", "b.com/2"})
    assert (spans["retrieval"].input["seen_after"], spans["retrieval"].input["excluded"]) == (
        "2026-04-01T08:00:00+00:00", 2)


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
        candidates = retrieval_mod.lookup_candidates_by_query(
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
    assert (candidates[0]["article"].published_at, candidates[0]["article"].first_seen_at) == (
        datetime(2026, 4, 10, 10, 0, 0), datetime(2026, 4, 10, 11, 0, 0))


def test_lookup_candidates_by_query_empty_query_returns_nothing(traced) -> None:
    candidates, spans = traced(lambda: retrieval_mod.lookup_candidates_by_query(query="   ", rerank_mode="llm_rerank"))
    assert (candidates, spans) == ([], {})


def test_lookup_candidates_by_query_rerank_on_uses_reranker_order(monkeypatch, traced) -> None:
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
        candidates, spans = traced(lambda: retrieval_mod.lookup_candidates_by_query(
            query="OpenAI",
            days=14,
            limit=2,
            rerank_mode="llm_rerank",
        ))

    assert candidates[0]["url"] == "https://2.com"
    assert candidates[1]["url"] == "https://1.com"
    rerank = spans["rerank"]
    assert (rerank.status, rerank.input, rerank.attributes["top_k"]) == ("ok", {"candidates": 2}, 2)
    assert rerank.output == {"ranked": [{"article_id": 2, "score": 0.95}, {"article_id": 1, "score": 0.12}]}


def test_lookup_candidates_by_query_rerank_failure_fallbacks_to_recall_order(monkeypatch, traced) -> None:
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
        candidates, spans = traced(lambda: retrieval_mod.lookup_candidates_by_query(
            query="OpenAI",
            days=14,
            limit=2,
            rerank_mode="llm_rerank",
        ))

    assert [item["url"] for item in candidates] == ["https://1.com", "https://2.com"]
    rerank = spans["rerank"]
    assert (rerank.status, rerank.error_code, rerank.warnings, rerank.output) == (
        "error", "rerank_timeout", ["rerank_fallback"], {})


@pytest.mark.parametrize("key, post, code", [
    ("", None, "embedding_no_api_key"),
    ("fake-key", requests.ConnectionError("down"), "embedding_connectionerror"),
])
def test_query_embedding_failure_skips_semantic_recall(monkeypatch, traced, key, post, code) -> None:
    monkeypatch.setenv("JINA_API_KEY", key)

    def _post(*_args, **_kwargs):
        raise post

    monkeypatch.setattr(embeddings_mod.requests, "post", _post)
    vector, spans = traced(lambda: embeddings_mod.get_query_embedding("OpenAI"))

    assert vector is None
    embedding = spans["embedding"]
    assert (embedding.status, embedding.error_code, embedding.warnings) == ("error", code, ["semantic_skipped"])
