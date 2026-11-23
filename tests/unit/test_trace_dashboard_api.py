"""Unit tests for the Trace Console API."""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

STARTED = datetime(2026, 11, 22, 8, 30, tzinfo=timezone.utc)


@pytest.fixture()
def trace_api_mod(agent_dependency_stubs, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001
    monkeypatch.setenv("TRACE_DASHBOARD_TOKEN", "unit-secret")
    monkeypatch.delitem(sys.modules, "app.trace_api", raising=False)
    return importlib.import_module("app.trace_api")


class _FakeCursor:
    def __init__(self, handler):  # noqa: ANN001
        self.handler = handler
        self.calls: list[tuple[str, tuple]] = []
        self.description = []
        self.rows = []

    def execute(self, sql, params=()):  # noqa: ANN001
        self.calls.append((" ".join(str(sql).split()), tuple(params or ())))
        columns, rows = self.handler(self.calls[-1][0], self.calls[-1][1])
        self.description = [(column,) for column in columns]
        self.rows = rows

    def fetchall(self):  # noqa: ANN001
        return self.rows


def _install_db(monkeypatch: pytest.MonkeyPatch, trace_api_mod, handler) -> _FakeCursor:  # noqa: ANN001
    cursor = _FakeCursor(handler)

    @contextmanager
    def _ctx():
        yield None, cursor

    monkeypatch.setattr(trace_api_mod, "db_cursor", _ctx)
    return cursor


def _get(trace_api_mod, path: str, token: str = "unit-secret"):  # noqa: ANN001
    with TestClient(trace_api_mod.app) as client:
        return client.get(path, headers={"Authorization": f"Bearer {token}"})


def test_trace_api_rejects_missing_or_wrong_token(trace_api_mod) -> None:  # noqa: ANN001
    with TestClient(trace_api_mod.app) as client:
        assert client.get("/trace-api/runs").status_code == 401
    assert _get(trace_api_mod, "/trace-api/runs", token="wrong").status_code == 401


def test_list_runs_hides_eval_runs_and_returns_compact_rows(trace_api_mod, monkeypatch) -> None:  # noqa: ANN001
    def _handler(sql: str, params: tuple):
        if "COUNT(*)" in sql:
            return ["total"], [(1,)]
        return (["request_id", "thread_id", "channel", "user_message", "status", "started_at", "latency_ms",
                 "total_tokens", "warnings", "error_code"],
                [("r1", "t1", "web", "Genie 4 有什么新功能？", "answered", STARTED, 48000, 9, ["low_relevance"], None)])

    cursor = _install_db(monkeypatch, trace_api_mod, _handler)
    response = _get(trace_api_mod, "/trace-api/runs?status=answered&q=Genie")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"] == [{"request_id": "r1", "thread_id": "t1", "channel": "web",
                                 "user_message": "Genie 4 有什么新功能？", "status": "answered",
                                 "started_at": "2026-11-22T08:30:00+00:00", "latency_ms": 48000, "total_tokens": 9,
                                 "warnings": ["low_relevance"], "error_code": None}]
    count_sql, count_params = cursor.calls[0]
    assert "status = %s AND channel <> 'eval' AND (user_message ILIKE %s" in count_sql
    assert count_params == ("answered", "%Genie%", "%Genie%", "%Genie%", "%Genie%")
    assert cursor.calls[1][1][-2:] == (50, 0)


@pytest.mark.parametrize("query, where", [
    ("thread_id=t1", "WHERE thread_id = %s"),
    ("channel=eval", "WHERE channel = %s"),
])
def test_list_runs_by_thread_or_channel_includes_eval_runs(trace_api_mod, monkeypatch, query, where) -> None:  # noqa: ANN001
    cursor = _install_db(monkeypatch, trace_api_mod,
                         lambda sql, params: (["total"], [(0,)]) if "COUNT(*)" in sql else (["request_id"], []))
    assert _get(trace_api_mod, f"/trace-api/runs?{query}").status_code == 200
    assert cursor.calls[0][0].endswith(where)


