"""User memory: the followed queries of a user, and the answers given to the user before."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from langchain_core.messages import HumanMessage, SystemMessage

from agent.config import user_zone
from agent.context import _text
from agent.evidence import counted
from agent.llm import ModelCallError, build_role_model, call_text
from agent.types import Answer
from common import prompt_text
from common.db import db_cursor, db_transaction
from common.urls import url_key

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


@dataclass(frozen=True)
class PastAnswer:
    day: date  # the day of the answer in the user's zone
    question: str
    text: str
    sources: list[dict]


def past_answers(messages: list[dict]) -> list[PastAnswer]:
    """The model messages with sources, each with the latest user message before it in its thread, oldest first."""
    questions: dict[str, str] = {}
    answers = []
    for message in messages:
        if message.get("role") == "user":
            questions[message["thread_id"]] = _text(message)
        elif message.get("role") == "model" and message.get("sources"):
            day = message["created_at"].astimezone(user_zone()).date()
            answers.append(PastAnswer(day, questions.get(message["thread_id"], ""), _text(message), message["sources"]))
    return answers


def told_dates(answers: list[PastAnswer]) -> dict[str, date]:
    """The url_key of each source of the past answers, oldest first, and the day of the latest answer that cites it."""
    return {url_key(source["url"]): answer.day for answer in answers for source in answer.sources}


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


def record_turn(user_key: str, answer: Answer) -> None:
    if not answer.followed_queries:
        return
    try:
        with db_transaction() as (_, cur):
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
