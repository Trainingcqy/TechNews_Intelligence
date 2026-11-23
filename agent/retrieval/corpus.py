"""Corpus overview text for the model and the MCP resource."""

from __future__ import annotations

import logging
import time

from agent.trace import warn
from common.db import get_conn, put_conn
from common.taxonomy import KINDS, TOPICS

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600
UNAVAILABLE = "新闻库范围暂时无法获取"

_cache: tuple[float, str] | None = None


def _render(kind_rows: list[tuple], publisher_rows: list[tuple]) -> str:
    counts = {kind: count for kind, count, _, _ in kind_rows}
    total = sum(counts.values())
    first = min(row[2] for row in kind_rows)
    last = max(row[3] for row in kind_rows)
    official = counts.get("official", 0)
    media = counts.get("media", 0)
    return "\n".join([
        f"新闻库范围：共 {total} 篇，{first.isoformat()} 至 {last.isoformat()}。",
        f"发布方角色：官方 {official} 篇；媒体 {media} 篇；经 HackerNews 发现的其他站点 {total - official - media} 篇。",
        "主要发布方：" + "、".join(name for name, _ in publisher_rows) + "。",
        "领域：" + "；".join(f"{key} {label}" for key, label in TOPICS.items()) + "。",
        "类型：" + "；".join(f"{key} {label}" for key, label in KINDS.items()) + "。",
        "只有被转到 HackerNews 首页的文章带社区分数和评论数。",
    ])


def corpus_info() -> str:
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.kind, COUNT(*), MIN(a.event_at)::date, MAX(a.event_at)::date
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
    _cache = (time.monotonic(), text)
    return text
