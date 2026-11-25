"""Tests for agent.retrieval.stories."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent.retrieval import literal, stories
from agent.retrieval.stories import pick_lead, stats_from_members
from agent.types import Article


def _at(day: int) -> datetime:
    return datetime(2026, 11, day, tzinfo=timezone.utc)


BASE = Article(
    article_id=1, url="https://example.com/1", title="t", title_zh="", summary="", topics=("ai",),
    kind="news", publisher="TechCrunch", publisher_kind="media", lang="en", published_at=_at(10),
    body_level="excerpt", hn_score=None, hn_comments=None, hn_sighting_id=None, story_id=7,
)


def test_stats_from_members_dedupes_publishers_keeps_role_order_and_takes_extremes():
    members = [
        replace(BASE, article_id=1, publisher="TechCrunch", publisher_kind="media", published_at=_at(10),
                hn_score=100, hn_comments=50, hn_sighting_id=11),
        replace(BASE, article_id=2, publisher="OpenAI", publisher_kind="official", published_at=_at(11)),
        replace(BASE, article_id=3, publisher="TechCrunch", publisher_kind="media", published_at=_at(13),
                hn_score=300, hn_comments=20, hn_sighting_id=12),
    ]
    stats = stats_from_members(7, members)
    assert stats.story_id == 7
    assert stats.report_count == 3
    assert stats.publisher_count == 2
    assert stats.publisher_kinds == ("media", "official")
    assert (stats.hn_score, stats.hn_comments, stats.hn_sighting_id) == (300, 50, 12)
    assert (stats.first_at, stats.last_at) == (_at(10), _at(13))


def test_stats_from_members_without_hn():
    stats = stats_from_members(7, [BASE])
    assert (stats.hn_score, stats.hn_comments, stats.hn_sighting_id) == (None, None, None)


@pytest.mark.parametrize(
    ("loser", "winner"),
    [
        (dict(publisher_kind="media", body_level="full", hn_score=900, published_at=_at(1)),
         dict(publisher_kind="official", body_level="none", hn_score=None, published_at=_at(9))),
        (dict(body_level="excerpt", hn_score=900, published_at=_at(1)),
         dict(body_level="full", hn_score=None, published_at=_at(9))),
        (dict(hn_score=None, published_at=_at(1)),
         dict(hn_score=0, published_at=_at(9))),
        (dict(hn_score=5, published_at=_at(1)),
         dict(hn_score=6, published_at=_at(9))),
        (dict(published_at=_at(9)),
         dict(published_at=_at(1))),
    ],
    ids=["official", "full_body", "hn_present", "hn_higher", "earlier"],
)
def test_pick_lead_orders_by_role_body_hn_then_time(loser, winner):
    lose = replace(BASE, article_id=1, **loser)
    win = replace(BASE, article_id=2, **winner)
    assert pick_lead([lose, win]).article_id == 2
    assert pick_lead([win, lose]).article_id == 2


class _CaptureCursor:
    def execute(self, sql: str, params: dict) -> None:
        self.sql, self.params = sql, params

    def fetchall(self) -> list:
        return []


def _browse(monkeypatch, **kwargs) -> tuple[str, dict]:
    monkeypatch.setattr(stories, "_get_hn_channel_id", lambda cur: None)
    cur = _CaptureCursor()
    stories.browse_story_ids(cur, start_at=_at(1), end_at=_at(1) + timedelta(days=14), topic=None, kinds=None,
                             publisher_kind=None, sort="importance", limit=8, **kwargs)
    return cur.sql, cur.params


def test_browse_without_about_keeps_every_story(monkeypatch):
    sql, params = _browse(monkeypatch)
    assert "~*" not in sql and "ILIKE" not in sql
    assert "WHERE publisher_count >= %(min_publishers)s" in sql
    assert params["min_publishers"] == 1


def test_browse_about_matches_titles_and_the_official_publisher_of_that_name(monkeypatch):
    sql, params = _browse(monkeypatch, about="Nvidia", min_publishers=2)
    assert "AND (a.title ~* %(exact_pattern)s OR (p.kind = 'official' AND p.name ~* %(exact_pattern)s))" in sql
    assert (params["exact_pattern"], params["min_publishers"]) == (literal.exact_pattern("Nvidia"), 2)

    sql, params = _browse(monkeypatch, about="英伟达")
    assert ("AND (a.title ILIKE ('%%' || %(about)s || '%%')"
            " OR (p.kind = 'official' AND p.name ILIKE ('%%' || %(about)s || '%%')))") in sql
    assert (params["about"], params["exact_pattern"]) == ("英伟达", None)
