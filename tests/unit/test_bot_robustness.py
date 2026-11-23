"""Unit tests for Telegram bot transport-layer robustness."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest
from cachetools import TTLCache


@pytest.fixture()
def bot_mod(
    agent_dependency_stubs,  # noqa: ANN001
    telegram_import_stubs,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delitem(sys.modules, "app.bot", raising=False)
    return importlib.import_module("app.bot")


@pytest.fixture(autouse=True)
def reset_bot_runtime_state(bot_mod):  # noqa: ANN001
    env = os.environ.copy()
    original = {
        "conversation_histories": bot_mod.conversation_histories,
        "chat_history_limits": bot_mod.chat_history_limits,
        "chat_request_log": bot_mod.chat_request_log,
    }
    bot_mod.chat_request_log.clear()
    yield
    os.environ.clear()
    os.environ.update(env)
    bot_mod.conversation_histories = original["conversation_histories"]
    bot_mod.chat_history_limits = original["chat_history_limits"]
    bot_mod.chat_request_log = original["chat_request_log"]
    bot_mod.chat_request_log.clear()
    bot_mod.conversation_histories.clear()
    bot_mod.chat_history_limits.clear()


class _FakeMessage:
    def __init__(self, fail_html: bool = False, fail_count: int = 0):
        self.fail_html = fail_html
        self.fail_count = fail_count
        self.calls: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.calls.append((text, kwargs))
        if self.fail_count > 0:
            self.fail_count -= 1
            raise RuntimeError("transient")
        if self.fail_html and kwargs.get("parse_mode") == "HTML":
            raise ValueError("bad html")
        return types.SimpleNamespace(message_id=len(self.calls))


class _ThinkingMessage:
    def __init__(self):
        self.edits: list[str] = []

    async def delete(self):
        return None

    async def edit_text(self, text: str):
        self.edits.append(text)
        return None


def _source(n: int, url: str, title: str, title_zh: str = ""):
    from agent.types import Source

    return Source(n=n, url=url, title=title, title_zh=title_zh, source="Pub", published_at="2026-11-12")


def _answer(status: str = "answered", text: str = "", sources=()):
    from agent.types import Answer

    return Answer(status=status, text=text, sources=tuple(sources))


def test_format_for_telegram_linkify_uses_title(bot_mod) -> None:  # noqa: ANN001
    html = bot_mod._format_for_telegram(  # pylint: disable=protected-access
        "- https://a.com/news",
        [_source(1, "https://a.com/news", "Title A", "中文标题A")],
    )
    assert '- <a href="https://a.com/news">中文标题A</a>' in html


def test_format_for_telegram_linkify_fallback_to_url_slug(bot_mod) -> None:  # noqa: ANN001
    text = "- https://a.com/path/to/my-post-name"
    html = bot_mod._format_for_telegram(text, [])  # pylint: disable=protected-access
    assert "my post name" in html


def test_format_for_telegram_citations_and_source_lines(bot_mod) -> None:  # noqa: ANN001
    sources = [
        _source(1, "https://a.com/openai", "OpenAI update", "中文A"),
        _source(2, "https://b.com/g?x=1&y=2", "Google <News>"),
    ]
    html = bot_mod._format_for_telegram(  # pylint: disable=protected-access
        "OpenAI [1], Google [2], other [3]", sources
    )
    body, _, tail = html.partition("\n\n")
    assert body == (
        'OpenAI <a href="https://a.com/openai">[1]</a>, '
        'Google <a href="https://b.com/g?x=1&amp;y=2">[2]</a>, other [3]'
    )
    assert tail.split("\n") == [
        "<b>来源</b>",
        '- [1] <a href="https://a.com/openai">中文A</a>',
        '- [2] <a href="https://b.com/g?x=1&amp;y=2">Google &lt;News&gt;</a>',
    ]


def test_format_for_telegram_without_sources_has_no_source_section(bot_mod) -> None:  # noqa: ANN001
    html = bot_mod._format_for_telegram("直接回答 [1]", [])  # pylint: disable=protected-access
    assert html == "直接回答 [1]"


def test_reply_text_with_retry_succeeds_after_transient_failures(bot_mod) -> None:  # noqa: ANN001
    os.environ["BOT_SEND_RETRY_ATTEMPTS"] = "3"
    os.environ["BOT_SEND_RETRY_BASE_SEC"] = "0.1"
    msg = _FakeMessage(fail_count=2)
    with patch.object(bot_mod.asyncio, "sleep", new=AsyncMock()):
        asyncio.run(bot_mod._reply_text_with_retry(msg, "hello"))  # pylint: disable=protected-access
    assert len(msg.calls) == 3


def test_reply_text_with_retry_raises_after_max_attempts(bot_mod) -> None:  # noqa: ANN001
    os.environ["BOT_SEND_RETRY_ATTEMPTS"] = "1"
    os.environ["BOT_SEND_RETRY_BASE_SEC"] = "0.1"
    msg = _FakeMessage(fail_count=5)
    with patch.object(bot_mod.asyncio, "sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError):
            asyncio.run(bot_mod._reply_text_with_retry(msg, "hello"))  # pylint: disable=protected-access
    assert len(msg.calls) == 2


def test_send_reply_uses_answer_and_html_fallback(bot_mod) -> None:  # noqa: ANN001
    os.environ["BOT_SEND_RETRY_ATTEMPTS"] = "0"
    msg = _FakeMessage(fail_html=True)
    answer = _answer(text="结论 [1]", sources=[_source(1, "https://a.com/n1", "A", "中文A")])

    with patch.object(bot_mod, "get_conn", side_effect=AssertionError("bot should not query DB when sending")):
        asyncio.run(bot_mod._send_reply(msg, answer))  # pylint: disable=protected-access

    assert len(msg.calls) == 2
    first_text, first_kwargs = msg.calls[0]
    assert first_kwargs.get("parse_mode") == "HTML"
    assert '<a href="https://a.com/n1">中文A</a>' in first_text

    second_text, second_kwargs = msg.calls[-1]
    assert second_kwargs.get("parse_mode") is None
    assert "<a" not in second_text
    assert "- [1] 中文A" in second_text


def test_rate_limit_blocks_with_retry_after(bot_mod) -> None:  # noqa: ANN001
    os.environ["BOT_RATE_LIMIT"] = "2"
    os.environ["BOT_RATE_WINDOW_SEC"] = "10"
    with patch.object(bot_mod.time, "time", return_value=1000.0):
        assert bot_mod._consume_chat_rate_token(1) == (True, 0)  # pylint: disable=protected-access
        assert bot_mod._consume_chat_rate_token(1) == (True, 0)  # pylint: disable=protected-access
        allowed, retry_after = bot_mod._consume_chat_rate_token(1)  # pylint: disable=protected-access

    assert not allowed
    assert retry_after >= 1


def test_session_cache_evicts_oldest_when_over_capacity(bot_mod) -> None:  # noqa: ANN001
    bot_mod.conversation_histories = TTLCache(maxsize=2, ttl=3600)
    bot_mod.chat_history_limits = TTLCache(maxsize=2, ttl=3600)

    bot_mod.conversation_histories[1] = []
    bot_mod.conversation_histories[2] = []
    bot_mod.conversation_histories[3] = []

    bot_mod.chat_history_limits[1] = 10
    bot_mod.chat_history_limits[2] = 10
    bot_mod.chat_history_limits[3] = 10

    assert len(bot_mod.conversation_histories) == 2
    assert 1 not in bot_mod.conversation_histories
    assert 1 not in bot_mod.chat_history_limits


def test_rate_log_cache_evicts_oldest_chat(bot_mod) -> None:  # noqa: ANN001
    os.environ["BOT_RATE_LIMIT"] = "10"
    os.environ["BOT_RATE_WINDOW_SEC"] = "60"
    bot_mod.chat_request_log = TTLCache(maxsize=2, ttl=3600)

    with patch.object(bot_mod.time, "time", return_value=1000.0):
        assert bot_mod._consume_chat_rate_token(1) == (True, 0)  # pylint: disable=protected-access
        assert bot_mod._consume_chat_rate_token(2) == (True, 0)  # pylint: disable=protected-access
        assert bot_mod._consume_chat_rate_token(3) == (True, 0)  # pylint: disable=protected-access

    assert len(bot_mod.chat_request_log) == 2
    assert 1 not in bot_mod.chat_request_log


def _handle(bot_mod, chat_id: int, text: str, fake_run_turn, thinking, send_reply=None):  # noqa: ANN001
    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=chat_id),
        message=types.SimpleNamespace(text=text),
    )

    async def _to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with (
        patch.object(bot_mod, "_consume_chat_rate_token", return_value=(True, 0)),
        patch.object(bot_mod, "_reply_text_with_retry", new=AsyncMock(return_value=thinking)),
        patch.object(bot_mod, "_send_reply", new=send_reply or AsyncMock()),
        patch.object(bot_mod.asyncio, "to_thread", new=AsyncMock(side_effect=_to_thread)),
        patch.object(bot_mod, "run_turn", side_effect=fake_run_turn),
        patch.object(bot_mod.memory, "record_turn") as record_turn,
    ):
        asyncio.run(bot_mod.handle_message(update, types.SimpleNamespace(bot=None)))
    return record_turn


def test_handle_message_error_answer_not_pollute_history(bot_mod) -> None:  # noqa: ANN001
    thinking = _ThinkingMessage()
    send_reply = AsyncMock()

    record_turn = _handle(
        bot_mod,
        999,
        "analyze AI trend in recent 10 days",
        lambda *_a, **_k: _answer(status="error", text="抱歉，当前模型服务暂时不可用。"),
        thinking,
        send_reply,
    )

    assert bot_mod.conversation_histories.get(999, []) == []
    assert thinking.edits == ["抱歉，当前模型服务暂时不可用。"]
    send_reply.assert_not_awaited()
    record_turn.assert_not_called()


def test_handle_message_appends_model_message_format_and_passes_history(bot_mod) -> None:  # noqa: ANN001
    chat_id = 1001
    calls: list[tuple[str, list[dict], dict]] = []
    sources = [_source(1, "https://a.com/n1", "A", "中文A")]
    answers = [
        _answer(text="第一轮 [1]", sources=sources),
        _answer(status="insufficient", text="第二轮"),
    ]

    def _fake_run_turn(question, history, **kwargs):  # noqa: ANN001
        calls.append((question, [dict(item) for item in history], kwargs))
        return answers[len(calls) - 1]

    send_reply = AsyncMock()
    first = _handle(bot_mod, chat_id, "问题一", _fake_run_turn, _ThinkingMessage(), send_reply)
    second = _handle(bot_mod, chat_id, "问题二", _fake_run_turn, _ThinkingMessage(), send_reply)

    first_model = {
        "role": "model",
        "parts": [{"text": "第一轮 [1]"}],
        "status": "answered",
        "sources": [
            {"n": 1, "url": "https://a.com/n1", "title": "A", "title_zh": "中文A", "source": "Pub", "published_at": "2026-11-12"}
        ],
    }
    assert calls[0][1] == []
    assert calls[1][1] == [{"role": "user", "parts": [{"text": "问题一"}]}, first_model]
    assert calls[0][2] == calls[1][2] == {"channel": "telegram", "user_key": "tg:1001"}

    history = bot_mod.conversation_histories[chat_id]
    assert [item["role"] for item in history] == ["user", "model", "user", "model"]
    assert history[3] == {"role": "model", "parts": [{"text": "第二轮"}], "status": "insufficient", "sources": []}
    assert [call.args[1] for call in send_reply.await_args_list] == answers
    first.assert_called_once_with("tg:1001", answers[0])
    second.assert_not_called()


def _command(bot_mod, handler, text: str):  # noqa: ANN001
    message = _FakeMessage()
    message.text = text
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=42), message=message)

    async def _to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch.object(bot_mod.asyncio, "to_thread", new=AsyncMock(side_effect=_to_thread)):
        asyncio.run(handler(update, types.SimpleNamespace(bot=None)))
    return [reply for reply, _ in message.calls]


def _follow(follow_id: int, query: str):
    from agent.memory import Follow

    return Follow(follow_id=follow_id, query=query, last_checked_at=None)


def test_follow_command_adds_follow_for_chat(bot_mod) -> None:  # noqa: ANN001
    with patch.object(bot_mod.memory, "add_follow", return_value=_follow(3, "Anthropic 上市")) as add:
        replies = _command(bot_mod, bot_mod.follow_cmd, "/follow  Anthropic 上市 ")

    add.assert_called_once_with("tg:42", "Anthropic 上市")
    assert replies == ["已关注：Anthropic 上市"]


def test_follow_command_without_query_and_over_limit(bot_mod) -> None:  # noqa: ANN001
    with patch.object(bot_mod.memory, "add_follow", side_effect=bot_mod.memory.FollowLimitError("full")) as add:
        assert _command(bot_mod, bot_mod.follow_cmd, "/follow") == ["用法：/follow <检索词>"]
        add.assert_not_called()
        assert _command(bot_mod, bot_mod.follow_cmd, "/follow 具身智能") == [
            "关注项已达上限 20 个，请先用 /unfollow 删除。"
        ]


def test_follows_command_lists_with_ordinals(bot_mod) -> None:  # noqa: ANN001
    follows = [_follow(7, "Anthropic 上市"), _follow(9, "具身智能")]
    with patch.object(bot_mod.memory, "list_follows", return_value=follows) as list_follows:
        replies = _command(bot_mod, bot_mod.follows_cmd, "/follows")

    list_follows.assert_called_once_with("tg:42", 20)
    assert replies == ["关注项：\n1. Anthropic 上市\n2. 具身智能"]
    with patch.object(bot_mod.memory, "list_follows", return_value=[]):
        assert _command(bot_mod, bot_mod.follows_cmd, "/follows") == ["还没有关注项。用 /follow <检索词> 添加。"]


def test_follow_list_failure_is_reported_not_shown_as_empty(bot_mod) -> None:  # noqa: ANN001
    with (
        patch.object(bot_mod.memory, "list_follows", side_effect=RuntimeError("database unavailable")),
        patch.object(bot_mod.memory, "remove_follow") as remove,
    ):
        assert _command(bot_mod, bot_mod.follows_cmd, "/follows") == ["读取关注项失败，请稍后重试。"]
        assert _command(bot_mod, bot_mod.unfollow_cmd, "/unfollow 1") == ["读取关注项失败，请稍后重试。"]
        remove.assert_not_called()


def test_unfollow_command_removes_by_ordinal(bot_mod) -> None:  # noqa: ANN001
    follows = [_follow(7, "Anthropic 上市"), _follow(9, "具身智能")]
    with (
        patch.object(bot_mod.memory, "list_follows", return_value=follows),
        patch.object(bot_mod.memory, "remove_follow", return_value=True) as remove,
    ):
        assert _command(bot_mod, bot_mod.unfollow_cmd, "/unfollow 2") == ["已取消关注：具身智能"]
        remove.assert_called_once_with("tg:42", 9)
        remove.reset_mock()
        for text in ("/unfollow 3", "/unfollow 0"):
            assert _command(bot_mod, bot_mod.unfollow_cmd, text) == [
                "序号不存在。\n关注项：\n1. Anthropic 上市\n2. 具身智能"
            ]
        assert _command(bot_mod, bot_mod.unfollow_cmd, "/unfollow x") == ["用法：/unfollow <序号>，序号见 /follows"]
        remove.assert_not_called()
