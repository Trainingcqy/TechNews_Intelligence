"""Chat orchestration shared by regular and SSE HTTP endpoints."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Callable

from fastapi import HTTPException

from agent import Answer, Progress, memory, run_turn
from agent.config import zone_named
from agent.types import Process
from app import streaming
from app.schemas import ChatRequest, ChatResponse, ProcessRecord, SourceItem
from app import quota_service
from app.conversations import (
    ConversationThreadNotFoundError,
    append_message_for_token,
    create_thread,
    delete_thread_for_token,
    last_turn_for_token,
    list_threads_for_token,
    load_history_for_token,
    replace_last_turn_for_token,
)

logger = logging.getLogger(__name__)

HISTORY_LOAD_LIMIT = 40
WEB_HISTORY_DISPLAY_LIMIT = 200
WEB_THREAD_LIST_LIMIT = 100


@dataclass(frozen=True)
class ChatTurn:
    body: ChatRequest
    token_info: dict
    request_id: str
    thread_id: str
    history: list[dict]
    user_key: str
    reservation: dict
    replace_ids: tuple[int, int] | None = None  # user and model message ids of the last turn this one replaces
    stop: threading.Event = field(default_factory=threading.Event)


# Streamed turns running in this process, by request_id: the owning token id and the turn's stop event.
_running_turns: dict[str, tuple[int, threading.Event]] = {}


def stop_turn(request_id: str, *, token_id: int) -> bool:
    """Set the stop event of a streamed turn the token owns; False when no such turn is running."""
    entry = _running_turns.get(request_id)
    if entry is None or entry[0] != token_id:
        return False
    entry[1].set()
    return True


def normalize_request_thread_id(thread_id: str | None) -> str | None:
    value = str(thread_id or "").strip()
    return value or None


def load_thread_history_or_404(
    thread_id: str, *, token_id: int, limit: int | None = None
) -> list[dict]:
    try:
        return load_history_for_token(
            thread_id,
            token_id,
            limit=limit if limit is not None else HISTORY_LOAD_LIMIT,
        )
    except ConversationThreadNotFoundError:
        raise HTTPException(status_code=404, detail="conversation_thread_not_found") from None


def list_threads(*, token_id: int) -> list[dict]:
    return list_threads_for_token(token_id, limit=WEB_THREAD_LIST_LIMIT)


def delete_thread_or_404(thread_id: str, *, token_id: int) -> None:
    try:
        delete_thread_for_token(thread_id, token_id)
    except ConversationThreadNotFoundError:
        raise HTTPException(status_code=404, detail="conversation_thread_not_found") from None


def last_turn_or_error(thread_id: str | None, *, token_id: int) -> tuple[int, int]:
    if thread_id is None:
        raise HTTPException(status_code=422, detail="replace_last_requires_thread")
    try:
        turn_ids = last_turn_for_token(thread_id, token_id)
    except ConversationThreadNotFoundError:
        raise HTTPException(status_code=404, detail="conversation_thread_not_found") from None
    if turn_ids is None:
        raise HTTPException(status_code=409, detail="last_turn_not_replaceable")
    return turn_ids


def thread_subject(message: str) -> str | None:
    return str(message or "").strip()[:80] or None


def create_thread_for_request(body: ChatRequest, token_info: dict) -> str:
    subject = thread_subject(body.message)
    metadata = {
        "channel": "web",
        "email": str(token_info.get("email", "") or ""),
        "token_id": token_info.get("id"),
    }
    row = create_thread(channel="web", subject=subject, metadata=metadata)
    thread_id = str(row.get("thread_id", "")).strip()
    if not thread_id:
        raise RuntimeError("conversation thread creation returned empty thread_id")
    return thread_id


def user_history_item(message: str) -> dict:
    return {"role": "user", "parts": [{"text": str(message or "")}]}


def model_history_item(answer: Answer) -> dict:
    item = {
        "role": "model",
        "parts": [{"text": answer.text}],
        "status": answer.status,
        "sources": [asdict(source) for source in answer.sources],
    }
    if answer.process is not None:
        item["process"] = _process_dict(answer.process)
    return item


def _process_dict(process: Process) -> dict:
    return {"summary": process.summary, "items": [asdict(item) for item in process.items]}


def persist_conversation_turn(
    *,
    thread_id: str,
    token_id: int,
    user_message: str,
    answer: Answer,
    request_id: str,
    replace_ids: tuple[int, int] | None = None,
) -> None:
    metadata = {"request_id": request_id}
    messages = [user_history_item(user_message), model_history_item(answer)]
    if replace_ids is not None:
        replace_last_turn_for_token(
            thread_id, token_id, replace_ids, messages, subject=thread_subject(user_message), metadata=metadata
        )
        return
    for message in messages:
        append_message_for_token(thread_id, token_id, message, metadata=metadata)


def prepare_chat_turn(body: ChatRequest, token_info: dict, request_id: str, *, stream: bool = False) -> ChatTurn:
    thread_id = normalize_request_thread_id(body.thread_id)
    token_id = int(token_info["id"])
    replace_ids = last_turn_or_error(thread_id, token_id=token_id) if body.replace_last else None
    if replace_ids is not None:
        # The model sees the history without the turn being replaced: the last two messages.
        history = load_thread_history_or_404(thread_id, token_id=token_id, limit=HISTORY_LOAD_LIMIT + 2)[:-2]
    else:
        history = load_thread_history_or_404(thread_id, token_id=token_id) if thread_id else []
    reservation = quota_service.reserve_quota_or_notify_403(token_info)

    if stream:
        logger.info(
            "[%s] 对话(流式): request_id=%s message=%s...",
            token_info["email"],
            request_id,
            body.message[:50],
        )
    else:
        logger.info(
            "[%s] 对话: request_id=%s message=%s...",
            token_info["email"],
            request_id,
            body.message[:50],
        )
    try:
        if thread_id is None:
            thread_id = create_thread_for_request(body, token_info)
    except Exception:
        quota_service.refund_reserved_quota(token_info)
        raise

    return ChatTurn(
        body=body,
        token_info=token_info,
        request_id=request_id,
        thread_id=thread_id,
        history=history,
        user_key="email:" + token_info["email"],
        reservation=reservation,
        replace_ids=replace_ids,
    )


def run_agent_turn(turn: ChatTurn, *, on_event: Callable[[Progress], None] | None = None) -> Answer:
    answer = run_turn(
        turn.body.message,
        turn.history,
        channel="web",
        thread_id=turn.thread_id,
        request_id=turn.request_id,
        user_key=turn.user_key,
        zone=zone_named(turn.body.timezone),
        on_event=on_event,
        stop=turn.stop,
    )
    # An error turn is not kept: its text would reach later turns as a model message. Telegram and the CLI
    # leave it out of their history too. A stopped turn has no answer to keep.
    if answer.status not in ("error", "stopped"):
        persist_conversation_turn(
            thread_id=turn.thread_id,
            token_id=int(turn.token_info["id"]),
            user_message=turn.body.message,
            answer=answer,
            request_id=turn.request_id,
            replace_ids=turn.replace_ids,
        )
    if answer.status == "answered":
        memory.record_turn(turn.user_key, answer)
    return answer


def chat_response(turn: ChatTurn, answer: Answer) -> ChatResponse:
    if answer.status == "answered":
        remaining = max(int(turn.reservation["remaining"]), 0)
        status = quota_service.status_from_reservation(turn.reservation)
    else:
        remaining = quota_service.remaining_after_refund(turn.reservation)
        status = "active"
    return ChatResponse(
        reply=answer.text,
        thread_id=turn.thread_id,
        kind=answer.status,
        sources=[SourceItem(**asdict(source)) for source in answer.sources],
        process=None if answer.process is None else ProcessRecord(**_process_dict(answer.process)),
        remaining=remaining,
        quota=int(turn.reservation["quota"]),
        unlimited=bool(turn.reservation.get("unlimited", False)),
        status=status,
    )


async def handle_chat_request(body: ChatRequest, token_info: dict, request_id: str) -> ChatResponse:
    turn = prepare_chat_turn(body, token_info, request_id)
    try:
        answer = await asyncio.to_thread(run_agent_turn, turn)
    except HTTPException:
        quota_service.refund_reserved_quota(token_info)
        raise
    except asyncio.CancelledError:
        quota_service.refund_reserved_quota(token_info)
        raise
    except Exception as e:
        quota_service.refund_reserved_quota(token_info)
        logger.error(
            "[%s] 处理失败: request_id=%s error=%s",
            token_info["email"],
            request_id,
            e,
        )
        raise HTTPException(status_code=500, detail="服务暂时不可用，请稍后重试")

    if answer.status != "answered":
        quota_service.refund_reserved_quota(token_info)
        if answer.status == "error":
            raise HTTPException(status_code=503, detail=answer.text)
    else:
        quota_service.maybe_send_quota_exhausted_notifications(token_info, turn.reservation)
    return chat_response(turn, answer)


async def stream_chat_events(turn: ChatTurn) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue = asyncio.Queue()
    done_event = asyncio.Event()
    result_holder: dict[str, Answer] = {}
    error_holder: dict[str, Exception] = {}
    completed = False
    worker_task: asyncio.Task | None = None

    def on_event(event: Progress) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, event)

    # The worker settles the quota itself: when the page stops the turn or goes away, this generator is
    # cancelled while the worker thread keeps running to the end of the turn.
    def worker() -> None:
        try:
            answer = run_agent_turn(turn, on_event=on_event)
        except Exception as exc:
            error_holder["exc"] = exc
            quota_service.refund_reserved_quota(turn.token_info)
            logger.error(
                "[%s] 处理失败: request_id=%s error=%s",
                turn.token_info["email"],
                turn.request_id,
                exc,
            )
        else:
            result_holder["answer"] = answer
            if answer.status == "answered":
                quota_service.maybe_send_quota_exhausted_notifications(turn.token_info, turn.reservation)
            else:
                quota_service.refund_reserved_quota(turn.token_info)
        finally:
            loop.call_soon_threadsafe(done_event.set)

    _running_turns[turn.request_id] = (int(turn.token_info["id"]), turn.stop)
    try:
        worker_task = asyncio.create_task(asyncio.to_thread(worker))
        yield streaming.sse_event("start", {"request_id": turn.request_id})

        while True:
            if done_event.is_set() and event_queue.empty():
                break
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            yield streaming.event_to_sse(event)

        await worker_task

        if "exc" in error_holder:
            yield streaming.sse_event("error", {"detail": "服务暂时不可用，请稍后重试", "status_code": 500})
            return

        # The final SSE waits for the worker, which persists the turn, so that a persistence
        # failure still ends the stream with an error.
        completed = True
        yield streaming.sse_event("final", chat_response(turn, result_holder["answer"]).model_dump())
    finally:
        _running_turns.pop(turn.request_id, None)
        # A page that goes away before the final event stops the turn, as the stop button does.
        if not completed:
            turn.stop.set()
        # Awaiting the task here would raise again inside a cancelled scope; the thread is not cancellable anyway.
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
