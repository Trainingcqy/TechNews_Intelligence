"""Tests for the candidate filter of the discover stage."""

from __future__ import annotations

from datetime import UTC, datetime

from ingest.discover import _parse, keep_candidate
from ingest.models import Candidate
from ingest.store import DueChannel

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def _candidate(url: str) -> Candidate:
    return Candidate(url, "t", None, None, None)


def test_max_path_segments():
    config = {"max_path_segments": 2}
    assert keep_candidate(_candidate("https://huggingface.co/blog/smolvlm"), config, now=NOW)
    assert keep_candidate(_candidate("https://huggingface.co/blog/smolvlm/"), config, now=NOW)
    assert not keep_candidate(_candidate("https://huggingface.co/blog/someuser/my-post"), config, now=NOW)


def test_no_limit_without_config():
    assert keep_candidate(_candidate("https://huggingface.co/blog/someuser/my-post"), {}, now=NOW)


def test_exclude_url_drops_matching_urls_ignoring_case():
    config = {"exclude_url": "/business/learn/|-sale(-|/|$)"}
    assert not keep_candidate(_candidate("https://openai.com/business/learn/pr-scans-demo"), config, now=NOW)
    assert not keep_candidate(_candidate("https://example.com/Big-SALE/"), config, now=NOW)
    assert keep_candidate(_candidate("https://example.com/gta-vi-sales"), config, now=NOW)
    assert keep_candidate(_candidate("https://openai.com/index/introducing-shirudo"), config, now=NOW)


def test_feed_timezone_overrides_the_stated_offset():
    # OpenAI's feed writes Pacific clock times labelled GMT.
    rss = ('<?xml version="1.0"?><rss version="2.0"><channel><item><title>T</title>'
           '<link>https://openai.com/index/a</link><pubDate>Tue, 17 Nov 2026 13:00:00 GMT</pubDate>'
           '</item></channel></rss>')
    channel = DueChannel(1, "openai_news", "feed", "https://openai.com/news/rss.xml", "corpus", "en",
                         {"feed_timezone": "America/Los_Angeles"}, None, None)
    (candidate,) = _parse(channel, rss.encode("utf-8"), NOW)
    assert candidate.published_at == datetime(2026, 11, 17, 21, 0, tzinfo=UTC)
