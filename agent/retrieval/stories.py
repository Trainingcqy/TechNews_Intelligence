"""Story-level reads: members, bodies, story statistics and browsing."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from agent.types import Article, StoryStats

from .literal import exact_pattern, literal_match

ARTICLE_COLUMNS = """
            a.article_id,
            a.url,
            a.title,
            COALESCE(a.title_zh, '') AS title_zh,
            COALESCE(a.summary, '') AS summary,
            a.topics,
            a.kind,
            p.name AS publisher,
            p.kind AS publisher_kind,
            a.lang,
            a.event_at,
            a.first_seen_at,
            a.body_level,
            m.story_id"""

ARTICLE_JOINS = """
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        JOIN news.story_member m ON m.article_id = a.article_id"""

RELATED_LIMIT = 6


def article_from_row(row: tuple) -> Article:
    (article_id, url, title, title_zh, summary, topics, kind, publisher, publisher_kind,
     lang, event_at, first_seen_at, body_level, story_id) = row[:14]
    return Article(
        article_id=article_id,
        url=url,
        title=title,
        title_zh=title_zh,
        summary=summary,
        topics=tuple(topics or ()),
        kind=kind,
        publisher=publisher,
        publisher_kind=publisher_kind,
        lang=lang,
        published_at=event_at,
        first_seen_at=first_seen_at,
        body_level=body_level,
        story_id=story_id,
    )


def fetch_story_members(cur, story_ids: list[int]) -> dict[int, list[Article]]:
    if not story_ids:
        return {}
    cur.execute(
        f"""
        SELECT {ARTICLE_COLUMNS}
        FROM news.article a
        {ARTICLE_JOINS}
        WHERE m.story_id = ANY(%(story_ids)s) AND a.status = 'ready'
        ORDER BY m.story_id, a.event_at
        """,
        {"story_ids": list(story_ids)},
    )
    members: dict[int, list[Article]] = {}
    for row in cur.fetchall():
        article = article_from_row(row)
        members.setdefault(article.story_id, []).append(article)
    return members


def fetch_articles_by_url_keys(cur, url_keys: list[str]) -> list[Article]:
    if not url_keys:
        return []
    cur.execute(
        f"""
        SELECT {ARTICLE_COLUMNS}
        FROM news.article a
        {ARTICLE_JOINS}
        WHERE a.url_key = ANY(%(url_keys)s) AND a.status = 'ready'
        """,
        {"url_keys": list(url_keys)},
    )
    return [article_from_row(row) for row in cur.fetchall()]


def fetch_bodies(cur, article_ids: list[int]) -> dict[int, str]:
    if not article_ids:
        return {}
    cur.execute("SELECT article_id, text FROM news.article_body WHERE article_id = ANY(%s)", (list(article_ids),))
    return {article_id: text for article_id, text in cur.fetchall()}


def stats_from_members(story_id: int, members: list[Article]) -> StoryStats:
    times = [m.published_at for m in members]
    return StoryStats(
        story_id=story_id,
        report_count=len(members),
        publisher_count=len({m.publisher for m in members}),
        publisher_kinds=tuple(dict.fromkeys(m.publisher_kind for m in members)),
        first_at=min(times),
        last_at=max(times),
    )


def pick_lead(members: list[Article]) -> Article:
    return min(members, key=lambda m: (m.publisher_kind != "official", m.body_level != "full", m.published_at))


def browse_story_ids(cur, *, start_at: datetime, end_at: datetime, topic: str | None,
                     kinds: tuple[str, ...] | None, publisher_kind: str | None,
                     sort: str, limit: int, about: str | None = None, min_publishers: int = 1,
                     seen_after: datetime | None = None, exclude_url_keys: Collection[str] = ()) -> list[int]:
    """With about, only stories with an article that names it in the title or comes from the official publisher of
    that name."""
    pattern = exact_pattern(about) if about else None
    about_hit = ""
    if about:
        about_hit = (f"AND ({literal_match('a.title', pattern, '%(about)s')}"
                     f" OR (p.kind = 'official' AND {literal_match('p.name', pattern, '%(about)s')}))")
    cur.execute(
        f"""
        WITH win AS (
            SELECT DISTINCT m.story_id
            FROM news.article a
            JOIN news.publisher p ON p.publisher_id = a.publisher_id
            JOIN news.story_member m ON m.article_id = a.article_id
            WHERE a.status = 'ready'
              AND a.event_at >= %(start_at)s AND a.event_at < %(end_at)s
              AND (%(topic)s::text IS NULL OR a.topics @> ARRAY[%(topic)s::text])
              AND (%(kinds)s::text[] IS NULL OR a.kind = ANY(%(kinds)s::text[]))
              AND (%(publisher_kind)s::text IS NULL OR p.kind = %(publisher_kind)s)
              AND (%(seen_after)s::timestamptz IS NULL OR a.first_seen_at > %(seen_after)s)
              AND (%(exclude_url_keys)s::text[] IS NULL OR a.url_key <> ALL(%(exclude_url_keys)s::text[]))
              {about_hit}
        ),
        stats AS (
            SELECT m.story_id,
                   COUNT(DISTINCT a.publisher_id) AS publisher_count,
                   MAX(a.event_at) AS last_at
            FROM win
            JOIN news.story_member m ON m.story_id = win.story_id
            JOIN news.article a ON a.article_id = m.article_id AND a.status = 'ready'
            GROUP BY m.story_id
        )
        SELECT story_id
        FROM stats
        WHERE publisher_count >= %(min_publishers)s
        ORDER BY
            CASE WHEN %(sort)s = 'importance' THEN publisher_count END DESC NULLS LAST,
            last_at DESC
        LIMIT %(limit)s
        """,
        {
            "start_at": start_at,
            "end_at": end_at,
            "topic": topic,
            # psycopg2 adapts a tuple to a row literal, not an array.
            "kinds": list(kinds) if kinds is not None else None,
            "publisher_kind": publisher_kind,
            "seen_after": seen_after,
            "exclude_url_keys": list(exclude_url_keys) or None,
            "sort": sort,
            "limit": limit,
            "about": about,
            "exact_pattern": pattern,
            "min_publishers": min_publishers,
        },
    )
    return [row[0] for row in cur.fetchall()]
