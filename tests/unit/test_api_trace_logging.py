"""API trace request_id propagation tests."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import patch

import pytest

from agent import Answer, Progress


@pytest.fixture()
def api_mod(
    email_validator_stub,  # noqa: ANN001
    agent_dependency_stubs,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delitem(sys.modules, "app.api", raising=False)
    return importlib.import_module("app.api")


def _fake_request(ip: str = "127.0.0.1"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=ip))


def _stub_conversation_memory(api_mod, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    memory: dict[str, list[dict]] = {}

    def _create_thread(**_kwargs):  # noqa: ANN001
        memory["thread-trace"] = []
        return {"thread_id": "thread-trace"}

    def _load_history_for_token(thread_id, token_id, limit=None):  # noqa: ANN001
        del token_id
        del limit
        if thread_id not in memory:
            raise api_mod.chat_service.ConversationThreadNotFoundError(thread_id)
        return list(memory[thread_id])

    def _append_message_for_token(thread_id, token_id, message, metadata=None):  # noqa: ANN001
        del token_id
        del metadata
        memory.setdefault(thread_id, []).append(message)
        return {"thread_id": thread_id}

    monkeypatch.setattr(api_mod.chat_service, "create_thread", _create_thread)
    monkeypatch.setattr(api_mod.chat_service, "load_history_for_token", _load_history_for_token)
    monkeypatch.setattr(api_mod.chat_service, "append_message_for_token", _append_message_for_token)
    monkeypatch.setattr(api_mod.chat_service.memory, "record_turn", lambda *_a: None)


def test_chat_logs_and_forwards_trace_request_id(
    api_mod,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _fake_run_turn(_question, _history=None, *, channel, thread_id=None, request_id=None, user_key=None,
                       zone=None, on_event=None):
        del user_key, zone, on_event
        captured["request_id"] = str(request_id)
        captured["thread_id"] = str(thread_id)
        captured["channel"] = channel
        return Answer(status="answered", text="reply-ok")

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(api_mod.chat_service, "run_turn", _fake_run_turn)
    monkeypatch.setattr(api_mod.chat_service.asyncio, "to_thread", _fake_to_thread)
    _stub_conversation_memory(api_mod, monkeypatch)
    monkeypatch.setattr(api_mod.rate_limit, "check_rate_limit", lambda _ip: None)
    monkeypatch.setattr(
        api_mod.chat_service.quota_service,
        "reserve_quota_or_notify_403",
        lambda _token: {"remaining": 9, "quota": 10, "notified": False},
    )
    monkeypatch.setattr(api_mod.chat_service.quota_service, "maybe_send_quota_exhausted_notifications", lambda *_a, **_k: None)

    body = api_mod.ChatRequest(message="hello")
    token_info = {"id": 1, "email": "user@example.com"}

    with patch.object(api_mod.chat_service.logger, "info") as info_log:
        response = asyncio.run(api_mod.chat(body, _fake_request(), token_info))

    assert response.reply == "reply-ok"
    assert info_log.call_count >= 1
    # logger.info(fmt, email, request_id, message_preview)
    logged_request_id = str(info_log.call_args.args[2])
    assert logged_request_id
    assert captured["request_id"] == logged_request_id
    assert captured["thread_id"] == response.thread_id
    assert captured["channel"] == "web"


def test_chat_stream_logs_and_forwards_trace_request_id(
    api_mod,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _fake_run_turn(_question, _history=None, *, channel, thread_id=None, request_id=None, user_key=None,
                       zone=None, on_event=None):
        del user_key
        on_event(Progress("正在理解问题"))
        captured["request_id"] = str(request_id)
        captured["thread_id"] = str(thread_id)
        return Answer(status="answered", text="reply-stream")

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _collect_stream(response) -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunks.append(chunk.decode("utf-8"))
            else:
                chunks.append(str(chunk))
        return "".join(chunks)

    monkeypatch.setattr(api_mod.chat_service, "run_turn", _fake_run_turn)
    monkeypatch.setattr(api_mod.chat_service.asyncio, "to_thread", _fake_to_thread)
    _stub_conversation_memory(api_mod, monkeypatch)
    monkeypatch.setattr(api_mod.rate_limit, "check_rate_limit", lambda _ip: None)
    monkeypatch.setattr(
        api_mod.chat_service.quota_service,
        "reserve_quota_or_notify_403",
        lambda _token: {"remaining": 8, "quota": 10, "notified": False},
    )
    monkeypatch.setattr(api_mod.chat_service.quota_service, "maybe_send_quota_exhausted_notifications", lambda *_a, **_k: None)
    monkeypatch.setattr(api_mod.chat_service.quota_service, "refund_reserved_quota", lambda *_a, **_k: None)

    body = api_mod.ChatRequest(message="stream hello")
    token_info = {"id": 1, "email": "user@example.com"}

    with patch.object(api_mod.chat_service.logger, "info") as info_log:
        response = asyncio.run(api_mod.chat_stream(body, _fake_request(), token_info))

    payload = asyncio.run(_collect_stream(response))

    assert "event: final" in payload
    assert info_log.call_count >= 1
    logged_request_id = str(info_log.call_args.args[2])
    assert logged_request_id
    assert captured["request_id"] == logged_request_id
    assert captured["thread_id"] == "thread-trace"
