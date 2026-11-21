"""RSS and Atom parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser

import feedparser

from ingest.dates import parse_datetime
from ingest.models import Candidate

_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "ul", "ol", "tr", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (line.strip() for line in "".join(extractor.parts).splitlines())
    return "\n".join(line for line in lines if line)


def entry_published_at(entry, *, now: datetime) -> datetime | None:
    parsed = entry.get("published_parsed")
    if parsed:
        return min(datetime(*parsed[:6], tzinfo=UTC), now)
    return parse_datetime(entry.get("published"), now=now)


def _feed_text(entry) -> str | None:
    contents = entry.get("content")
    raw = contents[0].get("value") if contents else None
    text = strip_html(raw or entry.get("summary") or "")
    return text or None


def parse_feed(content: bytes, *, now: datetime) -> list[Candidate]:
    candidates = []
    for entry in feedparser.parse(content).entries:
        url = (entry.get("link") or "").strip()
        if not url:
            continue
        candidates.append(
            Candidate(
                url=url,
                title=(entry.get("title") or "").strip(),
                published_at=entry_published_at(entry, now=now),
                external_id=entry.get("id"),
                feed_text=_feed_text(entry),
                score=None,
                comment_count=None,
                channel_time=None,
            )
        )
    return candidates
