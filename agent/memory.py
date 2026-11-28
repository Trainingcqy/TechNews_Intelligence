"""User memory: followed queries and the stories a user has already been shown."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from langchain_core.messages import HumanMessage, SystemMessage

from agent.config import user_zone
from agent.evidence import counted
from agent.llm import ModelCallError, build_role_model, call_text
from agent.trace import warn
from agent.types import Answer
from common import prompt_text
from common.db import db_cursor, db_transaction

logger = logging.getLogger(__name__)

MAX_FOLLOWS = 20


class FollowLimitError(ValueError):
    pass


class FollowQueryError(Exception):
    """The English search query of a follow could not be generated. Not a ValueError: the API maps that to 422."""


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


def _search_query(query: str) -> str:
    """The English search query of a follow: the text itself when it is ASCII, else one model call."""
    if query.isascii():
        return query
    messages = [SystemMessage(prompt_text.text("follow.system")), HumanMessage(query)]
    try:
        text = call_text(build_role_model("follow"), messages)
    except ModelCallError as exc:
        raise FollowQueryError(f"model call failed: {exc}") from exc
    if not text or not text.isascii() or len(text.splitlines()) > 1:
        raise FollowQueryError(f"invalid search query: {text!r}")
    return text


def add_follow(user_key: str, query: str) -> Follow:
    query = query.strip()
    if not query:
        raise ValueError("query must not be blank")
    search_query = _search_query(query)
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
            INSERT INTO public.user_follow (user_key, query, search_query)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_key, LOWER(query)) DO UPDATE SET active = TRUE, search_query = EXCLUDED.search_query
            RETURNING follow_id, query, last_checked_at
            """,
            (user_key, query, search_query),
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


def check_window(follow: Follow, today: date) -> int:
    """Days to search when checking a follow: since its last check, at least 1 and at most 30; 14 if never checked."""
    if follow.last_checked_at is None:
        return 14
    days = (today - follow.last_checked_at.astimezone(user_zone()).date()).days
    return min(max(days, 1), 30)


def render_follows(follows: list[Follow], today: date) -> str:
    if not follows:
        return ""
    items = []
    for follow in follows:
        if follow.last_checked_at is None:
            items.append(f"- {follow.query}: never checked")
        else:
            checked = follow.last_checked_at.astimezone(user_zone()).date()
            ago = counted((today - checked).days, "day", "days")
            items.append(f"- {follow.query}: last checked {checked:%Y-%m-%d}, {ago} ago")
    return prompt_text.render("gather.follows", items="\n".join(items))
