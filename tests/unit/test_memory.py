"""User memory: follows and their cap, record_turn, failures treated as no memory, and past answers."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage

from agent import memory
from agent.config import using_zone
from agent.llm import ModelHandle
from agent.memory import Follow, FollowLimitError, FollowQueryError, PastAnswer
from agent.types import Answer
from common import model_settings, prompt_text

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
    db.results = [[(1, "Anthropic 上市", CHECKED, "Anthropic IPO"), (2, "具身智能", None, None)]]

    follows = memory.list_follows(USER, 10)

    assert follows == [Follow(1, "Anthropic 上市", CHECKED, "Anthropic IPO"), Follow(2, "具身智能", None, None)]
    ((sql, params),) = db.executed
    assert "SELECT follow_id, query, last_checked_at, search_query FROM public.user_follow" in sql
    assert "WHERE user_key = %s AND active" in sql
    assert params == (USER, 10)


class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return AIMessage(content=self.reply)


@pytest.fixture
def follow_model(monkeypatch):
    """Route the follow model to a fake client; the client records the calls it receives."""
    client = FakeClient()
    monkeypatch.setattr(memory, "build_role_model", lambda role: ModelHandle(
        role=role, provider="deepseek", model="m", client=client, temperature=0))
    return client


def test_add_follow_inserts_or_revives_stripped_query(db, follow_model):
    db.results = [(19,), (4, "Anthropic 上市", None, "Anthropic IPO")]
    follow_model.reply = "  Anthropic IPO \n"

    follow = memory.add_follow(USER, "  Anthropic 上市 ")

    assert follow == Follow(4, "Anthropic 上市", None, "Anthropic IPO")
    (count_sql, count_params), (insert_sql, insert_params) = db.executed
    assert "active AND LOWER(query) <> LOWER(%s)" in count_sql
    assert count_params == (USER, "Anthropic 上市")
    assert "ON CONFLICT (user_key, LOWER(query)) DO UPDATE SET active = TRUE, search_query = EXCLUDED.search_query" in insert_sql
    assert "INSERT INTO public.user_follow (user_key, query, search_query)" in insert_sql
    assert insert_sql.endswith("RETURNING follow_id, query, last_checked_at, search_query")
    assert insert_params == (USER, "Anthropic 上市", "Anthropic IPO")
    (messages,) = follow_model.calls
    assert [(type(message).__name__, message.content) for message in messages] == [
        ("SystemMessage", prompt_text.text("follow.system")), ("HumanMessage", "Anthropic 上市")]


def test_add_follow_uses_an_ascii_query_as_its_search_query_without_a_model_call(db, follow_model):
    db.results = [(0,), (4, "OpenAI GPT-5", None, "OpenAI GPT-5")]

    memory.add_follow(USER, " OpenAI GPT-5 ")

    assert db.executed[1][1] == (USER, "OpenAI GPT-5", "OpenAI GPT-5")
    assert follow_model.calls == []


@pytest.mark.parametrize("reply", ["", "  \n ", "谷歌", "Google 谷歌", "Google\nAlphabet", "Google\rAlphabet"])
def test_add_follow_fails_on_an_invalid_search_query_and_writes_nothing(db, follow_model, reply):
    follow_model.reply = reply

    with pytest.raises(FollowQueryError):
        memory.add_follow(USER, "谷歌的新动态")

    assert db.executed == []


def test_add_follow_fails_when_the_model_call_fails(db, follow_model):
    follow_model.error = TimeoutError("slow")

    with pytest.raises(FollowQueryError):
        memory.add_follow(USER, "谷歌的新动态")

    assert db.executed == []


def test_add_follow_fails_when_the_model_cannot_be_built(db, monkeypatch):
    def _build(role):
        raise memory.ModelCallError("model_build_keyerror", "no key")

    monkeypatch.setattr(memory, "build_role_model", _build)

    with pytest.raises(FollowQueryError):
        memory.add_follow(USER, "谷歌的新动态")

    assert db.executed == []


def test_add_follow_calls_the_model_before_opening_the_transaction(monkeypatch, follow_model):
    events = []

    @contextmanager
    def _transaction(**_kwargs):
        events.append("transaction")
        yield None, FakeCursor([(0,), (1, "谷歌", None, "Google")])

    follow_model.reply = "Google"
    monkeypatch.setattr(memory, "build_role_model", lambda role: events.append("model") or ModelHandle(
        role=role, provider="deepseek", model="m", client=follow_model, temperature=0))
    monkeypatch.setattr(memory, "db_transaction", _transaction)

    memory.add_follow(USER, "谷歌")

    assert events == ["model", "transaction"]


def test_follow_query_error_is_not_a_value_error():
    # The API maps ValueError to 422 for blank text; a failed generation must not reach that branch.
    assert not issubclass(FollowQueryError, ValueError)


def test_follow_model_settings():
    spec = model_settings.get("follow")
    assert (spec.provider, spec.model, spec.temperature) == ("deepseek", "deepseek-flash", 0)


def test_add_follow_rejects_the_21st_follow(db, follow_model):
    db.results = [(20,)]
    follow_model.reply = "Embodied AI"

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


def test_record_turn_updates_check_time_of_followed_queries(db):
    memory.record_turn(USER, Answer(status="answered", text="t", followed_queries=("Anthropic 上市", "OpenAI")))

    ((update_sql, update_params),) = db.executed
    assert ("UPDATE public.user_follow SET last_checked_at = NOW() "
            "WHERE user_key = %s AND active AND LOWER(query) = ANY(%s)") in update_sql
    assert update_params == (USER, ["anthropic 上市", "openai"])


def test_record_turn_without_followed_queries_does_not_open_a_transaction(monkeypatch):
    monkeypatch.setattr(memory, "db_transaction", lambda **_kwargs: pytest.fail("transaction opened"))

    memory.record_turn(USER, Answer(status="answered", text="t"))


def test_record_turn_failure_is_swallowed_but_list_follows_raises(broken_db):
    memory.record_turn(USER, Answer(status="answered", text="t", followed_queries=("x",)))
    # The follows page must show the failure; in a turn, check_follows fails with it.
    with pytest.raises(RuntimeError):
        memory.list_follows(USER, 10)


def _message(message_id, thread_id, role, parts, day=25, hour=2, sources=None):
    message = {"role": role, "parts": [{"text": text} for text in parts], "id": message_id, "thread_id": thread_id,
               "created_at": datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc)}
    if sources is not None:
        message["sources"] = sources
    return message


A1 = {"n": 1, "url": "https://www.a.com/1/", "title": "A1"}
B2 = {"n": 1, "url": "https://b.com/2", "title": "B2"}


def test_past_answers_pair_each_model_message_with_sources_with_the_last_user_message_of_its_thread():
    messages = [
        _message(1, "A", "user", ["q1"]),
        _message(2, "B", "user", ["qB"]),
        _message(3, "A", "model", ["a1 ", "[1]"], sources=[A1]),
        _message(4, "B", "model", ["aB"], sources=[B2]),
        _message(5, "A", "user", ["q2"]),
        _message(6, "A", "model", ["不客气"], sources=[]),
        _message(7, "A", "model", ["旧格式"]),
        _message(8, "C", "model", ["aC"], sources=[A1]),
        _message(9, "A", "model", ["a2"], sources=[B2]),
    ]

    assert memory.past_answers(messages) == [
        PastAnswer(date(2026, 11, 25), "q1", "a1 [1]", [A1]),
        PastAnswer(date(2026, 11, 25), "qB", "aB", [B2]),
        PastAnswer(date(2026, 11, 25), "", "aC", [A1]),
        PastAnswer(date(2026, 11, 25), "q2", "a2", [B2]),
    ]


def test_the_day_of_a_past_answer_is_in_the_user_zone():
    # 02:00 UTC on 11-25 is 11-25 in Beijing and 11-24 in New York.
    messages = [_message(1, "A", "user", ["q"]), _message(2, "A", "model", ["a"], sources=[A1])]

    assert memory.past_answers(messages)[0].day == date(2026, 11, 25)
    with using_zone(ZoneInfo("America/New_York")):
        assert memory.past_answers(messages)[0].day == date(2026, 11, 24)


def test_told_dates_give_each_source_the_day_of_the_latest_answer_citing_it():
    messages = [
        _message(1, "A", "user", ["q1"], day=20), _message(2, "A", "model", ["a1"], day=20, sources=[A1, B2]),
        _message(3, "B", "user", ["q2"], day=23),
        _message(4, "B", "model", ["a2"], day=23, sources=[{"n": 1, "url": "https://a.com/1"}]),
        _message(5, "B", "model", ["a3"], day=24, sources=[]),
    ]

    assert memory.told_dates(memory.past_answers(messages)) == {
        "a.com/1": date(2026, 11, 23), "b.com/2": date(2026, 11, 20)}
    assert memory.told_dates([]) == {}

