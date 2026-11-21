"""Tests for HackerNews Algolia parsing, using a recorded front-page response (trimmed to 3 hits)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ingest.discover.hn import parse_hn

CONTENT = (Path(__file__).resolve().parents[1] / "fixtures" / "ingest" / "hn_front_page.json").read_bytes()


def test_link_story():
    story = parse_hn(CONTENT)[0]
    assert story.url == "https://openai.com/index/introducing-gpt-6-2-astra-sol/"
    assert story.title == "GPT‑6.2 Astra and Sol"
    assert story.external_id == "50535230"
    assert (story.score, story.comment_count) == (1283, 798)
    assert story.channel_time == datetime(2026, 11, 17, 17, 46, 43, tzinfo=UTC)
    assert story.published_at is None
    assert story.feed_text is None


def test_self_post_uses_item_page_and_story_text():
    post = parse_hn(CONTENT)[1]
    assert post.url == "https://news.ycombinator.com/item?id=50536468"
    assert post.external_id == "50536468"
    assert post.title.startswith("Launch HN: StarSling")
    assert post.feed_text.startswith("We’re Yonas and Daniel and we’re building StarSling")
    assert "<" not in post.feed_text
    assert (post.score, post.comment_count) == (81, 39)
    assert post.published_at is None


def test_story_text_of_a_link_post_is_not_used():
    show_hn = parse_hn(CONTENT)[2]
    assert show_hn.url == "https://github.com/Apakottur/unref"
    assert show_hn.feed_text is None
