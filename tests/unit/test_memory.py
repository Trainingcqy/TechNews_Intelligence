"""User memory: follows and their cap, render_follows, record_turn, and failures treated as no memory."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest

from agent import memory, trace_store
from agent.memory import Follow, FollowLimitError
from agent.trace import request_trace
from agent.types import Answer

USER = "email:user@example.com"
CHECKED = datetime(2026, 11, 9, 17, 0, tzinfo=timezone.utc)  # 2026-11-10 in Beijing


class FakeCursor:
    def __init__(self, results=(), rowcount=0):
        self.results = list(results)
        self.rowcount = rowcount
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


@pytest.fixture
def db(monkeypatch):
    cur = FakeCursor()

    @contextmanager
    def _cursor(**_kwargs):
        yield None, cur

    monkeypatch.setattr(memory, "db_cursor", _cursor)
    monkeypatch.setattr(memory, "db_transaction", _cursor)
    return cur


@pytest.fixture
def broken_db(monkeypatch):
    @contextmanager
    def _cursor(**_kwargs):
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(memory, "db_cursor", _cursor)
    monkeypatch.setattr(memory, "db_transaction", _cursor)


def test_list_follows_returns_active_follows_of_user(db):
    db.results = [[(1, "Anthropic 上市", CHECKED), (2, "具身智能", None)]]

    follows = memory.list_follows(USER, 10)

    assert follows == [Follow(1, "Anthropic 上市", CHECKED), Follow(2, "具身智能", None)]
    ((sql, params),) = db.executed
    assert "WHERE user_key = %s AND active" in sql
    assert params == (USER, 10)


def test_add_follow_inserts_or_revives_stripped_query(db):
    db.results = [(19,), (4, "Anthropic 上市", None)]

    follow = memory.add_follow(USER, "  Anthropic 上市 ")

    assert follow == Follow(4, "Anthropic 上市", None)
    (count_sql, count_params), (insert_sql, insert_params) = db.executed
    assert "active AND LOWER(query) <> LOWER(%s)" in count_sql
    assert count_params == (USER, "Anthropic 上市")
    assert "ON CONFLICT (user_key, LOWER(query)) DO UPDATE SET active = TRUE" in insert_sql
    assert insert_params == (USER, "Anthropic 上市")


def test_add_follow_rejects_the_21st_follow(db):
    db.results = [(20,)]

    with pytest.raises(FollowLimitError):
        memory.add_follow(USER, "具身智能")

    assert len(db.executed) == 1


def test_add_follow_rejects_blank_query(db):
    with pytest.raises(ValueError):
        memory.add_follow(USER, "   ")

    assert db.executed == []


@pytest.mark.parametrize("rowcount, expected", [(1, True), (0, False)])
def test_remove_follow_deactivates_own_follow(db, rowcount, expected):
    db.rowcount = rowcount

    assert memory.remove_follow(USER, 7) is expected
    ((sql, params),) = db.executed
    assert sql == "UPDATE public.user_follow SET active = FALSE WHERE user_key = %s AND follow_id = %s AND active"
    assert params == (USER, 7)


def test_seen_story_dates_in_beijing_time(db):
    db.results = [[(5, CHECKED), (6, datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc))]]

    assert memory.seen_story_dates(USER, [5, 6, 7]) == {5: date(2026, 11, 10), 6: date(2026, 11, 10)}
    ((sql, params),) = db.executed
    assert "story_id = ANY(%s)" in sql
    assert params == (USER, [5, 6, 7])


def test_seen_story_dates_without_stories_does_not_query(db):
    assert memory.seen_story_dates(USER, []) == {}
    assert db.executed == []


def test_record_turn_writes_seen_stories_and_check_time(db):
    answer = Answer(status="answered", text="t", story_ids=(5, 9), followed_queries=("Anthropic 上市", "OpenAI"))

    memory.record_turn(USER, answer)

    (seen5, params5), (seen9, params9), (update_sql, update_params) = db.executed
    assert seen5 == seen9
    assert "ON CONFLICT (user_key, story_id) DO UPDATE SET last_seen_at = NOW()" in seen5
    assert (params5, params9) == ((USER, 5), (USER, 9))
    assert "SET last_checked_at = NOW() WHERE user_key = %s AND active AND LOWER(query) = ANY(%s)" in update_sql
    assert update_params == (USER, ["anthropic 上市", "openai"])


def test_record_turn_without_followed_queries_leaves_follows_alone(db):
    memory.record_turn(USER, Answer(status="answered", text="t", story_ids=(5,)))

    assert [sql.split()[0] for sql, _ in db.executed] == ["INSERT"]


def test_turn_lookups_count_failures_as_no_memory_but_list_follows_raises(broken_db, monkeypatch):
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    with request_trace(question="q", channel="web") as trace:
        assert memory.seen_story_dates(USER, [5]) == {}
    assert trace.warnings == ["seen_dates_unavailable"]
    memory.record_turn(USER, Answer(status="answered", text="t", story_ids=(5,), followed_queries=("x",)))
    # The follows page must show the failure; the turn catches it in prepare.
    with pytest.raises(RuntimeError):
        memory.list_follows(USER, 10)


def test_render_follows():
    follows = [Follow(1, "Anthropic 上市", CHECKED), Follow(2, "具身智能", None)]

    assert memory.render_follows(follows, date(2026, 11, 17)) == (
        "用户的关注项，不是事实来源：\n"
        "- Anthropic 上市，上次查看 2026-11-10，距今 7 天\n"
        "- 具身智能，尚未查看过\n"
        "只有用户问到关注项本身时，例如\"我关注的有什么新进展\"，才对每个关注项各发一次 search_news：query 填关注项原文，"
        "days 填距上次查看的天数，最少 1，尚未查看过的填 14，最多 30。其他问题不受关注项影响，照常按规则检索；"
        "\"最近发生了什么\"这类没有具体对象的问题，仍然不填 query，用 topic 和 days 浏览。"
    )
    assert memory.render_follows([], date(2026, 11, 17)) == ""
