"""RSS and Atom parsing."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser

import feedparser

from ingest.dates import parse_datetime
from ingest.models import Candidate

_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "ul", "ol", "tr", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})
_MARKUP_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(\s[^<>]*)?/?>")
# The line that WordPress adds to the end of every entry: "The post <title> appeared first on <site>."
_WORDPRESS_FOOTER_RE = re.compile(r"(?:^|\n)The post .+ appeared first on .+$")


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


def _strip_once(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (line.strip() for line in "".join(extractor.parts).splitlines())
    return "\n".join(line for line in lines if line)


def strip_html(html: str) -> str:
    text = _strip_once(html)
    # Some feeds escape their markup twice, so the first pass leaves the tags as literal text.
    if _MARKUP_RE.search(text):
        text = _strip_once(text)
    return text


def entry_published_at(entry, *, now: datetime) -> datetime | None:
    parsed = entry.get("published_parsed")
    if parsed:
        return min(datetime(*parsed[:6], tzinfo=UTC), now)
    return parse_datetime(entry.get("published"), now=now)


def _feed_text(entry) -> str | None:
    # An entry can carry several content items; Tom's Hardware puts the author bio first and the article second.
    texts = [strip_html(content.get("value") or "") for content in entry.get("content") or []]
    text = max(texts, key=len, default="") or strip_html(entry.get("summary") or "")
    return _WORDPRESS_FOOTER_RE.sub("", text) or None


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
            )
        )
    return candidates
