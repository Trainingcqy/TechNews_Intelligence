"""Postgres-only hybrid retrieval primitives.

This module implements the shared SQL pipeline used by query-oriented retrieval:

    FTS rank + pgvector rank + exact match of the query + RRF fusion.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from .literal import exact_pattern, literal_match
from .recall_profile import resolve_recall_profile
from .stories import ARTICLE_COLUMNS, ARTICLE_JOINS, article_from_row


RRF_K = 60
LEXICAL_WEIGHT = 1.0
SEMANTIC_WEIGHT = 1.0
EXACT_WEIGHT = 0.7

_FRESHNESS_BOOST_WEIGHT = 0.0
_TRGM_THRESHOLD = 0.42

_FILTERS = """
              AND a.status = 'ready'
              AND (%(topic)s::text IS NULL OR a.topics @> ARRAY[%(topic)s::text])
              AND (%(kinds)s::text[] IS NULL OR a.kind = ANY(%(kinds)s::text[]))
              AND (%(publisher_kind)s::text IS NULL OR p.kind = %(publisher_kind)s)"""

_TRGM_HIT = """(similarity(LOWER(COALESCE(a.title, '')), LOWER(q.raw_query)) >= %(trgm_threshold)s
                     OR similarity(LOWER(COALESCE(a.title_zh, '')), LOWER(q.raw_query)) >= %(trgm_threshold)s)"""


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = minimum
    return max(minimum, min(maximum, n))


def _env_float(name: str, *, fallback: float, min_value: float, max_value: float) -> float:
    try:
        parsed = float(str(os.getenv(name, "")).strip())
    except Exception:
        return fallback
    return max(min_value, min(max_value, parsed))


def _vector_literal(query_vec: list[float] | None) -> str | None:
    if not query_vec:
        return None
    return "[" + ",".join(str(v) for v in query_vec) + "]"


def _profile_limits(limit: int) -> tuple[int, int, int]:
    recall_profile = resolve_recall_profile()
    pool_limit = _clamp_int(limit, 1, 500)
    channel_limit = max(pool_limit, min(pool_limit * 4, int(recall_profile.query_cand_max)))
    semantic_limit = max(pool_limit, min(pool_limit * int(recall_profile.query_cand_multiplier), int(recall_profile.query_cand_max)))
    return pool_limit, channel_limit, semantic_limit


def fetch_hybrid_rows(
    cur,
    *,
    query: str,
    limit: int,
    query_vec: list[float] | None,
    start_at: datetime,
    end_at: datetime,
    sim_floor: float | None = None,
    topic: str | None = None,
    kinds: tuple[str, ...] | None = None,
    publisher_kind: str | None = None,
) -> list[tuple]:
    """Execute the hybrid RRF SQL; each row is ARTICLE_COLUMNS followed by the five score columns."""
    query_clean = (query or "").strip()
    if not query_clean:
        return []

    result_limit, lexical_limit, semantic_limit = _profile_limits(limit)
    recall_profile = resolve_recall_profile()
    floor = sim_floor if sim_floor is not None else float(recall_profile.sim_floor)

    query_vec_literal = _vector_literal(query_vec)
    has_vector = bool(query_vec_literal)

    freshness_weight = _env_float(
        "RETRIEVAL_FRESHNESS_BOOST",
        fallback=_FRESHNESS_BOOST_WEIGHT,
        min_value=0.0,
        max_value=0.05,
    )
    trgm_threshold = _env_float(
        "RETRIEVAL_TRGM_THRESHOLD",
        fallback=_TRGM_THRESHOLD,
        min_value=0.1,
        max_value=0.9,
    )
    pattern = exact_pattern(query_clean)
    title_hit = "(" + " OR ".join(literal_match(column, pattern, "q.raw_query")
                                  for column in ("a.title", "COALESCE(a.title_zh, '')")) + ")"
    summary_hit = literal_match("COALESCE(a.summary, '')", pattern, "q.raw_query")

    semantic_cte = f"""
        semantic AS (
            SELECT
                a.article_id,
                ROW_NUMBER() OVER (ORDER BY e.embedding <=> %(query_vec)s::vector, a.event_at DESC) AS semantic_rank,
                (1 - (e.embedding <=> %(query_vec)s::vector))::float AS semantic_score
            FROM news.article a
            JOIN news.publisher p ON p.publisher_id = a.publisher_id
            JOIN news.article_embedding e ON e.article_id = a.article_id
            WHERE a.event_at >= %(start_at)s AND a.event_at < %(end_at)s{_FILTERS}
            ORDER BY e.embedding <=> %(query_vec)s::vector
            LIMIT %(semantic_limit)s
        )
    """ if has_vector else """
        semantic AS (
            SELECT
                NULL::bigint AS article_id,
                NULL::bigint AS semantic_rank,
                NULL::float AS semantic_score
            WHERE FALSE
        )
    """

    sql = f"""
        WITH q AS (
            SELECT
                %(query)s::text AS raw_query,
                (websearch_to_tsquery('english', %(query)s)
                    || websearch_to_tsquery('simple', %(query)s)) AS tsq
        ),
        lexical_ranked AS (
            SELECT
                a.article_id,
                ts_rank_cd(a.search_tsv, q.tsq, 32)::float AS text_score,
                a.event_at
            FROM q
            JOIN news.article a ON numnode(q.tsq) > 0 AND a.search_tsv @@ q.tsq
            JOIN news.publisher p ON p.publisher_id = a.publisher_id
            WHERE a.event_at >= %(start_at)s AND a.event_at < %(end_at)s{_FILTERS}
            ORDER BY text_score DESC, a.event_at DESC
            LIMIT %(lexical_limit)s
        ),
        lexical AS (
            SELECT
                article_id,
                ROW_NUMBER() OVER (ORDER BY text_score DESC, event_at DESC) AS lexical_rank,
                text_score
            FROM lexical_ranked
        ),
        {semantic_cte},
        exact_source AS (
            SELECT
                a.article_id,
                (CASE
                    WHEN {title_hit} THEN 2.0
                    WHEN {_TRGM_HIT} THEN 1.4
                    WHEN {summary_hit} THEN 0.8
                    ELSE 0.0
                END)::float AS exact_score,
                a.event_at
            FROM q
            JOIN news.article a ON {title_hit} OR {_TRGM_HIT} OR {summary_hit}
            JOIN news.publisher p ON p.publisher_id = a.publisher_id
            WHERE a.event_at >= %(start_at)s AND a.event_at < %(end_at)s{_FILTERS}
            ORDER BY exact_score DESC, a.event_at DESC
            LIMIT %(lexical_limit)s
        ),
        exact AS (
            SELECT
                article_id,
                ROW_NUMBER() OVER (ORDER BY exact_score DESC, event_at DESC) AS exact_rank,
                exact_score
            FROM exact_source
        ),
        fused_ids AS (
            SELECT article_id FROM lexical
            UNION
            SELECT article_id FROM semantic
            UNION
            SELECT article_id FROM exact
        ),
        fused AS (
            SELECT
                u.article_id,
                (
                    COALESCE(%(lexical_weight)s::float / (%(rrf_k)s::float + l.lexical_rank::float), 0.0)
                  + COALESCE(%(semantic_weight)s::float / (%(rrf_k)s::float + s.semantic_rank::float), 0.0)
                  + COALESCE(%(exact_weight)s::float / (%(rrf_k)s::float + x.exact_rank::float), 0.0)
                )::float AS rrf_score,
                COALESCE(l.text_score, 0.0)::float AS text_score,
                COALESCE(s.semantic_score, 0.0)::float AS semantic_score,
                COALESCE(x.exact_score, 0.0)::float AS exact_score
            FROM fused_ids u
            LEFT JOIN lexical l ON l.article_id = u.article_id
            LEFT JOIN semantic s ON s.article_id = u.article_id
            LEFT JOIN exact x ON x.article_id = u.article_id
        )
        SELECT
            {ARTICLE_COLUMNS},
            (
                f.rrf_score
                + %(freshness_weight)s::float
                    * EXP(-EXTRACT(EPOCH FROM (%(end_at)s - a.event_at)) / 86400.0
                          / GREATEST(EXTRACT(EPOCH FROM (%(end_at)s - %(start_at)s)) / 86400.0, 1.0))
            )::float AS score,
            COALESCE(f.text_score, 0.0)::float AS text_score,
            COALESCE(f.semantic_score, 0.0)::float AS semantic_score,
            COALESCE(f.exact_score, 0.0)::float AS exact_score,
            GREATEST(
                COALESCE(f.semantic_score, 0.0)::float,
                CASE
                    WHEN COALESCE(f.exact_score, 0.0) > 0.0
                        THEN (COALESCE(f.exact_score, 0.0)::float / (1.0 + COALESCE(f.exact_score, 0.0)::float))
                    ELSE 0.0
                END,
                CASE
                    WHEN COALESCE(f.text_score, 0.0) > 0.0
                        THEN (COALESCE(f.text_score, 0.0)::float / (1.0 + COALESCE(f.text_score, 0.0)::float))
                    ELSE 0.0
                END
            )::float AS match_score
        FROM fused f
        JOIN news.article a ON a.article_id = f.article_id
        {ARTICLE_JOINS}
        WHERE (
            %(sim_floor)s::float <= 0.0
            OR f.semantic_score = 0.0
            OR f.semantic_score >= %(sim_floor)s::float
            OR f.text_score > 0.0
            OR f.exact_score > 0.0
        )
        ORDER BY score DESC, a.event_at DESC
        LIMIT %(result_limit)s
    """

    cur.execute(
        sql,
        {
            "query": query_clean,
            "start_at": start_at,
            "end_at": end_at,
            "topic": topic,
            # psycopg2 adapts a tuple to a row literal, not an array.
            "kinds": list(kinds) if kinds is not None else None,
            "publisher_kind": publisher_kind,
            "query_vec": query_vec_literal,
            "lexical_limit": lexical_limit,
            "semantic_limit": semantic_limit,
            "result_limit": result_limit,
            "rrf_k": RRF_K,
            "lexical_weight": LEXICAL_WEIGHT,
            "semantic_weight": SEMANTIC_WEIGHT,
            "exact_weight": EXACT_WEIGHT,
            "freshness_weight": freshness_weight,
            "trgm_threshold": trgm_threshold,
            "exact_pattern": pattern,
            "sim_floor": floor,
        },
    )
    return list(cur.fetchall())


def candidate_from_row(row: tuple) -> dict[str, Any]:
    """{"article": Article, "score": float, "text_score": float,
        "semantic_score": float, "exact_score": float, "match_score": float,
        "title": str, "summary": str, "source_type": str, "url": str}"""
    article = article_from_row(row)
    score, text_score, semantic_score, exact_score, match_score = (float(v or 0.0) for v in row[-5:])
    return {
        "article": article,
        "score": score,
        "text_score": text_score,
        "semantic_score": semantic_score,
        "exact_score": exact_score,
        "match_score": match_score,
        "title": article.title_zh or article.title,
        "summary": article.summary,
        "source_type": article.publisher,
        "url": article.url,
    }
