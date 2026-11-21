"""Tests for the legacy backfill's pure mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ingest.migrate import (
    LegacyBody,
    failed_articles,
    hn_item_id,
    initial_status,
    legacy_body,
    news_articles,
    news_sightings,
    split_title_cn,
)
from ingest.store import LegacyFailed, LegacyNews

_CREATED = datetime(2026, 5, 1, 8, 0)
_LANGS = {"hn_frontpage": "en", "techcrunch": "en"}


def _row(id: int, url: str, *, source_key: str = "hackernews_frontpage", title_cn: str = "[AI] 标题", points: int = 0,
         source_id: str | None = None, created: datetime = _CREATED) -> LegacyNews:
    return LegacyNews(id, url, f"title {id}", "摘要。", title_cn, points, source_id, created, source_key)


def test_split_title_cn_strips_category_prefix():
    assert split_title_cn("[硬件] 英伟达发布新 GPU") == ("英伟达发布新 GPU", "硬件")
    assert split_title_cn("无前缀标题") == ("无前缀标题", None)


@pytest.mark.parametrize(
    ("title_cn", "topics"), [("[AI] a", ("ai",)), ("[商业] a", ("business",)), ("[生态] a", ())]
)
def test_news_articles_maps_legacy_category(title_cn, topics):
    [(article, _)] = news_articles([_row(1, "https://a.com/x", title_cn=title_cn)], {}, set(), _LANGS)
    assert article.topics == topics
    assert article.label_source == "mapped"
    assert article.title_zh == "a"


def test_hn_item_id_cleans_source_id():
    assert hn_item_id("=50497621\n") == "50497621"
    assert hn_item_id("47179038") == "47179038"
    assert hn_item_id("=\n") is None


def test_hn_published_at_comes_from_reader_header():
    raw = "Title: X\n\nPublished Time: 2025-12-30T10:00:00Z\n\nMarkdown Content:\n" + "b" * 400
    bodies = {"https://a.com/hn": legacy_body(raw), "https://techcrunch.com/tc": legacy_body(raw)}
    rows = [_row(1, "https://a.com/hn"), _row(2, "https://techcrunch.com/tc", source_key="techcrunch_feed")]
    (hn, _), (tc, _) = news_articles(rows, bodies, set(), _LANGS)
    assert hn.published_at == datetime(2025, 12, 30, 10, tzinfo=UTC)
    assert tc.published_at == datetime(2026, 5, 1, 8, tzinfo=UTC)
    assert hn.first_seen_at == tc.first_seen_at == datetime(2026, 5, 1, 8, tzinfo=UTC)


def test_body_length_is_measured_before_cleaning():
    image = "![alt](https://img.example/x.png)"
    assert legacy_body("Markdown Content:\n" + "b" * (400 - len(image)) + image).full
    assert not legacy_body("Markdown Content:\n" + "b" * 399).full


@pytest.mark.parametrize(
    ("has_summary", "has_embedding", "full_body", "status"),
    [
        (True, True, False, "embedded"),
        (True, False, True, "analyzed"),
        (False, False, True, "fetched"),
        (False, False, False, "discovered"),
    ],
)
def test_initial_status(has_summary, has_embedding, full_body, status):
    assert initial_status(has_summary=has_summary, has_embedding=has_embedding, full_body=full_body) == status


def test_normalized_duplicates_merge_into_the_smallest_id():
    rows = [
        _row(9, "https://www.c82.net/euclid/", source_id="=222\n", points=50),
        _row(3, "https://c82.net/euclid", source_id="=111\n", points=10),
        _row(2, "https://c82.net/euclid#top", source_key="techcrunch_feed", points=70),
    ]
    articles = news_articles(rows, {"https://c82.net/euclid#top": LegacyBody(True, None)}, set(), _LANGS)
    assert [(a.url, a.body_level) for a, _ in articles] == [("https://c82.net/euclid#top", "full")]

    sightings = {s.channel_key: s for s in news_sightings(rows)}
    assert {s.url_key for s in sightings.values()} == {"c82.net/euclid"}
    # The TechCrunch row's points do not displace the HackerNews rows' sighting, though they are higher.
    assert (sightings["hn_frontpage"].external_id, sightings["hn_frontpage"].score) == ("222", 50)
    assert (sightings["techcrunch"].external_id, sightings["techcrunch"].score) == (None, None)


def test_same_channel_rows_keep_the_highest_score_and_span_the_seen_times():
    rows = [
        _row(3, "https://a.com/x", source_id="=111\n", points=10, created=datetime(2026, 5, 3, 8)),
        _row(9, "https://a.com/x/", source_id="=222\n", points=50, created=datetime(2026, 5, 2, 8)),
        _row(12, "https://www.a.com/x", source_id="=333\n", points=50, created=datetime(2026, 5, 1, 8)),
    ]
    [sighting] = news_sightings(rows)
    assert (sighting.external_id, sighting.title, sighting.score) == ("222", "title 9", 50)
    assert (sighting.first_seen_at, sighting.last_seen_at) == (
        datetime(2026, 5, 1, 8, tzinfo=UTC),
        datetime(2026, 5, 3, 8, tzinfo=UTC),
    )


def test_failed_rows_without_source_key_are_english():
    created = datetime(2026, 3, 1, tzinfo=UTC)
    rows = [
        LegacyFailed(1, "https://a.com/x", "t", created, None),
        LegacyFailed(2, "https://b.com/y", "t", created, "techcrunch_feed"),
    ]
    articles = failed_articles(rows, {}, {"techcrunch": "zh"})
    assert [a.lang for a in articles] == ["en", "zh"]


def test_points_on_other_rows_add_a_hackernews_sighting():
    rows = [
        _row(1, "https://techcrunch.com/a", source_key="techcrunch_feed", points=42,
             source_id="https://techcrunch.com/?p=3071234"),
        _row(2, "https://techcrunch.com/b", source_key="techcrunch_feed", points=0),
    ]
    sightings = sorted(news_sightings(rows), key=lambda s: (s.channel_key, s.url_key))
    assert [(s.channel_key, s.url_key, s.external_id, s.score) for s in sightings] == [
        ("hn_frontpage", "techcrunch.com/a", None, 42),
        ("techcrunch", "techcrunch.com/a", None, None),
        ("techcrunch", "techcrunch.com/b", None, None),
    ]
