"""Sitemap parsing."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from ingest.dates import parse_datetime
from ingest.models import Candidate

_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def placeholder_title(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rpartition("/")[2]


def parse_sitemap(
    content: bytes, *, url_pattern: str, now: datetime, lookback_days: int = 3, require_lastmod: bool = True
) -> list[Candidate]:
    """Entries under url_pattern modified within lookback_days.

    By default entries without <lastmod> are skipped: on Anthropic's sitemap that is only the /news/<category> index
    pages. A sitemap that never carries <lastmod>, such as DeepSeek's, sets require_lastmod to False; its entries are
    all returned and discover keeps only the URLs not stored yet.
    """
    cutoff = now - timedelta(days=lookback_days)
    candidates = []
    for node in ET.fromstring(content).iter(f"{_NS}url"):
        loc = (node.findtext(f"{_NS}loc") or "").strip()
        path = urlsplit(loc).path
        if url_pattern not in path:
            continue
        lastmod = parse_datetime(node.findtext(f"{_NS}lastmod"), now=now)
        if lastmod is None:
            if require_lastmod:
                continue
        elif lastmod < cutoff:
            continue
        candidates.append(
            Candidate(
                url=loc, title=placeholder_title(loc), published_at=None, external_id=None, feed_text=None,
                lastmod=lastmod,
            )
        )
    return candidates
