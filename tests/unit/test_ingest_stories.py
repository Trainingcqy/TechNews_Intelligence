"""Tests for story assignment, with the store calls replaced by an in-memory fake."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from ingest import stories
from ingest.store import NearestStory

NEW_STORY = 900


def _fake_store(monkeypatch, *, existing=None, nearest=None):
    calls = {"members": [], "ready": [], "nearest": 0}

    def nearest_story(cur, article_id, *, window_days):
        calls["nearest"] += 1
        return nearest

    fake = SimpleNamespace(
        NearestStory=NearestStory,
        story_of=lambda cur, article_id: existing,
        nearest_story=nearest_story,
        next_story_id=lambda cur: NEW_STORY,
        record_story_member=lambda cur, *args: calls["members"].append(args),
        mark_ready=lambda cur, article_id: calls["ready"].append(article_id),
    )
    monkeypatch.setattr(stories, "store", fake)
    return calls


@pytest.mark.parametrize(
    ("similarity", "span", "story_id"),
    [
        (0.85, timedelta(days=1), 7),
        (0.8499, timedelta(days=1), NEW_STORY),
        (0.95, timedelta(days=7), 7),
        (0.95, timedelta(days=7, seconds=1), NEW_STORY),
    ],
)
def test_assign_story_joins_or_opens_and_records_nearest(monkeypatch, similarity, span, story_id):
    calls = _fake_store(monkeypatch, nearest=NearestStory(article_id=3, story_id=7, similarity=similarity, span_after=span))
    assert stories.assign_story(None, 10) == story_id
    assert calls["members"] == [(10, story_id, 3, similarity)]
    assert calls["ready"] == [10]


def test_assign_story_without_candidates_opens_a_story(monkeypatch):
    calls = _fake_store(monkeypatch, nearest=None)
    assert stories.assign_story(None, 10) == NEW_STORY
    assert calls["members"] == [(10, NEW_STORY, None, None)]


def test_assign_story_keeps_an_existing_member(monkeypatch):
    calls = _fake_store(monkeypatch, existing=5, nearest=NearestStory(3, 7, 0.99, timedelta(0)))
    assert stories.assign_story(None, 10) == 5
    assert calls == {"members": [], "ready": [10], "nearest": 0}
