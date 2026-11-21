"""Unit tests for persistent conversation service primitives."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from services import conversations as conv_mod


class _FakeCursor:
    def __init__(
        self,
        *,
        fetchone_values: list[tuple | None] | None = None,
        fetchall_value: list[tuple] | None = None,
    ):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchone_values = list(fetchone_values or [])
        self._fetchall_value = list(fetchall_value or [])

    def execute(self, sql: str, params: tuple = ()):
        self.calls.append((sql, params))

    def fetchone(self):
        if not self._fetchone_values:
            return None
        return self._fetchone_values.pop(0)

    def fetchall(self):
        return list(self._fetchall_value)


def _transaction_context(cursor: _FakeCursor):
    @contextmanager
    def _ctx():
        yield None, cursor

    return _ctx


def _cursor_context(cursor: _FakeCursor):
    @contextmanager
    def _ctx():
        yield None, cursor

    return _ctx


def test_create_thread_returns_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 4, 12, 12, 0, 0, tzinfo=timezone.utc)
    cursor = _FakeCursor(
        fetchone_values=[
            ("thread-001", "web", "weekly summary", {"tenant": "acme"}, now, now, None),
        ],
    )
    monkeypatch.setattr(conv_mod, "Json", lambda value: value)
    monkeypatch.setattr(conv_mod, "db_transaction", _transaction_context(cursor))

    out = conv_mod.create_thread(
        thread_id="thread-001",
        channel="web",
        subject="weekly summary",
        metadata={"tenant": "acme"},
    )

    assert out["thread_id"] == "thread-001"
    assert out["channel"] == "web"
    assert out["metadata"] == {"tenant": "acme"}
    assert out["message_count"] == 0
    assert "INSERT INTO public.conversation_threads" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("thread-001", "web", "weekly summary", {"tenant": "acme"})


def test_append_message_for_token_checks_thread_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    created_at = datetime(2026, 4, 12, 13, 0, 0, tzinfo=timezone.utc)
    payload = {"role": "model", "parts": [{"text": "ack"}]}
    cursor = _FakeCursor(
        fetchone_values=[
            (1,),
            (
                10,
                "thread-001",
                "model",
                [{"text": "ack"}],
                payload,
                {"source": "web"},
                created_at,
            ),
        ],
    )
    monkeypatch.setattr(conv_mod, "Json", lambda value: value)
    monkeypatch.setattr(conv_mod, "db_transaction", _transaction_context(cursor))

    out = conv_mod.append_message_for_token(
        "thread-001",
        7,
        payload,
        metadata={"source": "web"},
    )

    assert out["id"] == 10
    assert "channel = 'web'" in cursor.calls[0][0]
    assert "metadata->>'token_id' = %s" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("thread-001", "7")
    assert "INSERT INTO public.conversation_messages" in cursor.calls[1][0]
    assert "UPDATE public.conversation_threads" in cursor.calls[2][0]


def test_append_message_for_token_raises_when_thread_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetchone_values=[None])
    monkeypatch.setattr(conv_mod, "Json", lambda value: value)
    monkeypatch.setattr(conv_mod, "db_transaction", _transaction_context(cursor))

    with pytest.raises(conv_mod.ConversationThreadNotFoundError):
        conv_mod.append_message_for_token(
            "thread-001",
            99,
            {"role": "user", "parts": [{"text": "hello"}]},
        )


def test_load_history_for_token_checks_thread_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(
        fetchone_values=[(1,)],
        fetchall_value=[
            ({"role": "user", "parts": [{"text": "hello"}]}, "user", [{"text": "hello"}]),
            ({"role": "model", "parts": [{"text": "ack"}]}, "model", [{"text": "ack"}]),
        ],
    )
    monkeypatch.setattr(conv_mod, "db_cursor", _cursor_context(cursor))

    history = conv_mod.load_history_for_token("thread-001", 7)

    assert len(history) == 2
    assert "channel = 'web'" in cursor.calls[0][0]
    assert "metadata->>'token_id' = %s" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("thread-001", "7")
    assert "ORDER BY id ASC" in cursor.calls[1][0]
    assert cursor.calls[1][1] == ("thread-001",)


def test_load_history_for_token_raises_when_thread_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetchone_values=[None])
    monkeypatch.setattr(conv_mod, "db_cursor", _cursor_context(cursor))

    with pytest.raises(conv_mod.ConversationThreadNotFoundError):
        conv_mod.load_history_for_token("thread-001", 8)
