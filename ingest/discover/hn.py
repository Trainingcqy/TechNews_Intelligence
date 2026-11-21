"""HackerNews Algolia API parsing."""

from __future__ import annotations

import json
from datetime import datetime

from ingest.discover.feed import strip_html
from ingest.models import Candidate

_ITEM_URL = "https://news.ycombinator.com/item?id={}"


def parse_hn(content: bytes) -> list[Candidate]:
    candidates = []
    for hit in json.loads(content)["hits"]:
        object_id = hit["objectID"]
        url = hit.get("url")
        created_at = hit.get("created_at")
        story_text = None if url else strip_html(hit.get("story_text") or "") or None
        candidates.append(
            Candidate(
                url=url or _ITEM_URL.format(object_id),
                title=hit.get("title") or "",
                published_at=None,
                external_id=object_id,
                feed_text=story_text,
                score=hit.get("points"),
                comment_count=hit.get("num_comments"),
                channel_time=datetime.fromisoformat(created_at) if created_at else None,
            )
        )
    return candidates
