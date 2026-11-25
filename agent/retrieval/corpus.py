"""Corpus overview text for the model and the MCP resource."""

from __future__ import annotations

import logging
import time

from agent.config import user_zone
from agent.evidence import counted
from agent.trace import warn
from common.db import get_conn, put_conn
from common.taxonomy import KINDS, TOPIC_SCOPES

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600
UNAVAILABLE = "The news database overview is temporarily unavailable."

# The query rows, not the text: the dates are rendered in each user's zone.
_cache: tuple[float, tuple[list[tuple], list[tuple]]] | None = None


def _render(kind_rows: list[tuple], publisher_rows: list[tuple]) -> str:
    counts = {kind: count for kind, count, _, _ in kind_rows}
    total = sum(counts.values())
    first = min(row[2] for row in kind_rows).astimezone(user_zone()).date()
    last = max(row[3] for row in kind_rows).astimezone(user_zone()).date()
    official = counts.get("official", 0)
    media = counts.get("media", 0)
    roles = f"Articles by publisher role: official {official}, media {media}"
    if total - official - media:
        roles += f", other {total - official - media}"
    return "\n".join([
        f"Range: {counted(total, 'article', 'articles')} from {first.isoformat()} to {last.isoformat()}.",
        roles + ".",
        "Main publishers: " + ", ".join(name for name, _ in publisher_rows) + ".",
        "Topics:",
        *(f"- {key}: {scope}" for key, scope in TOPIC_SCOPES.items()),
        "Article types: " + ", ".join(KINDS) + ".",
    ])


def corpus_info() -> str:
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _render(*_cache[1])
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.kind, COUNT(*), MIN(a.event_at), MAX(a.event_at)
            FROM news.article a
            JOIN news.publisher p ON p.publisher_id = a.publisher_id
            WHERE a.status = 'ready'
            GROUP BY 1
            ORDER BY 2 DESC
            """
        )
        kind_rows = cur.fetchall()
        cur.execute(
            """
            SELECT p.name, COUNT(*)
            FROM news.article a
            JOIN news.publisher p ON p.publisher_id = a.publisher_id
            WHERE a.status = 'ready'
            GROUP BY 1
            ORDER BY 2 DESC, 1
            LIMIT 12
            """
        )
        publisher_rows = cur.fetchall()
        cur.close()
        text = _render(kind_rows, publisher_rows)
    except Exception:
        logger.exception("corpus_info failed")
        warn("corpus_unavailable")
        return UNAVAILABLE
    finally:
        if conn is not None:
            put_conn(conn)
    _cache = (time.monotonic(), (kind_rows, publisher_rows))
    return text
