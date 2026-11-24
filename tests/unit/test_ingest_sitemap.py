"""Tests for sitemap parsing, using a recorded Anthropic sitemap (trimmed to 3 entries)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ingest.discover.sitemap import parse_sitemap
from ingest.models import Candidate

CONTENT = (Path(__file__).resolve().parents[1] / "fixtures" / "ingest" / "sitemap_anthropic.xml").read_bytes()
# The sample holds /news/2026-us-midterms, /research/81k-economics (lastmod 2026-11-13T22:37Z)
# and /news/product (no <lastmod>).
MIDTERMS_LASTMOD = datetime(2026, 11, 13, 17, 2, 43, 448000, tzinfo=UTC)
NOW = datetime(2026, 11, 15, tzinfo=UTC)
MIDTERMS = Candidate(
    url="https://www.anthropic.com/news/2026-us-midterms",
    title="2026-us-midterms",
    published_at=None,
    external_id=None,
    feed_text=None,
)


def test_path_match_and_placeholder_title():
    assert parse_sitemap(CONTENT, url_pattern="/news/", now=NOW) == [MIDTERMS]


def test_other_pattern_selects_other_path():
    result = parse_sitemap(CONTENT, url_pattern="/research/", now=NOW)
    assert [c.url for c in result] == ["https://www.anthropic.com/research/81k-economics"]
    # the pattern is matched against the path, not the host
    assert parse_sitemap(CONTENT, url_pattern="anthropic.com", now=NOW) == []


def test_lookback_window_boundary_and_parameter():
    edge = MIDTERMS_LASTMOD + timedelta(days=3)
    assert parse_sitemap(CONTENT, url_pattern="/news/", now=edge) == [MIDTERMS]
    after = edge + timedelta(milliseconds=1)
    assert parse_sitemap(CONTENT, url_pattern="/news/", now=after) == []
    assert parse_sitemap(CONTENT, url_pattern="/news/", now=after, lookback_days=7) == [MIDTERMS]


def test_entry_without_lastmod_is_not_taken():
    # /news/product matches the pattern but has no <lastmod>, so even a huge window leaves it out
    result = parse_sitemap(CONTENT, url_pattern="/news/", now=NOW, lookback_days=3650)
    assert [c.url for c in result] == [MIDTERMS.url]


def test_optional_lastmod_takes_undated_entries_and_still_drops_old_ones():
    # DeepSeek's sitemap has no <lastmod> at all; dated entries keep the lookback window.
    after = MIDTERMS_LASTMOD + timedelta(days=4)
    result = parse_sitemap(CONTENT, url_pattern="/news/", now=after, require_lastmod=False)
    assert [c.url for c in result] == ["https://www.anthropic.com/news/product"]
    result = parse_sitemap(CONTENT, url_pattern="/news/", now=NOW, require_lastmod=False)
    assert [c.url for c in result] == [MIDTERMS.url, "https://www.anthropic.com/news/product"]
