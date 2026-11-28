"""chat_service turn handling: event-to-SSE mapping, quota by answer status, persisted message format."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from dataclasses import asdict

import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

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
STATUSES = ["answered", "insufficient", "direct", "not_found", "blocked", "error", "stopped"]


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


@pytest.mark.parametrize("status", ["answered", "insufficient", "error", "stopped"])
def test_stream_emits_events_then_final(env, status: str) -> None:  # noqa: ANN001
    env.events = [Progress("正在理解问题"), Progress("正在检索", "Genie 4")]
    env.answer = Answer(status=status, text="正文 [1]", sources=(SOURCE,), process=PROCESS)
    turn = chat_service.prepare_chat_turn(ChatRequest(message="问题"), TOKEN, "req-1", stream=True)

    async def _collect() -> list[str]:
        return [chunk async for chunk in chat_service.stream_chat_events(turn)]

    parsed = [_parse(chunk) for chunk in asyncio.run(_collect())]

    answered = status == "answered"
    assert [name for name, _ in parsed] == ["start", "progress", "progress", "final"]
    assert parsed[0][1] == {"request_id": "req-1"}
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
    assert len(env.appended) == (0 if status in ("error", "stopped") else 2)
    assert chat_service._running_turns == {}


def test_persisted_turn_uses_history_message_format(env) -> None:  # noqa: ANN001
    env.answer = Answer(status="answered", text="正文 [1]", sources=(SOURCE,), process=PROCESS)

    asyncio.run(chat_service.handle_chat_request(ChatRequest(message="问题", thread_id="thread-9"), TOKEN, "req-1"))

    assert env.loads == [("thread-9", 7, 40)]
    question, history, kwargs = env.run_turn_calls[0]
    stop = kwargs.pop("stop")
    assert isinstance(stop, threading.Event) and not stop.is_set()
    assert (question, history, kwargs) == (
        "问题", HISTORY, {"channel": "web", "thread_id": "thread-9", "request_id": "req-1",
                        "user_key": "email:user@example.com", "zone": DEFAULT_ZONE}
    )
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


def _wait_until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def _run_turn_until_stopped(question, history=None, *, on_event=None, stop=None, **kwargs):  # noqa: ANN001
    """Stands in for run_turn: blocks until the turn's stop event is set, as a turn waiting on a model would."""
    on_event(Progress("正在理解问题"))
    stopped = stop.wait(2)
    return Answer(status="stopped" if stopped else "answered", text="" if stopped else "正文")


def test_page_that_goes_away_stops_the_turn(env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(chat_service, "run_turn", _run_turn_until_stopped)
    turn = chat_service.prepare_chat_turn(ChatRequest(message="问题"), TOKEN, "req-1", stream=True)
    response = StreamingResponse(chat_service.stream_chat_events(turn), media_type="text/event-stream")
    first_body = asyncio.Event()

    async def send(message):  # noqa: ANN001
        if message.get("type") == "http.response.body" and message.get("body"):
            first_body.set()

    async def receive():
        await first_body.wait()
        return {"type": "http.disconnect"}

    # uvicorn reports ASGI 2.3 for HTTP, so Starlette listens for the disconnect while it streams.
    asyncio.run(response({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}, receive, send))

    assert turn.stop.is_set()
    _wait_until(lambda: env.refunds)
    assert (env.refunds, env.notices, env.appended, env.recorded) == (1, 0, [], [])
    assert chat_service._running_turns == {}


def test_stop_request_stops_only_the_owner_turn(env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(chat_service, "run_turn", _run_turn_until_stopped)
    turn = chat_service.prepare_chat_turn(ChatRequest(message="问题"), TOKEN, "req-1", stream=True)
    stops: list[bool] = []

    async def _collect() -> list[tuple[str, dict]]:
        parsed = []
        async for chunk in chat_service.stream_chat_events(turn):
            parsed.append(_parse(chunk))
            if parsed[-1][0] == "start":
                stops.append(chat_service.stop_turn("req-1", token_id=8))
                stops.append(chat_service.stop_turn("req-1", token_id=7))
        return parsed

    parsed = asyncio.run(_collect())

    assert stops == [False, True]
    assert parsed[-1][0] == "final" and parsed[-1][1]["kind"] == "stopped"
    assert (env.refunds, env.notices, env.appended, env.recorded) == (1, 0, [], [])
    assert chat_service.stop_turn("req-1", token_id=7) is False


def test_replace_last_runs_without_the_last_turn_and_replaces_it(env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    turns = [{"role": "user", "parts": [{"text": f"问{i}"}]} if i % 2 == 0 else {"role": "model", "parts": []}
             for i in range(4)]
    replaced: list[tuple] = []
    monkeypatch.setattr(chat_service, "last_turn_for_token", lambda thread_id, token_id: (11, 12))
    monkeypatch.setattr(chat_service, "load_history_for_token",
                        lambda thread_id, token_id, limit=None: env.loads.append(limit) or list(turns))
    monkeypatch.setattr(chat_service, "replace_last_turn_for_token",
                        lambda *args, **kwargs: replaced.append((args, kwargs)))
    env.answer = Answer(status="answered", text="新回答", sources=(SOURCE,))

    request = ChatRequest(message=" 改过的问题 ", thread_id="thread-9", replace_last=True)
    asyncio.run(chat_service.handle_chat_request(request, TOKEN, "req-1"))

    assert env.loads == [42]
    assert env.run_turn_calls[0][1] == turns[:2]
    assert env.appended == []
    (thread_id, token_id, turn_ids, messages), kwargs = replaced[0]
    assert (thread_id, token_id, turn_ids) == ("thread-9", 7, (11, 12))
    assert [message["role"] for message in messages] == ["user", "model"]
    assert kwargs == {"subject": "改过的问题", "metadata": {"request_id": "req-1"}}


def test_replace_last_needs_a_turn_to_replace(env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    reserved: list = []
    monkeypatch.setattr(chat_service.quota_service, "reserve_quota_or_notify_403", reserved.append)
    monkeypatch.setattr(chat_service, "last_turn_for_token", lambda thread_id, token_id: None)

    with pytest.raises(HTTPException) as no_thread:
        chat_service.prepare_chat_turn(ChatRequest(message="问题", replace_last=True), TOKEN, "req-1")
    with pytest.raises(HTTPException) as no_turn:
        chat_service.prepare_chat_turn(ChatRequest(message="问题", thread_id="t", replace_last=True), TOKEN, "req-1")

    assert (no_thread.value.status_code, no_thread.value.detail) == (422, "replace_last_requires_thread")
    assert (no_turn.value.status_code, no_turn.value.detail) == (409, "last_turn_not_replaceable")
    assert reserved == []
