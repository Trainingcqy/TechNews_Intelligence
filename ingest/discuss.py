"""discuss stage: visit HackerNews discussions once and store the leading comments as text."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ingest import http, store
from ingest.config import IngestConfig
from ingest.discover.feed import strip_html

logger = logging.getLogger(__name__)

ITEM_API_URL = "https://hn.algolia.com/api/v1/items/{}"
ITEM_PAGE_URL = "https://news.ycombinator.com/item?id={}"
DEFAULT_AFTER_HOURS = 24
DEFAULT_MIN_COMMENTS = 20
# A deleted item fails every visit and, taken oldest first, would otherwise keep its place in every round.
MAX_AGE_DAYS = 7
TOP_COMMENTS = 30
COMMENT_CHARS = 800


@dataclass(frozen=True)
class Discussion:
    points: int | None
    comment_total: int
    text: str


def _replies(node: dict) -> int:
    """Descendants with text. A deleted comment has no text, but its replies stay in the tree."""
    return sum(bool(child.get("text")) + _replies(child) for child in node.get("children") or [])


def parse_discussion(content: bytes) -> Discussion:
    item = json.loads(content)
    top = [child for child in item.get("children") or [] if child.get("text")]
    replies = {child["id"]: _replies(child) for child in top}
    # HackerNews does not publish comment scores; the number of replies stands in for where the discussion is.
    top.sort(key=lambda child: (-replies[child["id"]], child["created_at_i"]))
    entries = [
        f"[{n}] {child.get('author') or ''} · 回复 {replies[child['id']]} 条\n"
        f"{strip_html(child['text'])[:COMMENT_CHARS]}"
        for n, child in enumerate(top[:TOP_COMMENTS], 1)
    ]
    return Discussion(
        points=item.get("points"), comment_total=_replies(item), text="\n\n".join(entries).replace("\x00", "")
    )


def discuss_sighting(item: store.DiscussItem, *, transaction: store.Transaction, config: IngestConfig) -> None:
    response = http.get(ITEM_API_URL.format(item.external_id), user_agent=http.USER_AGENT, timeout=config.discover_timeout)
    if response.status != 200:
        raise ValueError(f"HTTP {response.status}")
    discussion = parse_discussion(response.content)
    with transaction() as cur:
        store.record_discussion(
            cur,
            item.sighting_id,
            url=ITEM_PAGE_URL.format(item.external_id),
            comment_total=discussion.comment_total,
            text=discussion.text,
            score=discussion.points,
        )


def run_discuss(config: IngestConfig, *, transaction: store.Transaction) -> None:
    with transaction() as cur:
        items = store.take_discuss(
            cur,
            config.discuss_limit,
            default_after_hours=DEFAULT_AFTER_HOURS,
            default_min_comments=DEFAULT_MIN_COMMENTS,
            max_age_days=MAX_AGE_DAYS,
        )
    for item in items:
        try:
            discuss_sighting(item, transaction=transaction, config=config)
        except Exception:
            # No discussion row is written, so the next round tries again.
            logger.exception("discuss %s failed", item.external_id)
