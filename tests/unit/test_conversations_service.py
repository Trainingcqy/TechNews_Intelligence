"""Unit tests for persistent conversation service primitives."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from app import conversations as conv_mod


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


def test_list_threads_for_token_lists_owned_threads_with_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetchall_value=[("thread-002", "Nvidia earnings"), ("thread-001", None)])
    monkeypatch.setattr(conv_mod, "db_cursor", _cursor_context(cursor))

    threads = conv_mod.list_threads_for_token(7, limit=100)

    assert threads == [
        {"thread_id": "thread-002", "subject": "Nvidia earnings"},
        {"thread_id": "thread-001", "subject": ""},
    ]
    sql, params = cursor.calls[0]
    assert "channel = 'web'" in sql
    assert "metadata->>'token_id' = %s" in sql
    assert "last_message_at IS NOT NULL" in sql
    assert "ORDER BY last_message_at DESC" in sql
    assert params == ("7", 100)


def test_load_messages_for_token_reads_the_latest_messages_of_every_web_thread_of_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = (datetime(2026, 11, 25, 6, minute, tzinfo=timezone.utc) for minute in (0, 1))
    model = {"role": "model", "parts": [{"text": "回答 [1]"}], "sources": [{"n": 1, "url": "https://a.com/1"}]}
    # Newest first, as the query returns them; a row without a payload falls back to its role and parts.
    cursor = _FakeCursor(fetchall_value=[
        (31, "thread-002", second, model, "model", [{"text": "回答 [1]"}]),
        (30, "thread-001", first, None, "user", [{"text": "问题"}]),
    ])
    monkeypatch.setattr(conv_mod, "db_cursor", _cursor_context(cursor))

    messages = conv_mod.load_messages_for_token(7, limit=400)

    assert messages == [
        {"role": "user", "parts": [{"text": "问题"}], "id": 30, "thread_id": "thread-001", "created_at": first},
        {**model, "id": 31, "thread_id": "thread-002", "created_at": second},
    ]
    ((sql, params),) = cursor.calls
    sql = " ".join(sql.split())
    assert "JOIN public.conversation_threads t ON t.thread_id = m.thread_id" in sql
    assert "WHERE t.channel = 'web' AND t.metadata->>'token_id' = %s" in sql
    assert sql.endswith("ORDER BY m.id DESC LIMIT %s")
    assert params == ("7", 400)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([(12, "model"), (11, "user")], (11, 12)),
        ([(12, "user"), (11, "model")], None),
        ([(11, "user")], None),
        ([], None),
    ],
)
def test_last_turn_for_token_returns_the_closing_pair(monkeypatch: pytest.MonkeyPatch, rows, expected) -> None:  # noqa: ANN001
    cursor = _FakeCursor(fetchone_values=[(1,)], fetchall_value=rows)
    monkeypatch.setattr(conv_mod, "db_cursor", _cursor_context(cursor))

    assert conv_mod.last_turn_for_token("thread-001", 7) == expected
    assert "metadata->>'token_id' = %s" in cursor.calls[0][0]
    assert cursor.calls[1][1] == ("thread-001",)


def test_replace_last_turn_for_token_swaps_the_pair_in_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = (datetime(2026, 11, 28, 12, 0, second, tzinfo=timezone.utc) for second in (1, 2))
    cursor = _FakeCursor(fetchone_values=[(1,), (first,), (second,)], fetchall_value=[(12,), (11,)])
    monkeypatch.setattr(conv_mod, "Json", lambda value: value)
    monkeypatch.setattr(conv_mod, "db_transaction", _transaction_context(cursor))
    messages = [{"role": "user", "parts": [{"text": "新问题"}]}, {"role": "model", "parts": [{"text": "新回答"}]}]

    conv_mod.replace_last_turn_for_token(
        "thread-001", 7, (11, 12), messages, subject="新问题", metadata={"request_id": "req-2"}
    )

    sql = [call[0] for call in cursor.calls]
    assert "FOR UPDATE" in sql[0] and cursor.calls[0][1] == ("thread-001", "7")
    assert "ORDER BY id DESC" in sql[1]
    assert "SET subject = %s" in sql[2] and "NOT EXISTS" in sql[2] and "id < %s" in sql[2]
    assert cursor.calls[2][1] == ("新问题", "thread-001", "thread-001", 11)
    assert "DELETE FROM public.conversation_messages" in sql[3] and cursor.calls[3][1] == (11, 12)
    assert [call[1][1] for call in cursor.calls[4:6]] == ["user", "model"]
    assert cursor.calls[4][1][4] == {"request_id": "req-2"}
    assert "SET last_message_at = %s" in sql[6] and cursor.calls[6][1] == (second, "thread-001")


def test_replace_last_turn_for_token_refuses_a_turn_that_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetchone_values=[(1,)], fetchall_value=[(13,), (12,)])
    monkeypatch.setattr(conv_mod, "Json", lambda value: value)
    monkeypatch.setattr(conv_mod, "db_transaction", _transaction_context(cursor))

    with pytest.raises(conv_mod.ConversationTurnChangedError):
        conv_mod.replace_last_turn_for_token("thread-001", 7, (11, 12), [{"role": "user", "parts": []}])

    assert len(cursor.calls) == 2
