"""Tests for RSS/Atom parsing, using recorded feeds (trimmed to 3 entries)."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from pathlib import Path

from ingest.discover.feed import entry_published_at, parse_feed, strip_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
FB_FIRST_PUBDATE = "<pubDate>Fri, 13 Nov 2026 17:00:39 +0000</pubDate>"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(text: str):
    return parse_feed(text.encode("utf-8"), now=NOW)


def test_rss_content_encoded_is_preferred_over_summary():
    first, *rest = _parse(_load("feed_rss_content_encoded.xml"))
    assert len(rest) == 2
    assert first.url == "https://engineering.fb.com/2026/11/13/android/cuttlefish-on-mac/"
    assert first.external_id == "https://engineering.fb.com/?p=25047"
    assert first.published_at == datetime(2026, 11, 13, 17, 0, 39, tzinfo=UTC)
    # the <description> excerpt is 524 characters; the body is in content:encoded
    assert len(first.feed_text) > 5000
    assert first.feed_text.startswith("Meta’s Reality Labs builds products such as AI glasses.")
    assert "<" not in first.feed_text
    # WordPress ends every entry with "The post <title> appeared first on <site>."; the line is dropped.
    assert first.feed_text.endswith("subsequent productization work.")


def test_wordpress_footer_is_the_only_line_dropped():
    def entry(body: str) -> str:
        return ('<?xml version="1.0"?><rss version="2.0"><channel><item><title>T</title>'
                f"<link>https://example.com/a</link><description><![CDATA[{body}]]></description>"
                "</item></channel></rss>")

    footer = ('<p>The post <a href="https://example.com/a">T</a> appeared first on '
              '<a href="https://example.com">Site</a>.</p>')
    (candidate,) = _parse(entry("<p>The post office opened a new branch.</p>" + footer))
    assert candidate.feed_text == "The post office opened a new branch."
    (only_footer,) = _parse(entry(footer))
    assert only_footer.feed_text is None


def test_longest_content_item_is_the_body_and_double_escaped_markup_is_removed():
    # Tom's Hardware puts the author bio, its markup escaped twice, in the first content item and the article second.
    bio = "&lt;p&gt;Brandon has been tinkering with PCs since childhood.&lt;/p&gt;"
    article = "<p>" + "The router is 55 percent off this week. " * 50 + "</p>"
    feed = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title>'
        '<link href="https://www.tomshardware.com/a"/><id>1</id>'
        f'<content type="html">{bio}</content><content type="html"><![CDATA[{article}]]></content>'
        "</entry></feed>"
    )
    (candidate,) = _parse(feed)
    assert candidate.feed_text.startswith("The router is 55 percent off this week.")
    assert strip_html(html.unescape(bio)) == "Brandon has been tinkering with PCs since childhood."
    assert strip_html("&lt;p&gt;a &lt;b&gt;b&lt;/b&gt;&lt;/p&gt;") == "a b"


def test_atom_content_and_time_zone():
    first, *rest = _parse(_load("feed_atom.xml"))
    assert len(rest) == 2
    assert first.url == "https://www.theverge.com/entertainment/1035806/grand-theft-auto-6-rob-nelson-interview"
    assert first.title.startswith("‘It’s a risk’")
    assert first.external_id == "https://www.theverge.com/?p=1035806"
    # <published> is 2026-11-18T06:00:00-05:00
    assert first.published_at == datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    assert first.feed_text.startswith("Grand Theft Auto VI isn't just the biggest GTA game to date")
    assert "<" not in first.feed_text


def test_body_in_description():
    first, *_ = _parse(_load("feed_rss_description.xml"))
    assert first.feed_text.startswith("阿里旗下跨境电商平台速卖通")
    assert "<" not in first.feed_text
    # no <guid> in this feed
    assert first.external_id is None
    # pubDate is "Wed, 18 Nov 2026 13:34:00 +0800"
    assert first.published_at == datetime(2026, 11, 18, 5, 34, tzinfo=UTC)


def test_entry_without_published_time():
    text = _load("feed_rss_content_encoded.xml")
    assert FB_FIRST_PUBDATE in text
    first, second, third = _parse(text.replace(FB_FIRST_PUBDATE, ""))
    assert first.published_at is None
    assert second.published_at == datetime(2026, 11, 10, 17, 0, 49, tzinfo=UTC)
    assert third.published_at == datetime(2026, 11, 9, 17, 0, 12, tzinfo=UTC)


def test_future_published_time_is_clamped_to_now():
    text = _load("feed_rss_content_encoded.xml").replace("Fri, 13 Nov 2026 17:00:39", "Fri, 13 Nov 2027 17:00:39")
    assert _parse(text)[0].published_at == NOW


def test_published_time_falls_back_to_raw_string():
    # feedparser accepts nearly every format seen in feeds, so the fallback is exercised with a bare entry
    entry = {"published": "2026-11-17 12:31:40  +0800"}
    assert entry_published_at(entry, now=NOW) == datetime(2026, 11, 17, 4, 31, 40, tzinfo=UTC)
    assert entry_published_at({"published": "2027-01-01T00:00:00+00:00"}, now=NOW) == NOW
    assert entry_published_at({"published": "not a date"}, now=NOW) is None
    assert entry_published_at({}, now=NOW) is None


def test_strip_html_separates_blocks_and_unescapes():
    assert strip_html("<p>a</p><p>b &amp; <b>c</b></p>") == "a\nb & c"
