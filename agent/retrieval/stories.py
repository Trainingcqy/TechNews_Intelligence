"""Story-level reads: members, bodies, discussions, story statistics and browsing."""

from __future__ import annotations

from datetime import datetime

from agent.types import Article, Discussion, StoryStats

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
            a.body_level,
            hn.score AS hn_score,
            hn.comment_count AS hn_comments,
            hn.sighting_id AS hn_sighting_id,
            m.story_id"""

ARTICLE_JOINS = """
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        JOIN news.story_member m ON m.article_id = a.article_id
        LEFT JOIN LATERAL (
            SELECT s.sighting_id, s.score, s.comment_count
            FROM news.sighting s
            JOIN news.channel c ON c.channel_id = s.channel_id
            WHERE s.article_id = a.article_id AND c.fetch_kind = 'hn'
            ORDER BY s.score DESC NULLS LAST
            LIMIT 1
        ) hn ON TRUE"""

RELATED_LIMIT = 6

_DISCUSSION_SELECT = """
        SELECT d.sighting_id, s.article_id, d.url, a.title, s.score, d.comment_total
        FROM news.discussion d
        JOIN news.sighting s ON s.sighting_id = d.sighting_id
        JOIN news.article a ON a.article_id = s.article_id"""

HN_CHANNEL_KEY = "hn_frontpage"

_hn_channel_id: int | None = None


def article_from_row(row: tuple) -> Article:
    (article_id, url, title, title_zh, summary, topics, kind, publisher, publisher_kind,
     lang, event_at, body_level, hn_score, hn_comments, hn_sighting_id, story_id) = row[:16]
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
        body_level=body_level,
        hn_score=hn_score,
        hn_comments=hn_comments,
        hn_sighting_id=hn_sighting_id,
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


def fetch_discussions(cur, sighting_ids: list[int]) -> dict[int, Discussion]:
    if not sighting_ids:
        return {}
    cur.execute(f"{_DISCUSSION_SELECT} WHERE d.sighting_id = ANY(%s)", (list(sighting_ids),))
    return {row[0]: Discussion(*row) for row in cur.fetchall()}


def fetch_discussions_by_url_keys(cur, url_keys: list[str]) -> list[Discussion]:
    if not url_keys:
        return []
    # discuss.py stores every discussion URL as https://news.ycombinator.com/item?id=N, which is "https://" + url_key.
    cur.execute(
        f"{_DISCUSSION_SELECT} WHERE d.url = ANY(%s) AND a.status = 'ready'",
        (["https://" + key for key in url_keys],),
    )
    return [Discussion(*row) for row in cur.fetchall()]


def fetch_discussion_texts(cur, sighting_ids: list[int]) -> dict[int, str]:
    if not sighting_ids:
        return {}
    cur.execute("SELECT sighting_id, text FROM news.discussion WHERE sighting_id = ANY(%s)", (list(sighting_ids),))
    return {sighting_id: text for sighting_id, text in cur.fetchall()}


def stats_from_members(story_id: int, members: list[Article]) -> StoryStats:
    on_hn = [m for m in members if m.hn_sighting_id is not None]
    best_hn = max(on_hn, key=lambda m: -1 if m.hn_score is None else m.hn_score, default=None)
    hn_comments = [m.hn_comments for m in members if m.hn_comments is not None]
    times = [m.published_at for m in members]
    return StoryStats(
        story_id=story_id,
        report_count=len(members),
        publisher_count=len({m.publisher for m in members}),
        publisher_kinds=tuple(dict.fromkeys(m.publisher_kind for m in members)),
        hn_score=best_hn.hn_score if best_hn else None,
        hn_comments=max(hn_comments, default=None),
        hn_sighting_id=best_hn.hn_sighting_id if best_hn else None,
        first_at=min(times),
        last_at=max(times),
    )


def pick_lead(members: list[Article]) -> Article:
    return min(
        members,
        key=lambda m: (
            m.publisher_kind != "official",
            m.body_level != "full",
            m.hn_score is None,
            -(m.hn_score or 0),
            m.published_at,
        ),
    )


def _get_hn_channel_id(cur) -> int | None:
    global _hn_channel_id
    if _hn_channel_id is None:
        cur.execute("SELECT channel_id FROM news.channel WHERE key = %s", (HN_CHANNEL_KEY,))
        row = cur.fetchone()
        _hn_channel_id = row[0] if row else None
    return _hn_channel_id


def browse_story_ids(cur, *, start_at: datetime, end_at: datetime, topic: str | None,
                     kinds: tuple[str, ...] | None, publisher_kind: str | None,
                     sort: str, limit: int, about: str | None = None, min_publishers: int = 1) -> list[int]:
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
              {about_hit}
        ),
        stats AS (
            SELECT m.story_id,
                   COUNT(DISTINCT a.publisher_id) AS publisher_count,
                   MAX(s.score) AS hn_score,
                   MAX(a.event_at) AS last_at
            FROM win
            JOIN news.story_member m ON m.story_id = win.story_id
            JOIN news.article a ON a.article_id = m.article_id AND a.status = 'ready'
            LEFT JOIN news.sighting s ON s.article_id = a.article_id AND s.channel_id = %(hn_channel_id)s
            GROUP BY m.story_id
        )
        SELECT story_id
        FROM stats
        WHERE publisher_count >= %(min_publishers)s
          AND (%(sort)s <> 'hn' OR hn_score IS NOT NULL)
        ORDER BY
            CASE WHEN %(sort)s = 'importance' THEN publisher_count END DESC NULLS LAST,
            CASE WHEN %(sort)s IN ('importance', 'hn') THEN hn_score END DESC NULLS LAST,
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
            "hn_channel_id": _get_hn_channel_id(cur),
            "sort": sort,
            "limit": limit,
            "about": about,
            "exact_pattern": pattern,
            "min_publishers": min_publishers,
        },
    )
    return [row[0] for row in cur.fetchall()]
