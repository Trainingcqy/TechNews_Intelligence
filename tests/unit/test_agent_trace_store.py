"""Unit tests for writing request traces."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from agent import trace_store as store_mod
from agent.trace import RequestTrace, Span


class _FakeCursor:
    def __init__(self):
        self.execute_calls: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    def execute(self, sql: str, params: tuple = ()):
        self.execute_calls.append((sql, params))

    def executemany(self, sql: str, params_seq):
        self.executemany_calls.append((sql, list(params_seq)))


@pytest.fixture
def cursor(monkeypatch):
    cur = _FakeCursor()
    transactions: list[int] = []

    @contextmanager
    def _transaction():
        transactions.append(1)
        yield None, cur

    monkeypatch.setattr(store_mod, "Json", lambda value, dumps=None: value)
    monkeypatch.setattr(store_mod, "db_transaction", _transaction)
    cur.transactions = transactions
    return cur


def _trace(**fields) -> RequestTrace:
    return RequestTrace(request_id="r1", question="Genie 4 有什么新功能？", channel="web", thread_id="t1",
                        started_at_ms=1_700_000_000_000, **fields)


def test_insert_running_writes_the_request_row(cursor) -> None:
    store_mod.insert_running(_trace())

    ((sql, params),) = cursor.execute_calls
    assert "INSERT INTO public.agent_runs" in sql and "'running'" in sql and "DO NOTHING" in sql
    assert params == ("r1", "t1", "web", "Genie 4 有什么新功能？",
                      datetime.fromtimestamp(1_700_000_000, tz=timezone.utc))


def test_persist_writes_request_spans_and_model_io_in_one_transaction(cursor) -> None:
    llm = Span(kind="llm", name="gather", parent_id="s0", span_id="s1", started_at_ms=10, finished_at_ms=30,
               attributes={"model": "flash"}, warnings=["usage_inconsistent"])
    trace = _trace(finished_at_ms=1_700_000_002_500, status="answered", token_usage={"total_tokens": 3},
                   warnings=["evidence_truncated"], output={"route": {"decision": "synthesize"}},
                   spans=[llm], model_io={"s1": {"input_messages": [{"content": "prompt"}], "output": {"content": "x"}}})

    store_mod.persist(trace)

    assert cursor.transactions == [1]
    ((run_sql, run),) = cursor.execute_calls
    assert "ON CONFLICT (request_id) DO UPDATE" in run_sql
    assert run[:5] == ("r1", "t1", "web", "Genie 4 有什么新功能？", "answered")
    assert run[9:] == (2500, {"total_tokens": 3}, ["evidence_truncated", "usage_inconsistent"],
                       {"route": {"decision": "synthesize"}})
    (span_sql, spans), (io_sql, ios) = cursor.executemany_calls
    assert "INSERT INTO public.agent_trace_spans" in span_sql
    assert spans == [("r1", "s1", "s0", "llm", "gather", "ok", 10, 30, {}, {}, {"model": "flash"},
                      ["usage_inconsistent"], None, None, None)]
    assert "INSERT INTO public.agent_model_io" in io_sql
    assert ios == [("r1", "s1", [{"content": "prompt"}], {"content": "x"})]


def test_persist_without_spans_writes_only_the_request_row(cursor) -> None:
    store_mod.persist(_trace(finished_at_ms=1_700_000_000_100, status="direct"))

    ((_, run),) = cursor.execute_calls
    assert run[4] == "direct" and run[10] is None
    assert cursor.executemany_calls == []
