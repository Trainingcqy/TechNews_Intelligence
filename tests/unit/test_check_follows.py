"""check_follows: the search of each follow, its window, and the result text and evidence."""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import pytest

from agent import trace_store
from agent.evidence import EvidenceRegistry
from agent.memory import Follow, check_window
from agent.retrieval.search import SearchResult
from agent.tools import follows
from agent.tools.follows import FollowCheck, FollowsResult, commit_check_follows, run_check_follows
from agent.tools.registry import CheckFollowsArgs, PreparedCall, TurnState
from agent.trace import request_trace, trace_span
from agent.types import Article, StoryHit, StoryStats

TODAY = date(2026, 11, 25)
PUBLISHED = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
ARTICLE = Article(article_id=30, url="https://example.com/a", title="Nvidia unveils Rubin", title_zh="",
                  summary="Nvidia announced Rubin.", topics=("chips",), kind="release", publisher="TechCrunch",
                  publisher_kind="media", lang="en", published_at=PUBLISHED, first_seen_at=PUBLISHED,
                  body_level="full", story_id=5)
HIT = StoryHit(lead=ARTICLE, stats=StoryStats(5, 1, 1, ("media",), PUBLISHED, PUBLISHED), related=(), match_score=None)


def _follow(query: str, checked: datetime | None = None) -> Follow:
    return Follow(follow_id=1, query=query, last_checked_at=checked)


@pytest.mark.parametrize(("checked", "days"), [
    (None, 14),
    (datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc), 1),
    (datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc), 7),
    (datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc), 30),
])
def test_check_window_runs_from_the_last_check_within_1_to_30_days(checked, days):
    assert check_window(_follow("x", checked), TODAY) == days


def test_run_searches_each_follow_in_english_or_in_its_own_words_under_the_tool_span(monkeypatch):
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    searched = {}

    def search_stories(*, query, days, **filters):
        with trace_span("retrieval") as span:
            searched[query] = (days, span.parent_id)
        return SearchResult(hits=[], weak_match=False)

    monkeypatch.setattr(follows, "search_stories", search_stories)
    state = TurnState(user_urls=set(), follows=[_follow("英伟达"), _follow("OpenAI")], today=TODAY)
    args = CheckFollowsArgs(queries=[{"follow": "英伟达", "query": "NVIDIA"}])
    with request_trace(question="q", channel="cli"):
        with trace_span("tool", "check_follows") as tool:
            result = run_check_follows(PreparedCall(0, "c1", "check_follows", args), registry=None, state=state)

    assert [(check.follow.query, check.query) for check in result.checks] == [("英伟达", "NVIDIA"), ("OpenAI", "OpenAI")]
    assert searched == {"NVIDIA": (14, tool.span_id), "OpenAI": (14, tool.span_id)}


def test_run_searches_every_follow_at_once(monkeypatch):
    many = [_follow(f"follow {i}") for i in range(4)]
    barrier = threading.Barrier(len(many), timeout=5)

    def search_stories(**kwargs):
        barrier.wait()
        return SearchResult(hits=[], weak_match=False)

    monkeypatch.setattr(follows, "search_stories", search_stories)
    call = PreparedCall(0, "c1", "check_follows", CheckFollowsArgs())
    result = run_check_follows(call, registry=None, state=TurnState(user_urls=set(), follows=many, today=TODAY))

    assert [check.error for check in result.checks] == [None] * len(many)
    assert run_check_follows(call, registry=None, state=TurnState(user_urls=set())).checks == []


def test_commit_groups_by_follow_and_registers_each_evidence_once():
    found = SearchResult(hits=[HIT], weak_match=False)
    result = FollowsResult([FollowCheck(_follow("Nvidia"), "Nvidia", 7, found, None),
                            FollowCheck(_follow("英伟达"), "NVIDIA", 7, found, None),
                            FollowCheck(_follow("OpenAI"), "OpenAI", 14, SearchResult(hits=[], weak_match=False), None)])
    registry = EvidenceRegistry()

    outcome = commit_check_follows(CheckFollowsArgs(), result, registry=registry)

    assert outcome.ok
    assert [e.eid for e in registry.items()] == ["E1"]
    lines = outcome.gather_text.splitlines()
    assert lines[0] == "check_follows results: 3 follows"
    assert 'Follow "英伟达", query="NVIDIA", last 7 days, by importance: 1 story' in lines
    assert 'Follow "OpenAI", query="OpenAI", last 14 days: no stories.' in lines


def test_commit_marks_a_lead_cited_in_a_past_answer_once_told_is_written():
    result = FollowsResult([FollowCheck(_follow("Nvidia"), "Nvidia", 7, SearchResult(hits=[HIT], weak_match=False),
                                        None)])
    registry = EvidenceRegistry()

    assert " | Coverage: 1 report, 1 publisher\n" in commit_check_follows(CheckFollowsArgs(), result,
                                                                        registry=registry).gather_text
    registry.told["example.com/a"] = date(2026, 11, 24)
    assert " | Coverage: 1 report, 1 publisher; cited in the answer on 2026-11-24\n" in commit_check_follows(
        CheckFollowsArgs(), result, registry=registry).gather_text


def test_commit_without_follows_or_with_every_search_failed():
    empty = commit_check_follows(CheckFollowsArgs(), FollowsResult([]), registry=EvidenceRegistry())
    assert (empty.ok, empty.gather_text) == (True, "check_follows: the user has no follows.")
    failed = commit_check_follows(CheckFollowsArgs(), FollowsResult([FollowCheck(_follow("x"), "x", 14, None, "Timeout")]),
                                  registry=EvidenceRegistry())
    assert (failed.ok, failed.error) == (False, failed.gather_text)
    assert failed.gather_text.endswith('Follow "x", query="x", last 14 days: search failed: Timeout.')
