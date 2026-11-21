"""Reference channel parsing (Techmeme)."""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser

import feedparser

from ingest.discover.feed import entry_published_at
from ingest.models import ReferenceHeadline

_PERMALINK_TITLE = "Techmeme permalink"


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict] = []  # href, title, text, bold
        self._open: dict | None = None
        self._bold_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "b":
            self._bold_depth += 1
        elif tag == "a":
            attr = dict(attrs)
            self._open = {"href": attr.get("href"), "title": attr.get("title"), "text": "", "bold": self._bold_depth > 0}
            self.anchors.append(self._open)

    def handle_endtag(self, tag):
        if tag == "b":
            self._bold_depth -= 1
        elif tag == "a":
            self._open = None

    def handle_data(self, data):
        if self._open is not None:
            self._open["text"] += data


def parse_techmeme_item(description: str) -> tuple[str | None, str | None, str | None]:
    """Return (original article URL, headline, outlet name) from a Techmeme item's description HTML."""
    parser = _DescriptionParser()
    parser.feed(description)
    parser.close()
    anchors = parser.anchors
    # The original URL is the headline link inside <B>. The first HREF is not it for items without a picture:
    # there it is the permalink.
    headline = next((a for a in anchors if a["bold"]), None)
    outlet = None
    for i, anchor in enumerate(anchors[:-1]):
        if anchor["title"] == _PERMALINK_TITLE:
            outlet = anchors[i + 1]["text"].strip() or None
            break
    if headline is None:
        return None, None, outlet
    return headline["href"], headline["text"].strip() or None, outlet


def parse_reference_feed(content: bytes, *, now: datetime) -> list[ReferenceHeadline]:
    headlines = []
    for entry in feedparser.parse(content).entries:
        external_id = entry.get("id")
        if not external_id:
            continue
        target_url, headline, outlet = parse_techmeme_item(entry.get("summary") or "")
        headlines.append(
            ReferenceHeadline(
                external_id=external_id,
                title=headline or (entry.get("title") or "").strip(),
                target_url=target_url,
                outlet=outlet,
                published_at=entry_published_at(entry, now=now),
            )
        )
    return headlines
