"""Article-level hybrid lookup and story-level search."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from agent.config import user_zone
from agent.trace import trace_span
from agent.types import Article, StoryHit
from common.db import get_conn, put_conn
from common.taxonomy import EVENT_KINDS

from .embeddings import get_query_embedding as _get_query_embedding
from .hybrid import _clamp_int, candidate_from_row, fetch_hybrid_rows
from .recall_profile import resolve_recall_profile
from .rerank import RERANK_MODE_NONE, rerank_candidates, resolve_rerank_mode
from .stories import (
    RELATED_LIMIT,
    browse_story_ids,
    fetch_discussions,
    fetch_story_members,
    pick_lead,
    stats_from_members,
)

MAX_ARTICLE_LIMIT = 45
WEAK_MATCH_THRESHOLD = 0.30


def time_window(days: int, start_date: date | None, end_date: date | None,
                *, now: datetime) -> tuple[datetime, datetime]:
    if start_date is None:
        return now - timedelta(days=days), now
    start_at = datetime.combine(start_date, time.min, tzinfo=user_zone())
    if end_date is None:
        return start_at, now
    return start_at, datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=user_zone())


def _rows_to_rerank_candidates(rows: list[tuple]) -> list[dict[str, Any]]:
    return [{**candidate_from_row(row), "payload": row} for row in rows]


def _finalize_candidate_rows(rows: list[tuple], *, query: str, limit: int, rerank_mode: str) -> list[tuple]:
    if not rows:
        return []
    if rerank_mode == RERANK_MODE_NONE:
        return list(rows)[:limit]
    reranked = rerank_candidates(query, _rows_to_rerank_candidates(rows), mode=rerank_mode, top_k=limit)
    return [item["payload"] for item in reranked]


def _trace_candidate(row: tuple) -> dict[str, Any]:
    candidate = candidate_from_row(row)
    return {"article_id": candidate["article"].article_id,
            **{key: candidate[key] for key in ("score", "text_score", "semantic_score", "exact_score",
                                                "match_score")}}


def lookup_candidates_by_query(
    query: str, *, days: int = 14, start_date: date | None = None, end_date: date | None = None,
    limit: int = 5, rerank_mode: str | None = None,
    topic: str | None = None, kinds: tuple[str, ...] | None = None,
    publisher_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return ranked article candidates in the shape of hybrid.candidate_from_row."""
    query_clean = (query or "").strip()
    if not query_clean:
        return []

    recall_profile = resolve_recall_profile()
    resolved_mode = resolve_rerank_mode(rerank_mode)
    days = _clamp_int(days, 1, 365)
    limit = _clamp_int(limit, 1, MAX_ARTICLE_LIMIT)
    start_at, end_at = time_window(days, start_date, end_date, now=datetime.now(timezone.utc))
    if resolved_mode == RERANK_MODE_NONE:
        candidate_pool_limit = limit
    else:
        candidate_pool_limit = min(
            limit * int(recall_profile.query_cand_multiplier),
            int(recall_profile.query_cand_max),
        )
    query_vec = _get_query_embedding(query_clean)

    conn = get_conn()
    try:
        cur = conn.cursor()
        span_input = {"query": query_clean, "start_at": start_at.isoformat(), "end_at": end_at.isoformat(),
                      "topic": topic, "kinds": list(kinds) if kinds else None, "publisher_kind": publisher_kind}
        span_attributes = {"pool_limit": candidate_pool_limit, "sim_floor": recall_profile.sim_floor,
                           "channels": ["lexical", "semantic", "exact"] if query_vec else ["lexical", "exact"]}
        with trace_span("retrieval", input=span_input, attributes=span_attributes) as span:
            rows = fetch_hybrid_rows(
                cur,
                query=query_clean,
                limit=candidate_pool_limit,
                query_vec=query_vec,
                start_at=start_at,
                end_at=end_at,
                topic=topic,
                kinds=kinds,
                publisher_kind=publisher_kind,
            )
            span.output = {"candidates": [_trace_candidate(row) for row in rows]}
        cur.close()
        ranked_rows = _finalize_candidate_rows(rows, query=query_clean, limit=limit, rerank_mode=resolved_mode)
        return [candidate_from_row(row) for row in ranked_rows]
    finally:
        put_conn(conn)


@dataclass(frozen=True)
class SearchResult:
    hits: list[StoryHit]
    weak_match: bool


def _related(lead: Article, members: list[Article]) -> tuple[Article, ...]:
    others = [m for m in members if m.article_id != lead.article_id]
    if len(others) > RELATED_LIMIT:
        # sorted() is stable, so each group keeps its time order.
        kept = sorted(others, key=lambda m: m.publisher_kind == lead.publisher_kind)[:RELATED_LIMIT]
        others = sorted(kept, key=lambda m: m.published_at)
    return tuple(others)


def search_stories(
    *, query: str | None, days: int, start_date: date | None, end_date: date | None,
    topic: str | None, kinds: tuple[str, ...] | None,
    publisher_kind: str | None, sort: str, limit: int,
) -> SearchResult:
    weak_match = False
    leads: dict[int, dict[str, Any]] = {}
    if query:
        candidates = lookup_candidates_by_query(
            query, days=days, start_date=start_date, end_date=end_date, limit=MAX_ARTICLE_LIMIT,
            topic=topic, kinds=kinds, publisher_kind=publisher_kind,
        )
        for candidate in candidates:
            leads.setdefault(candidate["article"].story_id, candidate)
            if len(leads) >= limit:
                break
        weak_match = bool(candidates) and candidates[0]["match_score"] < WEAK_MATCH_THRESHOLD

    conn = get_conn()
    try:
        cur = conn.cursor()
        if query:
            story_ids = list(leads)
        else:
            start_at, end_at = time_window(days, start_date, end_date, now=datetime.now(timezone.utc))
            story_ids = browse_story_ids(
                cur, start_at=start_at, end_at=end_at, topic=topic,
                kinds=kinds if kinds is not None else EVENT_KINDS,
                publisher_kind=publisher_kind, sort=sort, limit=limit,
            )
        members = fetch_story_members(cur, story_ids)
        stories = [
            (story_id, members[story_id], stats_from_members(story_id, members[story_id]))
            for story_id in story_ids
            if members.get(story_id)
        ]
        discussions = fetch_discussions(
            cur, [stats.hn_sighting_id for _, _, stats in stories if stats.hn_sighting_id is not None]
        )
        cur.close()
    finally:
        put_conn(conn)

    hits: list[StoryHit] = []
    for story_id, group, stats in stories:
        candidate = leads.get(story_id)
        lead = candidate["article"] if candidate else pick_lead(group)
        hits.append(StoryHit(
            lead=lead,
            stats=stats,
            related=_related(lead, group),
            discussion=discussions.get(stats.hn_sighting_id),
            match_score=candidate["match_score"] if candidate else None,
        ))
    if query and sort == "time":
        hits.sort(key=lambda hit: hit.lead.published_at, reverse=True)
    return SearchResult(hits=hits, weak_match=weak_match)
