"""Tests for agent.retrieval.search: story grouping, browsing and time windows."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.config import using_zone
from agent.retrieval import search
from agent.types import Article
from common.taxonomy import EVENT_KINDS


def _at(day: int) -> datetime:
    return datetime(2026, 11, day, tzinfo=timezone.utc)


BASE = Article(
    article_id=0, url="", title="t", title_zh="", summary="", topics=("ai",), kind="news",
    publisher="TechCrunch", publisher_kind="media", lang="en", published_at=_at(10), body_level="full",
    story_id=0,
)


def _article(article_id: int, story_id: int, day: int = 10, **fields) -> Article:
    return replace(BASE, article_id=article_id, url=f"https://x.com/{article_id}", story_id=story_id,
                   published_at=_at(day), **fields)


def _candidate(article: Article, match_score: float = 0.8) -> dict:
    return {"article": article, "match_score": match_score}


class _Conn:
    def cursor(self):
        return self

    def close(self):
        pass


def _patch(monkeypatch, *, candidates=(), members=None, browse=()):
    calls = {}

    def lookup(query, **kwargs):
        calls["lookup"] = kwargs
        return list(candidates)

    def browse_story_ids(cur, **kwargs):
        calls["browse"] = kwargs
        return list(browse)

    def fetch_story_members(cur, story_ids):
        calls["members"] = list(story_ids)
        return {sid: group for sid, group in (members or {}).items() if sid in story_ids}

    monkeypatch.setattr(search, "lookup_candidates_by_query", lookup)
    monkeypatch.setattr(search, "browse_story_ids", browse_story_ids)
    monkeypatch.setattr(search, "fetch_story_members", fetch_story_members)
    monkeypatch.setattr(search, "get_conn", _Conn)
    monkeypatch.setattr(search, "put_conn", lambda conn: None)
    return calls


def _search(query, *, sort="relevance", limit=3, kinds=None):
    return search.search_stories(query=query, days=14, start_date=None, end_date=None, topic=None,
                                 kinds=kinds, publisher_kind=None, sort=sort, limit=limit)


def test_query_groups_by_story_in_first_seen_order_and_stops_at_limit(monkeypatch):
    a1, a2, a3, a4, a5 = (_article(1, 5), _article(2, 3), _article(3, 5), _article(4, 7), _article(5, 9))
    members = {5: [a3, a1], 3: [a2], 7: [a4], 9: [a5]}
    calls = _patch(monkeypatch, candidates=[_candidate(a) for a in (a1, a2, a3, a4, a5)], members=members)

    result = _search("OpenAI", limit=3)

    assert [hit.stats.story_id for hit in result.hits] == [5, 3, 7]
    assert result.hits[0].lead.article_id == 1
    assert [a.article_id for a in result.hits[0].related] == [3]
    assert calls["members"] == [5, 3, 7]
    assert calls["lookup"]["limit"] == 45
    assert "browse" not in calls


def test_query_with_time_sort_orders_stories_by_lead_time(monkeypatch):
    a1, a2, a3 = _article(1, 1, day=3), _article(2, 2, day=9), _article(3, 3, day=5)
    calls = _patch(monkeypatch, candidates=[_candidate(a) for a in (a1, a2, a3)],
                   members={1: [a1], 2: [a2], 3: [a3]})

    assert [hit.lead.article_id for hit in _search("x", sort="time").hits] == [2, 3, 1]
    assert [hit.lead.article_id for hit in _search("x", sort="relevance").hits] == [1, 2, 3]
    assert "browse" not in calls


def test_importance_query_puts_multi_source_stories_about_it_first_then_fills_by_relevance(monkeypatch):
    a1, a2, a3, a4 = _article(1, 1), _article(2, 2), _article(3, 3), _article(4, 4)
    official3 = _article(30, 3, publisher_kind="official")
    media5, official5 = _article(50, 5), _article(51, 5, publisher_kind="official")
    calls = _patch(monkeypatch, candidates=[_candidate(a, 0.1) for a in (a3, a1, a2, a4)],
                   members={1: [a1], 2: [a2], 3: [a3, official3], 4: [a4], 5: [media5, official5]}, browse=[5, 3])

    result = _search("Nvidia", sort="importance", limit=4)

    assert [hit.stats.story_id for hit in result.hits] == [5, 3, 1, 2]
    assert [hit.lead.article_id for hit in result.hits] == [51, 3, 1, 2]
    assert [hit.match_score for hit in result.hits] == [None, 0.1, 0.1, 0.1]
    assert result.weak_match is False
    browse = calls["browse"]
    assert (browse["about"], browse["min_publishers"], browse["kinds"], browse["sort"], browse["limit"]) == (
        "Nvidia", 2, EVENT_KINDS, "importance", 4)


def test_importance_query_without_multi_source_stories_is_the_relevance_result(monkeypatch):
    a1, a2, a3 = _article(1, 1), _article(2, 2), _article(3, 3)
    _patch(monkeypatch, candidates=[_candidate(a1, 0.2), _candidate(a2), _candidate(a3)],
           members={1: [a1], 2: [a2], 3: [a3]})

    importance = _search("x", sort="importance")

    assert importance == _search("x", sort="relevance")
    assert [hit.stats.story_id for hit in importance.hits] == [1, 2, 3]
    assert importance.weak_match is True


@pytest.mark.parametrize(("scores", "weak"), [((0.29, 0.9), True), ((0.30, 0.1), False)])
def test_weak_match_uses_the_first_candidate(monkeypatch, scores, weak):
    a1, a2 = _article(1, 1), _article(2, 2)
    candidates = [_candidate(a1, scores[0]), _candidate(a2, scores[1])]
    _patch(monkeypatch, candidates=candidates, members={1: [a1], 2: [a2]})

    result = _search("x")

    assert result.weak_match is weak
    assert result.hits[0].match_score == scores[0]


def test_no_query_browses_event_kinds_by_default_and_picks_lead(monkeypatch):
    media, official = _article(1, 4, day=1), _article(2, 4, day=2, publisher_kind="official")
    calls = _patch(monkeypatch, members={4: [media, official]}, browse=[4])

    result = _search(None, sort="importance")

    assert "lookup" not in calls
    assert calls["browse"]["kinds"] == EVENT_KINDS
    assert calls["browse"]["sort"] == "importance"
    assert (calls["browse"]["about"], calls["browse"]["min_publishers"]) == (None, 1)
    assert result.hits[0].lead.article_id == 2
    assert result.hits[0].match_score is None
    assert result.weak_match is False

    _search(None, sort="importance", kinds=("opinion",))
    assert calls["browse"]["kinds"] == ("opinion",)


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("start_date", "end_date", "expected"),
    [
        (None, None, (NOW - timedelta(days=7), NOW)),
        (date(2026, 10, 1), None, (datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc), NOW)),
        (date(2026, 10, 1), date(2026, 10, 31),
         (datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc), datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc))),
    ],
    ids=["days_only", "start_only", "start_and_end"],
)
def test_time_window(start_date, end_date, expected):
    assert search.time_window(7, start_date, end_date, now=NOW) == expected


def test_time_window_dates_are_in_the_user_zone():
    with using_zone(ZoneInfo("America/New_York")):
        assert search.time_window(7, date(2026, 11, 24), date(2026, 11, 24), now=NOW) == (
            datetime(2026, 11, 24, 5, 0, tzinfo=timezone.utc), datetime(2026, 11, 25, 5, 0, tzinfo=timezone.utc))


def test_related_over_the_limit_keeps_other_roles_first():
    lead = _article(1, 9)
    media = [_article(10 + day, 9, day=day) for day in range(1, 8)]
    official = _article(99, 9, day=20, publisher_kind="official")
    related = search._related(lead, [lead, *media, official])
    assert [a.article_id for a in related] == [11, 12, 13, 14, 15, 99]
