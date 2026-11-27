"""chat_service turn handling: event-to-SSE mapping, quota by answer status, persisted message format."""

from __future__ import annotations

import asyncio
import json
import types
from dataclasses import asdict

import pytest
from fastapi import HTTPException

from zoneinfo import ZoneInfo

from agent import Answer, Progress
from agent.config import DEFAULT_ZONE
from agent.types import Process, ProcessItem, Source
from app import streaming
from app.schemas import ChatRequest
from app import chat_service

SOURCE = Source(
    n=1, url="https://example.com/a", title="Title", title_zh="标题", source="Example", published_at="2026-11-12"
)
PROCESS = Process("检索 1 次", (ProcessItem("检索", "Genie 4", "最近 14 天内找到 1 条新闻"),))
PROCESS_JSON = {"summary": "检索 1 次",
                "items": [{"action": "检索", "subject": "Genie 4", "meta": "最近 14 天内找到 1 条新闻"}]}
TOKEN = {"id": 7, "email": "user@example.com"}
HISTORY = [{"role": "user", "parts": [{"text": "上一问"}]}]
STATUSES = ["answered", "insufficient", "direct", "not_found", "blocked", "error"]


def _parse(chunk: str) -> tuple[str, dict]:
    event_line, data_line = chunk.rstrip("\n").split("\n")
    return event_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: "))


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):
    state = types.SimpleNamespace(
        answer=None, events=[], refunds=0, notices=0, appended=[], loads=[], run_turn_calls=[], recorded=[]
    )

    def _run_turn(question, history=None, *, on_event=None, **kwargs):
        state.run_turn_calls.append((question, history, kwargs))
        for event in state.events:
            if on_event is not None:
                on_event(event)
        return state.answer

    def _load_history(thread_id, token_id, limit=None):
        state.loads.append((thread_id, token_id, limit))
        return list(HISTORY)

    def _append(thread_id, token_id, message, metadata=None):
        state.appended.append((thread_id, token_id, message, metadata))
        return {"id": len(state.appended)}

    def _record_turn(user_key, answer):
        state.recorded.append((user_key, answer, len(state.appended)))

    def _refund(_token):
        state.refunds += 1

    def _notify(_token, _reservation):
        state.notices += 1

    quota = chat_service.quota_service
    monkeypatch.setattr(chat_service, "run_turn", _run_turn)
    monkeypatch.setattr(chat_service, "load_history_for_token", _load_history)
    monkeypatch.setattr(chat_service, "append_message_for_token", _append)
    monkeypatch.setattr(chat_service, "create_thread", lambda **_k: {"thread_id": "thread-new"})
    monkeypatch.setattr(chat_service.memory, "record_turn", _record_turn)
    monkeypatch.setattr(
        quota, "reserve_quota_or_notify_403", lambda _t: {"remaining": 4, "quota": 10, "tier": 0, "unlimited": False}
    )
    monkeypatch.setattr(quota, "refund_reserved_quota", _refund)
    monkeypatch.setattr(quota, "maybe_send_quota_exhausted_notifications", _notify)
    return state


def test_event_to_sse_mapping() -> None:
    assert _parse(streaming.event_to_sse(Progress("正在检索", "Genie 4"))) == (
        "progress",
        {"title": "正在检索", "detail": "Genie 4"},
    )


@pytest.mark.parametrize("status", STATUSES)
def test_chat_counts_quota_only_for_answered(env, status: str) -> None:  # noqa: ANN001
    env.answer = Answer(status=status, text="正文 [1]", sources=(SOURCE,))
    request = ChatRequest(message="问题")

    if status == "error":
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(chat_service.handle_chat_request(request, TOKEN, "req-1"))
        assert (exc_info.value.status_code, exc_info.value.detail) == (503, "正文 [1]")
        assert (env.refunds, env.notices) == (1, 0)
        assert env.recorded == []
        return

    response = asyncio.run(chat_service.handle_chat_request(request, TOKEN, "req-1"))

    answered = status == "answered"
    assert (env.refunds, env.notices) == ((0, 1) if answered else (1, 0))
    assert response.kind == status
    assert response.remaining == (4 if answered else 5)
    # Recorded only for an answered turn, and after both messages are persisted.
    assert env.recorded == ([("email:user@example.com", env.answer, 2)] if answered else [])
    assert [item.model_dump() for item in response.sources] == [asdict(SOURCE)]


@pytest.mark.parametrize("status", ["answered", "insufficient", "error"])
def test_stream_emits_events_then_final(env, status: str) -> None:  # noqa: ANN001
    env.events = [Progress("正在理解问题"), Progress("正在检索", "Genie 4")]
    env.answer = Answer(status=status, text="正文 [1]", sources=(SOURCE,), process=PROCESS)
    turn = chat_service.prepare_chat_turn(ChatRequest(message="问题"), TOKEN, "req-1", stream=True)

    async def _collect() -> list[str]:
        return [chunk async for chunk in chat_service.stream_chat_events(turn)]

    parsed = [_parse(chunk) for chunk in asyncio.run(_collect())]

    answered = status == "answered"
    assert [name for name, _ in parsed] == ["progress", "progress", "final"]
    assert parsed[-1][1] == {
        "reply": "正文 [1]",
        "thread_id": "thread-new",
        "kind": status,
        "sources": [asdict(SOURCE)],
        "process": PROCESS_JSON,
        "remaining": 4 if answered else 5,
        "quota": 10,
        "unlimited": False,
        "status": "active",
    }
    assert (env.refunds, env.notices) == ((0, 1) if answered else (1, 0))
    assert len(env.appended) == (0 if status == "error" else 2)


def test_persisted_turn_uses_history_message_format(env) -> None:  # noqa: ANN001
    env.answer = Answer(status="answered", text="正文 [1]", sources=(SOURCE,), process=PROCESS)

    asyncio.run(chat_service.handle_chat_request(ChatRequest(message="问题", thread_id="thread-9"), TOKEN, "req-1"))

    assert env.loads == [("thread-9", 7, 40)]
    assert env.run_turn_calls == [
        ("问题", HISTORY, {"channel": "web", "thread_id": "thread-9", "request_id": "req-1",
                         "user_key": "email:user@example.com", "zone": DEFAULT_ZONE})
    ]
    metadata = {"request_id": "req-1"}
    assert env.appended == [
        ("thread-9", 7, {"role": "user", "parts": [{"text": "问题"}]}, metadata),
        (
            "thread-9",
            7,
            {
                "role": "model",
                "parts": [{"text": "正文 [1]"}],
                "status": "answered",
                "sources": [
                    {
                        "n": 1,
                        "url": "https://example.com/a",
                        "title": "Title",
                        "title_zh": "标题",
                        "source": "Example",
                        "published_at": "2026-11-12",
                    }
                ],
                "process": PROCESS_JSON,
            },
            metadata,
        ),
    ]
    # A direct reply ran no tool, so it has no process.
    assert "process" not in chat_service.model_history_item(Answer(status="direct", text="你好"))


def test_turn_runs_in_the_zone_the_request_names(env) -> None:  # noqa: ANN001
    env.answer = Answer(status="answered", text="正文 [1]", sources=(SOURCE,))

    request = ChatRequest(message="问题", timezone="America/New_York")
    asyncio.run(chat_service.handle_chat_request(request, TOKEN, "req-1"))

    assert env.run_turn_calls[0][2]["zone"] is ZoneInfo("America/New_York")
