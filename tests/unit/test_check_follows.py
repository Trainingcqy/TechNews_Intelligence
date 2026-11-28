"""check_follows: the follows it reads, the search of each follow, the reports it leaves out, and its text and
evidence."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from agent import trace_store
from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry, render_for_synthesis
from agent.memory import Follow
from agent.retrieval.search import SearchResult
from agent.tools import follows
from agent.tools.follows import (
    FollowCheck,
    FollowsResult,
    commit_check_follows,
    follows_trace_output,
    run_check_follows,
)
from agent.tools.registry import CheckFollowsArgs, PreparedCall, TurnState, commit_call, prepare_call, run_call
from agent.trace import request_trace, trace_span
from agent.types import Article, StoryHit, StoryStats

PUBLISHED = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
ARTICLE = Article(article_id=30, url="https://example.com/a", title="Nvidia unveils Rubin", title_zh="",
                  summary="Nvidia announced Rubin.", topics=("chips",), kind="release", publisher="TechCrunch",
                  publisher_kind="media", lang="en", published_at=PUBLISHED, first_seen_at=PUBLISHED,
                  body_level="full", story_id=5)
HIT = StoryHit(lead=ARTICLE, stats=StoryStats(5, 1, 1, ("media",), PUBLISHED, PUBLISHED), related=(), match_score=None)
OTHER = replace(ARTICLE, article_id=31, url="https://example.com/b", title="Rubin ships", story_id=6)
OTHER_HIT = StoryHit(lead=OTHER, stats=StoryStats(6, 1, 1, ("media",), PUBLISHED, PUBLISHED), related=(),
                     match_score=None)
# 17:00 UTC is the next day in Beijing, the default zone.
CHECKED = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
NOTHING = SearchResult(hits=[], weak_match=False)
CALL = PreparedCall(0, "c1", "check_follows", CheckFollowsArgs())


def _follow(query: str, checked: datetime | None = None, search_query: str | None = None) -> Follow:
    return Follow(follow_id=1, query=query, last_checked_at=checked, search_query=search_query)


def _answer(day: int, *urls: str) -> list[dict]:
    created = datetime(2026, 11, day, 2, 0, tzinfo=timezone.utc)
    return [{"role": "user", "parts": [{"text": "q"}], "id": day * 2, "thread_id": "t", "created_at": created},
            {"role": "model", "parts": [{"text": "a"}], "id": day * 2 + 1, "thread_id": "t", "created_at": created,
             "sources": [{"n": n, "url": url} for n, url in enumerate(urls, 1)]}]


@pytest.fixture
def searched(monkeypatch):
    """The keyword arguments of each search, by query."""
    calls: dict[str, dict] = {}

    def search_stories(**kwargs):
        calls[kwargs["query"]] = {**kwargs, "exclude_url_keys": set(kwargs["exclude_url_keys"])}
        return NOTHING

    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    monkeypatch.setattr(follows, "search_stories", search_stories)
    return calls


def _lookup(monkeypatch, found: list[Follow]) -> list[tuple]:
    lookups: list[tuple] = []
    monkeypatch.setattr(follows.memory, "list_follows",
                        lambda user_key, limit: lookups.append((user_key, limit)) or found)
    return lookups


def _run(state: TurnState):
    with request_trace(question="q", channel="web"):
        with trace_span("tool", "check_follows") as tool:
            result = run_check_follows(CALL, registry=None, state=state)
    return result, tool


def test_each_follow_is_searched_with_its_search_query_since_its_last_check_without_the_reports_told(
        monkeypatch, searched):
    lookups = _lookup(monkeypatch, [_follow("谷歌的新动态", CHECKED, "Google"), _follow("具身智能", None, "embodied AI"),
                                    _follow("OpenAI"), _follow("Anthropic", None, "")])
    messages = [*_answer(20, "https://www.a.com/1/"), *_answer(25, "https://b.com/2")]

    result, tool = _run(TurnState(user_urls=set(), user_key="email:u", history_reader=lambda: messages))

    assert lookups == [("email:u", 20)]
    common = {"start_date": None, "end_date": None, "topic": None, "kinds": None, "publisher_kind": None,
              "sort": "importance", "limit": 5, "exclude_url_keys": {"a.com/1", "b.com/2"}}
    assert searched == {
        "Google": {**common, "query": "Google", "days": 30, "seen_after": CHECKED},
        "embodied AI": {**common, "query": "embodied AI", "days": 14, "seen_after": None},
        "OpenAI": {**common, "query": "OpenAI", "days": 14, "seen_after": None},
        "Anthropic": {**common, "query": "Anthropic", "days": 14, "seen_after": None},
    }
    assert [(check.follow.query, check.query, check.days) for check in result.checks] == [
        ("谷歌的新动态", "Google", 30), ("具身智能", "embodied AI", 14), ("OpenAI", "OpenAI", 14),
        ("Anthropic", "Anthropic", 14)]
    assert result.told == {"a.com/1": date(2026, 11, 20), "b.com/2": date(2026, 11, 25)}
    assert tool.warnings == []


def test_the_searches_run_under_the_tool_span(monkeypatch):
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    parents = []

    def search_stories(**kwargs):
        with trace_span("retrieval") as span:
            parents.append(span.parent_id)
        return NOTHING

    monkeypatch.setattr(follows, "search_stories", search_stories)
    _lookup(monkeypatch, [_follow("a"), _follow("b")])

    _, tool = _run(TurnState(user_urls=set(), user_key="email:u"))

    assert parents == [tool.span_id, tool.span_id]


def test_without_a_reader_nothing_is_left_out(monkeypatch, searched):
    _lookup(monkeypatch, [_follow("OpenAI")])

    result, tool = _run(TurnState(user_urls=set(), user_key="email:u"))

    assert searched["OpenAI"]["exclude_url_keys"] == set()
    assert (result.told, tool.warnings) == ({}, [])


def test_a_failing_reader_leaves_nothing_out_and_warns(monkeypatch, searched):
    def broken():
        raise RuntimeError("database unavailable")

    _lookup(monkeypatch, [_follow("OpenAI")])

    result, tool = _run(TurnState(user_urls=set(), user_key="email:u", history_reader=broken))

    assert searched["OpenAI"]["exclude_url_keys"] == set()
    assert result.told == {}
    assert [check.error for check in result.checks] == [None]
    assert tool.warnings == ["history_unavailable"]


def test_without_a_user_key_or_follows_nothing_is_read_or_searched(monkeypatch, searched):
    def reader():
        pytest.fail("the past answers are read without follows")

    monkeypatch.setattr(follows.memory, "list_follows", lambda *_a: pytest.fail("follows read without a user key"))
    assert _run(TurnState(user_urls=set(), history_reader=reader))[0] == FollowsResult([], {})
    _lookup(monkeypatch, [])
    assert _run(TurnState(user_urls=set(), user_key="email:u", history_reader=reader))[0] == FollowsResult([], {})
    assert searched == {}


def test_a_failing_follow_lookup_fails_the_call(monkeypatch):
    def broken(user_key, limit):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    monkeypatch.setattr(follows.memory, "list_follows", broken)
    state = TurnState(user_urls=set(), user_key="email:u")
    prepared = prepare_call(0, {"name": "check_follows", "args": {}, "id": "c"}, state=state, config=AgentConfig())
    with request_trace(question="q", channel="web"):
        payload = run_call(prepared, registry=EvidenceRegistry(), state=state)
    outcome = commit_call(payload, registry=EvidenceRegistry(), state=state, config=AgentConfig())

    assert (payload.ok, outcome.ok, outcome.gather_text) == (False, False, "Tool failed: RuntimeError")


def test_the_tool_takes_no_arguments():
    def prepared(args):
        return prepare_call(0, {"name": "check_follows", "args": args, "id": "c"}, state=TurnState(user_urls=set()),
                            config=AgentConfig())

    assert isinstance(prepared({}), PreparedCall)
    assert prepared({"queries": []}).gather_text == "Invalid arguments: queries: Extra inputs are not permitted"


def test_every_follow_is_searched_at_once(monkeypatch):
    many = [_follow(f"follow {i}") for i in range(4)]
    barrier = threading.Barrier(len(many), timeout=5)

    def search_stories(**kwargs):
        barrier.wait()
        return NOTHING

    monkeypatch.setattr(follows, "search_stories", search_stories)
    _lookup(monkeypatch, many)
    result = run_check_follows(CALL, registry=None, state=TurnState(user_urls=set(), user_key="email:u"))

    assert [check.error for check in result.checks] == [None] * len(many)


def test_trace_output_records_each_follow_and_the_reports_left_out():
    result = FollowsResult([FollowCheck(_follow("谷歌", CHECKED, "Google"), "Google", 30,
                                        SearchResult(hits=[HIT], weak_match=False), None),
                            FollowCheck(_follow("x"), "x", 14, None, "TimeoutError")],
                           {"a.com/1": date(2026, 11, 20), "b.com/2": date(2026, 11, 25)})

    assert follows_trace_output(result) == {
        "follows": [
            {"follow": "谷歌", "query": "Google", "since": "2026-11-24T17:00:00+00:00", "days": 30, "error": None,
             "stories": [5]},
            {"follow": "x", "query": "x", "since": None, "days": 14, "error": "TimeoutError", "stories": []},
        ],
        "excluded": 2,
    }


GOOGLE = FollowCheck(_follow("谷歌的新动态", CHECKED, "Google"), "Google", 30, SearchResult(hits=[HIT], weak_match=False),
                     None)
OPENAI = FollowCheck(_follow("OpenAI", datetime(2026, 11, 25, 17, 0, tzinfo=timezone.utc)), "OpenAI", 30, NOTHING,
                     None)
EMBODIED = FollowCheck(_follow("具身智能", None, "embodied AI"), "embodied AI", 14, NOTHING, None)
ANTHROPIC = FollowCheck(_follow("Anthropic", datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)), "Anthropic", 30, None,
                        "TimeoutError")


def test_commit_groups_by_follow_with_the_day_each_starts_from():
    registry = EvidenceRegistry()
    told = {"c.com/3": date(2026, 11, 20)}

    outcome = commit_check_follows(CheckFollowsArgs(), FollowsResult([GOOGLE, OPENAI, EMBODIED, ANTHROPIC], told),
                                   registry=registry)

    assert outcome.ok
    assert outcome.gather_text.splitlines() == [
        "check_follows results: 4 follows",
        'Follow "谷歌的新动态", since 2026-11-25, by importance: 1 story',
        "[E1] Nvidia unveils Rubin",
        "     TechCrunch · media · 2026-11-24 · chips · release | Coverage: 1 report, 1 publisher",
        "     Summary: Nvidia announced Rubin.",
        'Follow "OpenAI", since 2026-11-26: no new reports.',
        'Follow "具身智能", last 14 days: no new reports.',
        'Follow "Anthropic", since 2026-11-20: search failed: TimeoutError.',
    ]
    assert registry.told == told
    assert [(e.eid, e.level, e.stats) for e in registry.items()] == [("E1", "summary", HIT.stats)]
    assert registry.follows == {"E1": "谷歌的新动态, since 2026-11-25"}
    assert registry.follows_without_news == ["OpenAI (since 2026-11-26)", "具身智能 (last 14 days)"]


def test_commit_marks_a_lead_told_before():
    registry = EvidenceRegistry()

    text = commit_check_follows(CheckFollowsArgs(), FollowsResult([GOOGLE], {"example.com/a": date(2026, 11, 24)}),
                                registry=registry).gather_text

    assert " | Coverage: 1 report, 1 publisher; cited in the answer on 2026-11-24\n" in text


def test_commit_without_follows_or_with_every_search_failed():
    registry = EvidenceRegistry()
    empty = commit_check_follows(CheckFollowsArgs(), FollowsResult([], {}), registry=registry)
    assert (empty.ok, empty.gather_text) == (True, "check_follows: the user has no follows.")
    failed = commit_check_follows(CheckFollowsArgs(), FollowsResult([ANTHROPIC], {}), registry=registry)
    assert (failed.ok, failed.error) == (False, failed.gather_text)
    assert (registry.items(), registry.follows_without_news) == ([], [])


def test_the_brief_names_the_follow_of_each_lead_and_leads_with_the_follows_without_new_reports():
    registry = EvidenceRegistry()
    first_seen = FollowCheck(_follow("英伟达"), "NVIDIA", 14, SearchResult(hits=[OTHER_HIT, HIT], weak_match=False), None)
    commit_check_follows(CheckFollowsArgs(), FollowsResult([GOOGLE, OPENAI, first_seen, EMBODIED, ANTHROPIC],
                                                           {"example.com/a": date(2026, 11, 20)}), registry=registry)

    brief, left_out = render_for_synthesis(registry, 100_000)

    # A report two follows found keeps the follow that registered it first; a failed follow is not listed.
    assert left_out == []
    assert brief == (
        "Follows without new reports: OpenAI (since 2026-11-26), 具身智能 (last 14 days)\n"
        "\n"
        "[E1] Title: Nvidia unveils Rubin\n"
        "Publisher: TechCrunch, media | Date: 2026-11-24 | Topics: chips | Type: release\n"
        "Coverage: 1 report, 1 publisher, first 2026-11-24, latest 2026-11-24\n"
        "Follow: 谷歌的新动态, since 2026-11-25\n"
        "Cited in the answer on 2026-11-20\n"
        "Summary: Nvidia announced Rubin.\n"
        "\n"
        "[E2] Title: Rubin ships\n"
        "Publisher: TechCrunch, media | Date: 2026-11-24 | Topics: chips | Type: release\n"
        "Coverage: 1 report, 1 publisher, first 2026-11-24, latest 2026-11-24\n"
        "Follow: 英伟达, last 14 days\n"
        "Summary: Nvidia announced Rubin."
    )


def test_the_line_of_follows_without_new_reports_is_kept_outside_the_limit():
    registry = EvidenceRegistry()
    registry.add_article(OTHER, "summary", history=True)
    commit_check_follows(CheckFollowsArgs(), FollowsResult([GOOGLE, OPENAI], {}), registry=registry)
    head = "Follows without new reports: OpenAI (since 2026-11-26)\n\n"
    full, _ = render_for_synthesis(registry, 100_000)
    current = full[:full.index("\n\n[E1]")]

    assert current.startswith(head + "[E2] ")
    assert render_for_synthesis(registry, len(full) - len(head)) == (full, [])
    assert render_for_synthesis(registry, len(current) - len(head)) == (current, ["E1"])
