"""User memory: followed queries and the stories a user has already been shown."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from agent.config import user_zone
from agent.trace import warn
from agent.types import Answer
from common import prompt_text
from common.db import db_cursor, db_transaction

logger = logging.getLogger(__name__)

MAX_FOLLOWS = 20


class FollowLimitError(ValueError):
    pass


@dataclass(frozen=True)
class Follow:
    follow_id: int
    query: str
    last_checked_at: datetime | None


def list_follows(user_key: str, limit: int) -> list[Follow]:
    with db_cursor() as (_, cur):
        cur.execute(
            """
            SELECT follow_id, query, last_checked_at
            FROM public.user_follow
            WHERE user_key = %s AND active
            ORDER BY created_at, follow_id
            LIMIT %s
            """,
            (user_key, limit),
        )
        return [Follow(*row) for row in cur.fetchall()]


def add_follow(user_key: str, query: str) -> Follow:
    query = query.strip()
    if not query:
        raise ValueError("query must not be blank")
    with db_transaction() as (_, cur):
        cur.execute(
            "SELECT COUNT(*) FROM public.user_follow WHERE user_key = %s AND active AND LOWER(query) <> LOWER(%s)",
            (user_key, query),
        )
        if cur.fetchone()[0] >= MAX_FOLLOWS:
            raise FollowLimitError(f"at most {MAX_FOLLOWS} follows")
        # A removed follow is only deactivated, so adding the same query again revives that row.
        cur.execute(
            """
            INSERT INTO public.user_follow (user_key, query)
            VALUES (%s, %s)
            ON CONFLICT (user_key, LOWER(query)) DO UPDATE SET active = TRUE
            RETURNING follow_id, query, last_checked_at
            """,
            (user_key, query),
        )
        return Follow(*cur.fetchone())


def remove_follow(user_key: str, follow_id: int) -> bool:
    with db_transaction() as (_, cur):
        cur.execute(
            """
            UPDATE public.user_follow SET active = FALSE
            WHERE user_key = %s AND follow_id = %s AND active
            """,
            (user_key, follow_id),
        )
        return cur.rowcount > 0


def seen_story_dates(user_key: str, story_ids: list[int]) -> dict[int, date]:
    if not story_ids:
        return {}
    try:
        with db_cursor() as (_, cur):
            cur.execute(
                """
                SELECT story_id, last_seen_at FROM public.user_story_seen
                WHERE user_key = %s AND story_id = ANY(%s)
                """,
                (user_key, list(story_ids)),
            )
            return {story_id: seen_at.astimezone(user_zone()).date() for story_id, seen_at in cur.fetchall()}
    except Exception:
        logger.exception("seen story lookup failed")
        warn("seen_dates_unavailable")
        return {}


def record_turn(user_key: str, answer: Answer) -> None:
    try:
        with db_transaction() as (_, cur):
            for story_id in answer.story_ids:
                cur.execute(
                    """
                    INSERT INTO public.user_story_seen (user_key, story_id) VALUES (%s, %s)
                    ON CONFLICT (user_key, story_id) DO UPDATE SET last_seen_at = NOW()
                    """,
                    (user_key, story_id),
                )
            if answer.followed_queries:
                cur.execute(
                    """
                    UPDATE public.user_follow SET last_checked_at = NOW()
                    WHERE user_key = %s AND active AND LOWER(query) = ANY(%s)
                    """,
                    (user_key, [query.lower() for query in answer.followed_queries]),
                )
    except Exception:
        logger.exception("recording the turn in user memory failed")


def render_follows(follows: list[Follow], today: date) -> str:
    if not follows:
        return ""
    items = []
    for follow in follows:
        if follow.last_checked_at is None:
            items.append(f"- {follow.query}，尚未查看过")
        else:
            checked = follow.last_checked_at.astimezone(user_zone()).date()
            items.append(f"- {follow.query}，上次查看 {checked:%Y-%m-%d}，距今 {(today - checked).days} 天")
    return prompt_text.render("gather.follows", items="\n".join(items))
