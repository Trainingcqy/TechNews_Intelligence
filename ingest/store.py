"""All SQL of the collection pipeline. Functions take a cursor; the caller owns the transaction."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg2.extras import Json, execute_values

from ingest.config import IngestConfig
from ingest.models import Analysis, Candidate
from ingest.sources import Publisher, Sources, resolve_publisher
from services.taxonomy import TAXONOMY_VERSION
from services.urls import url_key

_FEED_FULL_BODY_CHARS = 1500
_HN_STORY_BODY_CHARS = 200
_EXCERPT_CHARS = 200

_ADVISORY_LOCK_KEY = 0x6E657773  # ASCII "news"

# Passed to the stages: each call opens a transaction and yields its cursor, committing on a normal exit
# and rolling back on an exception.
Transaction = Callable[[], AbstractContextManager]

_WAITING_STATUS = {"fetch": "discovered", "analyze": "fetched", "embed": "analyzed"}

_SET_STATE = (
    "status = %(status)s, attempts = %(attempts)s, error_kind = %(error_kind)s, "
    "next_attempt_at = now() + make_interval(mins => %(delay_minutes)s), updated_at = now()"
)


def _state(status: str, attempts: int = 0, error_kind: str | None = None, delay_minutes: int = 0) -> dict:
    return {"status": status, "attempts": attempts, "error_kind": error_kind, "delay_minutes": delay_minutes}


@dataclass(frozen=True)
class DueChannel:
    channel_id: int
    key: str
    fetch_kind: str
    endpoint: str
    purpose: str
    lang: str
    config: dict
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class FetchItem:
    article_id: int
    url: str
    title: str


@dataclass(frozen=True)
class AnalyzeItem:
    article_id: int
    title: str
    publisher: str
    body: str
    body_level: str


@dataclass(frozen=True)
class EmbedItem:
    article_id: int
    summary: str


@dataclass(frozen=True)
class NearestStory:
    article_id: int
    story_id: int
    similarity: float
    span_after: timedelta  # span of the story if the article joined it


@dataclass(frozen=True)
class DiscussItem:
    sighting_id: int
    external_id: str


@dataclass(frozen=True)
class WindowCounts:
    """Monitor counts over the articles first seen in one 24-hour window."""

    articles: int = 0
    ready: int = 0
    unlabeled: int = 0  # ready with empty topics or kind
    ascii_quote: int = 0  # ready with an ASCII double quote in the summary
    too_long: int = 0  # ready with a summary over 200 characters
    analyze_failed: int = 0  # failed, with analyze as the stage of the latest failure
    fetched: int = 0  # reached fetched: every status but discovered
    degraded: int = 0  # reached fetched with body_level other than full


@dataclass(frozen=True)
class LegacyNews:
    id: int
    url: str
    title: str
    summary: str | None
    title_cn: str | None
    points: int | None
    source_id: str | None
    created_at: datetime  # naive, UTC
    source_key: str


@dataclass(frozen=True)
class LegacyFailed:
    id: int
    url: str
    title: str
    created_at: datetime
    source_key: str | None


@dataclass(frozen=True)
class MigratedArticle:
    url: str
    url_key: str
    lang: str | None
    title: str
    title_zh: str | None
    summary: str | None
    topics: tuple[str, ...]
    label_source: str | None
    first_seen_at: datetime
    published_at: datetime | None
    body_level: str
    status: str


@dataclass(frozen=True)
class MigratedSighting:
    url_key: str
    channel_key: str
    external_id: str | None
    title: str
    first_seen_at: datetime
    last_seen_at: datetime
    score: int | None


@dataclass(frozen=True)
class RelabelItem:
    article_id: int
    title: str
    summary: str


def sync_sources(cur, sources: Sources) -> None:
    for publisher in sources.publishers:
        cur.execute(
            "INSERT INTO news.publisher (name, kind) VALUES (%(name)s, %(kind)s) "
            "ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind RETURNING publisher_id",
            {"name": publisher.name, "kind": publisher.kind},
        )
        publisher_id = cur.fetchone()[0]
        cur.execute("DELETE FROM news.publisher_host WHERE publisher_id = %s", (publisher_id,))
        for host, path_prefix in publisher.hosts:
            cur.execute(
                "INSERT INTO news.publisher_host (host, path_prefix, publisher_id) VALUES (%s, %s, %s) "
                "ON CONFLICT (host, path_prefix) DO UPDATE SET publisher_id = EXCLUDED.publisher_id",
                (host, path_prefix, publisher_id),
            )

    names = [publisher.name for publisher in sources.publishers]
    # Publishers dropped from sources.toml lose their entries. An auto-created publisher keeps its entry
    # unless sources.toml now registers the same domain, or a parent domain, without a path prefix.
    cur.execute(
        """
        DELETE FROM news.publisher_host h
        USING news.publisher p
        WHERE p.publisher_id = h.publisher_id
          AND p.name <> ALL(%(names)s::text[])
          AND NOT (
              p.kind = 'unknown' AND h.host = p.name AND h.path_prefix = ''
              AND NOT EXISTS (
                  SELECT 1 FROM unnest(%(domains)s::text[]) AS d (host)
                  WHERE h.host = d.host OR right(h.host, length(d.host) + 1) = '.' || d.host
              )
          )
        """,
        {"names": names, "domains": [host for host, path_prefix in sources.hosts if not path_prefix]},
    )
    _reresolve_auto_articles(cur, names)

    for channel in sources.channels:
        cur.execute(
            """
            INSERT INTO news.channel AS c
                (key, name, fetch_kind, endpoint, purpose, publisher_id, lang, enabled, config)
            VALUES (%(key)s, %(name)s, %(fetch_kind)s, %(endpoint)s, %(purpose)s,
                    (SELECT publisher_id FROM news.publisher WHERE name = %(publisher)s),
                    %(lang)s, %(enabled)s, %(config)s)
            ON CONFLICT (key) DO UPDATE SET
                name = EXCLUDED.name, fetch_kind = EXCLUDED.fetch_kind, endpoint = EXCLUDED.endpoint,
                purpose = EXCLUDED.purpose, publisher_id = EXCLUDED.publisher_id, lang = EXCLUDED.lang,
                enabled = EXCLUDED.enabled, config = EXCLUDED.config, updated_at = now()
            WHERE (c.name, c.fetch_kind, c.endpoint, c.purpose, c.publisher_id, c.lang, c.enabled, c.config)
                IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.fetch_kind, EXCLUDED.endpoint, EXCLUDED.purpose,
                                  EXCLUDED.publisher_id, EXCLUDED.lang, EXCLUDED.enabled, EXCLUDED.config)
            """,
            {
                "key": channel.key,
                "name": channel.name,
                "fetch_kind": channel.fetch_kind,
                "endpoint": channel.endpoint,
                "purpose": channel.purpose,
                "publisher": channel.publisher,
                "lang": channel.lang,
                "enabled": channel.enabled,
                "config": Json(channel.config),
            },
        )
    cur.execute(
        "UPDATE news.channel SET enabled = false, updated_at = now() WHERE enabled AND key <> ALL(%s::text[])",
        ([channel.key for channel in sources.channels],),
    )


def _reresolve_auto_articles(cur, toml_names: list[str]) -> None:
    """Moves the articles of auto-created publishers left without an entry to the publisher they resolve to now."""
    cur.execute(
        """
        SELECT a.article_id, a.url
        FROM news.article a
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        WHERE p.kind = 'unknown' AND p.name <> ALL(%s::text[])
          AND NOT EXISTS (SELECT 1 FROM news.publisher_host h WHERE h.publisher_id = p.publisher_id)
        """,
        (toml_names,),
    )
    rows = cur.fetchall()
    if not rows:
        return
    hosts = load_publisher_hosts(cur)
    for article_id, url in rows:
        cur.execute(
            "UPDATE news.article SET publisher_id = (SELECT publisher_id FROM news.publisher WHERE name = %s), "
            "updated_at = now() WHERE article_id = %s",
            (resolve_publisher(url, hosts).name, article_id),
        )


def load_publisher_hosts(cur) -> dict[tuple[str, str], Publisher]:
    cur.execute(
        "SELECT p.name, p.kind, h.host, h.path_prefix "
        "FROM news.publisher_host h JOIN news.publisher p USING (publisher_id) "
        "ORDER BY p.name, h.host, h.path_prefix"
    )
    entries: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for name, kind, host, path_prefix in cur.fetchall():
        entries.setdefault((name, kind), []).append((host, path_prefix))
    hosts: dict[tuple[str, str], Publisher] = {}
    for (name, kind), items in entries.items():
        publisher = Publisher(name=name, kind=kind, hosts=tuple(items))
        for item in items:
            hosts[item] = publisher
    return hosts


def ensure_auto_publisher(cur, host: str) -> None:
    cur.execute(
        "INSERT INTO news.publisher (name, kind) VALUES (%s, 'unknown') ON CONFLICT (name) DO NOTHING",
        (host,),
    )
    cur.execute(
        "INSERT INTO news.publisher_host (host, path_prefix, publisher_id) "
        "SELECT %s, '', publisher_id FROM news.publisher WHERE name = %s "
        "ON CONFLICT (host, path_prefix) DO NOTHING",
        (host, host),
    )


def due_channels(cur) -> list[DueChannel]:
    cur.execute(
        """
        SELECT channel_id, key, fetch_kind, endpoint, purpose, lang, config, etag, last_modified
        FROM news.channel
        WHERE enabled
          AND (last_polled_at IS NULL OR last_polled_at <= now() - make_interval(mins => poll_minutes))
        ORDER BY key
        """
    )
    return [DueChannel(*row) for row in cur.fetchall()]


def enabled_channel(cur, key: str) -> DueChannel | None:
    cur.execute(
        "SELECT channel_id, key, fetch_kind, endpoint, purpose, lang, config, etag, last_modified "
        "FROM news.channel WHERE enabled AND key = %s",
        (key,),
    )
    row = cur.fetchone()
    return DueChannel(*row) if row else None


def _initial_state(channel: DueChannel, candidate: Candidate) -> tuple[str, str, str | None]:
    """Status, body_level and body source of a newly discovered article."""
    text = candidate.feed_text or ""
    if channel.fetch_kind == "hn" and candidate.url == f"https://news.ycombinator.com/item?id={candidate.external_id}":
        if len(text) >= _HN_STORY_BODY_CHARS:
            return "fetched", "full", "hn"
        return "fetched", "none", None
    if channel.config.get("body_from_feed") and len(text) >= _FEED_FULL_BODY_CHARS:
        return "fetched", "full", "feed"
    if len(text) >= _EXCERPT_CHARS:
        return "discovered", "excerpt", "feed"
    return "discovered", "none", None


def _write_body(cur, article_id: int, source: str, text: str) -> None:
    cur.execute(
        "INSERT INTO news.article_body (article_id, source, text, char_count) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (article_id) DO UPDATE SET source = EXCLUDED.source, text = EXCLUDED.text, "
        "char_count = EXCLUDED.char_count, fetched_at = now()",
        (article_id, source, text, len(text)),
    )


def insert_candidate(cur, channel: DueChannel, candidate: Candidate, publisher_name: str) -> bool:
    """Steps 4-8 of candidate ingestion. Returns whether the article is new."""
    status, body_level, body_source = _initial_state(channel, candidate)
    key = url_key(candidate.url)
    cur.execute(
        """
        INSERT INTO news.article (url, url_key, publisher_id, lang, title, published_at, status, body_level)
        VALUES (%(url)s, %(url_key)s, (SELECT publisher_id FROM news.publisher WHERE name = %(publisher)s),
                %(lang)s, %(title)s, %(published_at)s, %(status)s, %(body_level)s)
        ON CONFLICT (url_key) DO NOTHING
        RETURNING article_id
        """,
        {
            "url": candidate.url,
            "url_key": key,
            "publisher": publisher_name,
            "lang": channel.lang,
            "title": candidate.title,
            "published_at": candidate.published_at,
            "status": status,
            "body_level": body_level,
        },
    )
    row = cur.fetchone()
    is_new = row is not None
    if is_new:
        article_id = row[0]
        if body_source is not None:
            _write_body(cur, article_id, body_source, candidate.feed_text)
    else:
        cur.execute("SELECT article_id FROM news.article WHERE url_key = %s", (key,))
        article_id = cur.fetchone()[0]

    # A URL submitted to HackerNews several times keeps one sighting, that of the highest-scoring submission.
    adopt = "(EXCLUDED.external_id IS NOT DISTINCT FROM s.external_id OR EXCLUDED.score > s.score)"
    cur.execute(
        f"""
        INSERT INTO news.sighting AS s
            (article_id, channel_id, external_id, title, first_seen_at, score, comment_count)
        VALUES (%(article_id)s, %(channel_id)s, %(external_id)s, %(title)s,
                COALESCE(%(channel_time)s, now()), %(score)s, %(comment_count)s)
        ON CONFLICT (channel_id, article_id) DO UPDATE SET
            last_seen_at = now(),
            external_id = CASE WHEN {adopt} THEN EXCLUDED.external_id ELSE s.external_id END,
            title = CASE WHEN {adopt} THEN EXCLUDED.title ELSE s.title END,
            score = CASE WHEN {adopt} THEN EXCLUDED.score ELSE s.score END,
            comment_count = CASE WHEN {adopt} THEN EXCLUDED.comment_count ELSE s.comment_count END
        """,
        {
            "article_id": article_id,
            "channel_id": channel.channel_id,
            "external_id": candidate.external_id,
            "title": candidate.title,
            "channel_time": candidate.channel_time,
            "score": candidate.score,
            "comment_count": candidate.comment_count,
        },
    )
    return is_new


def record_poll(
    cur,
    channel_id: int,
    *,
    polled_at: datetime,
    ok: bool,
    http_status: int | None,
    items_seen: int,
    items_new: int,
    error: str | None,
    etag: str | None,
    last_modified: str | None,
) -> int:
    """Returns the new fail_streak. etag or last_modified of None keeps the stored value."""
    cur.execute(
        "INSERT INTO news.channel_poll (channel_id, polled_at, ok, http_status, items_seen, items_new, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (channel_id, polled_at, ok, http_status, items_seen, items_new, error),
    )
    cur.execute(
        """
        UPDATE news.channel SET
            etag = COALESCE(%(etag)s, etag),
            last_modified = COALESCE(%(last_modified)s, last_modified),
            last_polled_at = now(),
            fail_streak = CASE WHEN %(ok)s THEN 0 ELSE fail_streak + 1 END,
            last_ok_at = CASE WHEN %(ok)s THEN now() ELSE last_ok_at END,
            updated_at = now()
        WHERE channel_id = %(channel_id)s
        RETURNING fail_streak
        """,
        {"etag": etag, "last_modified": last_modified, "ok": ok, "channel_id": channel_id},
    )
    return cur.fetchone()[0]


def insert_reference_headline(
    cur,
    channel_id: int,
    *,
    external_id: str,
    title: str,
    target_url: str | None,
    outlet: str | None,
    published_at: datetime | None,
) -> bool:
    """Returns whether the headline is new."""
    cur.execute(
        """
        INSERT INTO news.reference_headline
            (channel_id, external_id, title, target_url, target_url_key, outlet, published_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (channel_id, external_id) DO NOTHING
        """,
        (channel_id, external_id, title, target_url, url_key(target_url) if target_url else None, outlet, published_at),
    )
    return cur.rowcount == 1


def take_fetch(cur, limit: int) -> list[FetchItem]:
    cur.execute(
        "SELECT article_id, url, title FROM news.article "
        "WHERE status = 'discovered' AND next_attempt_at <= now() ORDER BY first_seen_at LIMIT %s",
        (limit,),
    )
    return [FetchItem(*row) for row in cur.fetchall()]


def take_analyze(cur, limit: int) -> list[AnalyzeItem]:
    cur.execute(
        """
        SELECT a.article_id, a.title, p.name, COALESCE(b.text, ''), a.body_level
        FROM news.article a
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        LEFT JOIN news.article_body b ON b.article_id = a.article_id
        WHERE a.status = 'fetched' AND a.next_attempt_at <= now()
        ORDER BY a.first_seen_at
        LIMIT %s
        """,
        (limit,),
    )
    return [AnalyzeItem(*row) for row in cur.fetchall()]


def take_embed(cur) -> list[EmbedItem]:
    cur.execute(
        "SELECT article_id, summary FROM news.article "
        "WHERE status = 'analyzed' AND next_attempt_at <= now() ORDER BY first_seen_at"
    )
    return [EmbedItem(*row) for row in cur.fetchall()]


def record_fetch_success(
    cur, article_id: int, *, body: str, published_at: datetime | None, title: str | None
) -> None:
    """published_at fills an empty value only; title, when given, replaces the stored placeholder."""
    _write_body(cur, article_id, "reader", body)
    cur.execute(
        f"""
        UPDATE news.article SET
            title = COALESCE(%(title)s, title),
            published_at = COALESCE(published_at, %(published_at)s),
            body_level = 'full',
            {_SET_STATE}
        WHERE article_id = %(article_id)s
        """,
        {"article_id": article_id, "title": title, "published_at": published_at, **_state("fetched")},
    )


def record_analyze_success(cur, article_id: int, analysis: Analysis) -> None:
    cur.execute(
        f"""
        UPDATE news.article SET
            title_zh = %(title_zh)s, summary = %(summary)s, topics = %(topics)s, kind = %(kind)s,
            taxonomy_version = %(taxonomy_version)s, label_source = 'model',
            {_SET_STATE}
        WHERE article_id = %(article_id)s
        """,
        {
            "article_id": article_id,
            "title_zh": analysis.title_zh,
            "summary": analysis.summary,
            "topics": list(analysis.topics),
            "kind": analysis.kind,
            "taxonomy_version": TAXONOMY_VERSION,
            **_state("analyzed"),
        },
    )


def record_embed_success(cur, article_id: int, embedding: list[float], model: str) -> None:
    cur.execute(
        "INSERT INTO news.article_embedding (article_id, embedding, model) VALUES (%s, %s::vector, %s) "
        "ON CONFLICT (article_id) DO UPDATE SET embedding = EXCLUDED.embedding, model = EXCLUDED.model, "
        "created_at = now()",
        (article_id, "[" + ",".join(str(value) for value in embedding) + "]", model),
    )
    cur.execute(
        f"UPDATE news.article SET {_SET_STATE} WHERE article_id = %(article_id)s",
        {"article_id": article_id, **_state("embedded")},
    )


def record_failure(
    cur, article_id: int, stage: str, error_kind: str, detail: str | None, config: IngestConfig
) -> None:
    max_attempts = {
        "fetch": config.fetch_max_attempts,
        "analyze": config.analyze_max_attempts,
        "embed": config.embed_max_attempts,
    }[stage]
    cur.execute("SELECT attempts FROM news.article WHERE article_id = %s FOR UPDATE", (article_id,))
    attempts = cur.fetchone()[0] + 1
    backoff = config.backoff_minutes[min(attempts, len(config.backoff_minutes)) - 1]
    if attempts < max_attempts:
        state = _state(_WAITING_STATUS[stage], attempts, error_kind, backoff)
    elif stage == "fetch":
        # body_level is already excerpt or none: a discovered article only has a body when it is an excerpt.
        state = _state("fetched", 0, error_kind)
    else:
        state = _state("failed", attempts, error_kind, backoff)
    cur.execute(
        f"UPDATE news.article SET {_SET_STATE} WHERE article_id = %(article_id)s",
        {"article_id": article_id, **state},
    )
    cur.execute(
        "INSERT INTO news.stage_failure (article_id, stage, error_kind, detail) VALUES (%s, %s, %s, %s)",
        (article_id, stage, error_kind, detail),
    )


def requeue_failed(cur, error_kind: str | None) -> int:
    """Returns failed articles to the status of the stage that failed them. Returns the number requeued."""
    cur.execute(
        """
        UPDATE news.article a
        SET status = m.status, attempts = 0, next_attempt_at = now(), updated_at = now()
        FROM (
            SELECT DISTINCT ON (f.article_id) f.article_id, f.stage
            FROM news.stage_failure f
            JOIN news.article fa ON fa.article_id = f.article_id AND fa.status = 'failed'
            ORDER BY f.article_id, f.failure_id DESC
        ) last
        JOIN unnest(%(stages)s::text[], %(statuses)s::text[]) AS m (stage, status) ON m.stage = last.stage
        WHERE a.article_id = last.article_id
          AND a.status = 'failed'
          AND (%(error_kind)s::text IS NULL OR a.error_kind = %(error_kind)s)
        """,
        {
            "stages": list(_WAITING_STATUS),
            "statuses": list(_WAITING_STATUS.values()),
            "error_kind": error_kind,
        },
    )
    return cur.rowcount


def take_link(cur) -> list[int]:
    cur.execute(
        "SELECT article_id FROM news.article "
        "WHERE status = 'embedded' AND next_attempt_at <= now() ORDER BY event_at, article_id"
    )
    return [row[0] for row in cur.fetchall()]


def story_of(cur, article_id: int) -> int | None:
    cur.execute("SELECT story_id FROM news.story_member WHERE article_id = %s", (article_id,))
    row = cur.fetchone()
    return row[0] if row else None


def nearest_story(cur, article_id: int, *, window_days: int) -> NearestStory | None:
    """The nearest article with a story within window_days of the article's event time, by exact cosine distance."""
    cur.execute(
        "SELECT a.event_at, e.embedding::text FROM news.article a "
        "JOIN news.article_embedding e ON e.article_id = a.article_id WHERE a.article_id = %s",
        (article_id,),
    )
    event_at, embedding = cur.fetchone()
    cur.execute(
        """
        WITH cand AS MATERIALIZED (
            SELECT c.article_id, m.story_id, e.embedding
            FROM news.article c
            JOIN news.article_embedding e ON e.article_id = c.article_id
            JOIN news.story_member m ON m.article_id = c.article_id
            WHERE c.article_id <> %(article_id)s
              AND c.event_at BETWEEN %(event_at)s - make_interval(days => %(window_days)s)
                                 AND %(event_at)s + make_interval(days => %(window_days)s)
        ),
        nearest AS (
            SELECT article_id, story_id, (1 - (embedding <=> %(embedding)s::vector))::real AS similarity
            FROM cand
            ORDER BY embedding <=> %(embedding)s::vector, article_id
            LIMIT 1
        )
        SELECT n.article_id, n.story_id, n.similarity,
               GREATEST(MAX(a.event_at), %(event_at)s) - LEAST(MIN(a.event_at), %(event_at)s) AS span_after
        FROM nearest n
        JOIN news.story_member m ON m.story_id = n.story_id
        JOIN news.article a ON a.article_id = m.article_id
        GROUP BY n.article_id, n.story_id, n.similarity
        """,
        {"article_id": article_id, "event_at": event_at, "embedding": embedding, "window_days": window_days},
    )
    row = cur.fetchone()
    return NearestStory(*row) if row else None


def next_story_id(cur) -> int:
    cur.execute("SELECT nextval('news.story_id_seq')")
    return cur.fetchone()[0]


def record_story_member(
    cur, article_id: int, story_id: int, nearest_article_id: int | None, nearest_similarity: float | None
) -> None:
    cur.execute(
        "INSERT INTO news.story_member (article_id, story_id, nearest_article_id, nearest_similarity) "
        "VALUES (%s, %s, %s, %s)",
        (article_id, story_id, nearest_article_id, nearest_similarity),
    )


def mark_ready(cur, article_id: int) -> None:
    cur.execute(
        f"UPDATE news.article SET {_SET_STATE} WHERE article_id = %(article_id)s",
        {"article_id": article_id, **_state("ready")},
    )


def take_discuss(
    cur, limit: int, *, default_after_hours: int, default_min_comments: int, max_age_days: int
) -> list[DiscussItem]:
    """HackerNews sightings due for a discussion visit; the thresholds come from the channel config."""
    cur.execute(
        """
        SELECT s.sighting_id, s.external_id
        FROM news.sighting s
        JOIN news.channel c ON c.channel_id = s.channel_id
        WHERE c.fetch_kind = 'hn'
          AND s.external_id IS NOT NULL
          AND s.first_seen_at <= now() - make_interval(
                hours => COALESCE((c.config ->> 'discussion_after_hours')::int, %(after_hours)s))
          AND s.first_seen_at > now() - make_interval(days => %(max_age_days)s)
          AND s.comment_count >= COALESCE((c.config ->> 'discussion_min_comments')::int, %(min_comments)s)
          AND NOT EXISTS (SELECT 1 FROM news.discussion d WHERE d.sighting_id = s.sighting_id)
        ORDER BY s.first_seen_at
        LIMIT %(limit)s
        """,
        {
            "after_hours": default_after_hours,
            "min_comments": default_min_comments,
            "max_age_days": max_age_days,
            "limit": limit,
        },
    )
    return [DiscussItem(*row) for row in cur.fetchall()]


def record_discussion(
    cur, sighting_id: int, *, url: str, comment_total: int, text: str, score: int | None
) -> None:
    cur.execute(
        "INSERT INTO news.discussion (sighting_id, url, comment_total, text, char_count) VALUES (%s, %s, %s, %s, %s)",
        (sighting_id, url, comment_total, text, len(text)),
    )
    cur.execute(
        "UPDATE news.sighting SET score = COALESCE(%s, score), comment_count = %s WHERE sighting_id = %s",
        (score, comment_total, sighting_id),
    )


def monitor_counts(cur, windows: int) -> tuple[int, dict[int, WindowCounts]]:
    """Whole days of polling before the last 24 hours, and the counts of windows 0..windows-1.

    Window 0 is the last 24 hours and window k the 24 hours before window k-1. Only articles first seen
    at or after the earliest poll count, which leaves out the migrated ones.
    """
    cur.execute("SELECT floor(extract(epoch FROM now() - min(polled_at)) / 86400)::int - 1 FROM news.channel_poll")
    polled_days = cur.fetchone()[0]
    cur.execute(
        """
        WITH scope AS (SELECT min(polled_at) AS start FROM news.channel_poll)
        SELECT floor(extract(epoch FROM now() - a.first_seen_at) / 86400)::int AS day,
               count(*),
               count(*) FILTER (WHERE a.status = 'ready'),
               count(*) FILTER (WHERE a.status = 'ready' AND (cardinality(a.topics) = 0 OR a.kind IS NULL)),
               count(*) FILTER (WHERE a.status = 'ready' AND a.summary ~ '"'),
               count(*) FILTER (WHERE a.status = 'ready' AND char_length(a.summary) > 200),
               count(*) FILTER (WHERE a.status = 'failed' AND f.stage = 'analyze'),
               count(*) FILTER (WHERE a.status <> 'discovered'),
               count(*) FILTER (WHERE a.status <> 'discovered' AND a.body_level <> 'full')
        FROM news.article a
        CROSS JOIN scope
        LEFT JOIN LATERAL (
            SELECT sf.stage FROM news.stage_failure sf
            WHERE sf.article_id = a.article_id
            ORDER BY sf.failure_id DESC
            LIMIT 1
        ) f ON true
        WHERE a.first_seen_at >= scope.start
          AND a.first_seen_at > now() - make_interval(days => %s)
          AND a.first_seen_at <= now()
        GROUP BY 1
        """,
        (windows,),
    )
    counts = {row[0]: WindowCounts(*row[1:]) for row in cur.fetchall()}
    return max(polled_days or 0, 0), counts


def failing_channels(cur, min_streak: int) -> list[str]:
    cur.execute(
        "SELECT key FROM news.channel WHERE enabled AND fail_streak >= %s ORDER BY key",
        (min_streak,),
    )
    return [row[0] for row in cur.fetchall()]


def channel_last_ok(cur) -> list[tuple[str, datetime | None]]:
    cur.execute("SELECT key, last_ok_at FROM news.channel WHERE enabled ORDER BY key")
    return cur.fetchall()


def backlog_counts(cur) -> list[tuple[str, int]]:
    cur.execute(
        "SELECT status, count(*) FROM news.article WHERE status <> 'ready' GROUP BY status ORDER BY status"
    )
    return cur.fetchall()


def recent_failure_counts(cur) -> list[tuple[str, str, int]]:
    cur.execute(
        "SELECT stage, error_kind, count(*) FROM news.stage_failure "
        "WHERE created_at >= now() - interval '24 hours' GROUP BY stage, error_kind ORDER BY stage, error_kind"
    )
    return cur.fetchall()


def legacy_news(cur) -> list[LegacyNews]:
    cur.execute(
        "SELECT id, url, title, summary, title_cn, points, source_id, created_at, source_key "
        "FROM tech_news ORDER BY id"
    )
    return [LegacyNews(*row) for row in cur.fetchall()]


def legacy_failed(cur) -> list[LegacyFailed]:
    cur.execute("SELECT id, url, original_title, created_at, source_key FROM tech_news_failed ORDER BY id")
    return [LegacyFailed(*row) for row in cur.fetchall()]


def legacy_embedded_urls(cur) -> set[str]:
    cur.execute("SELECT url FROM news_embeddings WHERE embedding IS NOT NULL")
    return {row[0] for row in cur.fetchall()}


def legacy_bodies(cur, urls: list[str]) -> Iterator[tuple[str, str]]:
    """Yields (url, raw Reader output). Streamed through a server-side cursor: the table is several hundred MB."""
    with cur.connection.cursor(name="legacy_bodies") as named:
        named.itersize = 200
        named.execute(
            "SELECT url, raw_content FROM jina_raw_logs WHERE url = ANY(%s) AND raw_content IS NOT NULL", (urls,)
        )
        yield from named


def channel_langs(cur) -> dict[str, str]:
    cur.execute("SELECT key, lang FROM news.channel")
    return dict(cur.fetchall())


def existing_url_keys(cur, keys: list[str]) -> set[str]:
    cur.execute("SELECT url_key FROM news.article WHERE url_key = ANY(%s)", (keys,))
    return {row[0] for row in cur.fetchall()}


def existing_sightings(cur, keys: list[str]) -> set[tuple[str, str]]:
    """(channel key, url_key) of the sightings of the given articles."""
    cur.execute(
        "SELECT c.key, a.url_key FROM news.sighting s "
        "JOIN news.article a ON a.article_id = s.article_id JOIN news.channel c ON c.channel_id = s.channel_id "
        "WHERE a.url_key = ANY(%s)",
        (keys,),
    )
    return {(key, article_key) for key, article_key in cur.fetchall()}


def insert_migrated_articles(cur, rows: list[tuple[MigratedArticle, str]]) -> dict[str, int]:
    """rows are (article, publisher name). Returns url_key -> article_id of the inserted articles."""
    inserted = execute_values(
        cur,
        """
        INSERT INTO news.article
            (url, url_key, publisher_id, lang, title, title_zh, summary, topics, label_source,
             first_seen_at, published_at, body_level, status)
        VALUES %s
        ON CONFLICT (url_key) DO NOTHING
        RETURNING url_key, article_id
        """,
        [
            (
                a.url, a.url_key, publisher, a.lang, a.title, a.title_zh, a.summary, list(a.topics),
                a.label_source, a.first_seen_at, a.published_at, a.body_level, a.status,
            )
            for a, publisher in rows
        ],
        template="(%s, %s, (SELECT publisher_id FROM news.publisher WHERE name = %s), %s, %s, %s, %s, "
        "%s::text[], %s, %s, %s, %s, %s)",
        page_size=500,
        fetch=True,
    )
    return dict(inserted)


def insert_migrated_bodies(cur, rows: list[tuple[int, str]]) -> int:
    """rows are (article_id, cleaned Reader body)."""
    inserted = execute_values(
        cur,
        "INSERT INTO news.article_body (article_id, source, text, char_count) VALUES %s "
        "ON CONFLICT (article_id) DO NOTHING RETURNING article_id",
        [(article_id, "reader", text, len(text)) for article_id, text in rows],
        page_size=100,
        fetch=True,
    )
    return len(inserted)


def copy_legacy_embeddings(cur, article_ids: list[int], model: str) -> int:
    cur.execute(
        "INSERT INTO news.article_embedding (article_id, embedding, model) "
        "SELECT a.article_id, e.embedding, %s FROM news.article a JOIN news_embeddings e ON e.url = a.url "
        "WHERE a.article_id = ANY(%s) AND e.embedding IS NOT NULL "
        "ON CONFLICT (article_id) DO NOTHING",
        (model, article_ids),
    )
    return cur.rowcount


def insert_migrated_sightings(cur, sightings: list[MigratedSighting]) -> int:
    inserted = execute_values(
        cur,
        """
        INSERT INTO news.sighting
            (article_id, channel_id, external_id, title, first_seen_at, last_seen_at, score)
        VALUES %s
        ON CONFLICT (channel_id, article_id) DO NOTHING
        RETURNING sighting_id
        """,
        [
            (s.url_key, s.channel_key, s.external_id, s.title, s.first_seen_at, s.last_seen_at, s.score)
            for s in sightings
        ],
        template="((SELECT article_id FROM news.article WHERE url_key = %s), "
        "(SELECT channel_id FROM news.channel WHERE key = %s), %s, %s, %s, %s, %s)",
        page_size=500,
        fetch=True,
    )
    return len(inserted)


def take_relabel(cur, *, limit: int | None, url_keys: list[str] | None) -> list[RelabelItem]:
    cur.execute(
        """
        SELECT article_id, title, summary FROM news.article
        WHERE (label_source = 'mapped' OR taxonomy_version IS NULL)
          AND summary IS NOT NULL
          AND (%(url_keys)s::text[] IS NULL OR url_key = ANY(%(url_keys)s::text[]))
        ORDER BY article_id
        LIMIT %(limit)s
        """,
        {"url_keys": url_keys, "limit": limit},
    )
    return [RelabelItem(*row) for row in cur.fetchall()]


def record_relabel(cur, article_id: int, topics: list[str], kind: str) -> None:
    cur.execute(
        "UPDATE news.article SET topics = %s, kind = %s, taxonomy_version = %s, label_source = 'model', "
        "updated_at = now() WHERE article_id = %s",
        (topics, kind, TAXONOMY_VERSION, article_id),
    )


def try_advisory_lock(cur) -> bool:
    cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    return cur.fetchone()[0]


def release_advisory_lock(cur) -> None:
    cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