def test_get_run_returns_spans_and_resolves_referenced_materials(trace_api_mod, monkeypatch) -> None:  # noqa: ANN001
    output = {"evidence": [{"eid": "E1", "doc": "article", "article_id": 30},
                           {"eid": "E2", "doc": "discussion", "sighting_id": 70, "article_id": 30}]}
    spans = [
        ("s1", None, "step", None, "ok", 1, 9, {}, {"calls": []}, {"step": 1}, [], None, None, None),
        ("s2", "s1", "tool", "search_news", "ok", 2, 8, {"query": "Genie 4"},
         {"stories": [{"story_id": 5, "lead": 30, "related": [31], "discussion": 70}]}, {}, [], None, None, None),
        ("s3", "s2", "retrieval", None, "ok", 3, 4, {}, {"candidates": [{"article_id": 32, "score": 0.1}]}, {},
         [], None, None, None),
    ]

    def _handler(sql: str, params: tuple):
        if "FROM public.agent_runs" in sql:
            return (["request_id", "status", "started_at", "output"], [("r1", "answered", STARTED, output)])
        if "FROM public.agent_trace_spans" in sql:
            return (["span_id", "parent_span_id", "kind", "name", "status", "started_at_ms", "finished_at_ms",
                     "input", "output", "attributes", "warnings", "error_code", "error_message", "exception"], spans)
        if "FROM news.article" in sql:
            assert params == ([30, 31, 32],)
            return (["article_id", "title", "title_zh", "publisher", "url", "event_at"],
                    [(30, "Genie 4 adds audio", "", "TechCrunch", "https://techcrunch.com/genie-4", STARTED)])
        assert "FROM news.discussion" in sql and params == ([70],)
        return (["sighting_id", "article_id", "url", "title", "score", "comment_total"],
                [(70, 30, "https://news.ycombinator.com/item?id=1", "Genie 4 adds audio", 955, 312)])

    _install_db(monkeypatch, trace_api_mod, _handler)
    response = _get(trace_api_mod, "/trace-api/runs/r1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["started_at"] == "2026-11-22T08:30:00+00:00"
    assert [(span["span_id"], span["parent_span_id"], span["kind"]) for span in payload["spans"]] == [
        ("s1", None, "step"), ("s2", "s1", "tool"), ("s3", "s2", "retrieval")]
    assert payload["articles"] == {"30": {"title": "Genie 4 adds audio", "title_zh": "", "publisher": "TechCrunch",
                                          "url": "https://techcrunch.com/genie-4",
                                          "event_at": "2026-11-22T08:30:00+00:00"}}
    assert payload["discussions"]["70"]["comment_total"] == 312


def test_get_run_not_found(trace_api_mod, monkeypatch) -> None:  # noqa: ANN001
    _install_db(monkeypatch, trace_api_mod, lambda sql, params: (["request_id"], []))
    response = _get(trace_api_mod, "/trace-api/runs/missing")
    assert (response.status_code, response.json()["detail"]) == (404, "trace_run_not_found")


def test_model_io_endpoint_returns_the_full_payload(trace_api_mod, monkeypatch) -> None:  # noqa: ANN001
    def _handler(sql: str, params: tuple):
        assert "FROM public.agent_model_io WHERE request_id = %s AND span_id = %s" in sql
        assert params == ("r1", "s4")
        return ["input_messages", "output"], [([{"class": "HumanMessage", "content": "full prompt"}],
                                               {"content": "full output"})]

    _install_db(monkeypatch, trace_api_mod, _handler)
    response = _get(trace_api_mod, "/trace-api/runs/r1/model-io/s4")

    assert response.status_code == 200
    assert response.json()["model_io"] == {"input_messages": [{"class": "HumanMessage", "content": "full prompt"}],
                                           "output": {"content": "full output"}}


def test_model_io_endpoint_not_found(trace_api_mod, monkeypatch) -> None:  # noqa: ANN001
    _install_db(monkeypatch, trace_api_mod, lambda sql, params: (["input_messages", "output"], []))
    response = _get(trace_api_mod, "/trace-api/runs/r1/model-io/s2")
    assert (response.status_code, response.json()["detail"]) == (404, "model_io_not_found")
