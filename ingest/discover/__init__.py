"""discover stage: poll a channel, parse its entries, store the candidates and record the poll."""

from __future__ import annotations

import html
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from ingest import http, store
from ingest.config import IngestConfig
from ingest.discover.feed import parse_feed
from ingest.discover.reference import parse_reference_feed
from ingest.discover.sitemap import parse_sitemap
from ingest.models import Candidate
from ingest.sources import resolve_publisher
from common.mail import _send
from common.urls import host_of, url_key

logger = logging.getLogger(__name__)

_ALERT_STREAK = 3
_DEFAULT_LOOKBACK_DAYS = 3


def path_depth(url: str) -> int:
    return len([segment for segment in urlsplit(url.strip()).path.split("/") if segment])


def keep_candidate(candidate: Candidate, config: dict, *, now: datetime) -> bool:
    if not url_key(candidate.url) or not host_of(candidate.url):
        return False
    lookback = timedelta(days=config.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    if candidate.published_at is not None and candidate.published_at < now - lookback:
        return False
    # Shopping posts, event promotions and other non-news sections that the source's URLs identify.
    exclude = config.get("exclude_url")
    if exclude and re.search(exclude, candidate.url, re.IGNORECASE):
        return False
    max_segments = config.get("max_path_segments")
    return max_segments is None or path_depth(candidate.url) <= max_segments


def _parse(channel: store.DueChannel, content: bytes, now: datetime) -> list[Candidate]:
    if channel.fetch_kind == "feed":
        return parse_feed(content, now=now)
    if channel.fetch_kind == "sitemap":
        return parse_sitemap(
            content,
            url_pattern=channel.config["url_pattern"],
            now=now,
            lookback_days=channel.config.get("lookback_days", _DEFAULT_LOOKBACK_DAYS),
            require_lastmod=not channel.config.get("lastmod_optional", False),
        )
    raise ValueError(f"unknown fetch_kind {channel.fetch_kind!r}")


def _store_candidates(channel: store.DueChannel, candidates: list[Candidate], transaction: store.Transaction) -> int:
    with transaction() as cur:
        hosts = store.load_publisher_hosts(cur)
        first_poll = not store.channel_has_sightings(cur, channel.channel_id)
    items_new = 0
    for candidate in candidates:
        with transaction() as cur:
            publisher = resolve_publisher(candidate.url, hosts)
            # A miss returns a fresh auto publisher whose only entry is not registered.
            if publisher.hosts[0] not in hosts:
                store.ensure_auto_publisher(cur, publisher.name)
            items_new += store.insert_candidate(cur, channel, candidate, publisher.name, first_poll=first_poll)
    return items_new


def _store_headlines(channel: store.DueChannel, content: bytes, now: datetime, transaction: store.Transaction) -> tuple[int, int]:
    headlines = parse_reference_feed(content, now=now)
    items_new = 0
    for headline in headlines:
        with transaction() as cur:
            items_new += store.insert_reference_headline(
                cur,
                channel.channel_id,
                external_id=headline.external_id,
                title=headline.title,
                target_url=headline.target_url,
                outlet=headline.outlet,
                published_at=headline.published_at,
            )
    return len(headlines), items_new


def discover_channel(channel: store.DueChannel, *, transaction: store.Transaction, config: IngestConfig) -> None:
    now = datetime.now(UTC)
    http_status = None
    etag = last_modified = None
    items_seen = items_new = 0
    error = None
    try:
        response = http.get(
            channel.endpoint,
            user_agent=http.USER_AGENT,
            timeout=config.discover_timeout,
            etag=channel.etag,
            last_modified=channel.last_modified,
        )
        http_status = response.status
        if response.status == 200:
            if channel.purpose == "reference":
                items_seen, items_new = _store_headlines(channel, response.content, now, transaction)
            else:
                candidates = _parse(channel, response.content, now)
                items_seen = len(candidates)
                kept = [c for c in candidates if keep_candidate(c, channel.config, now=now)]
                items_new = _store_candidates(channel, kept, transaction)
            # feedparser returns no entries instead of raising when the response is not a feed.
            if channel.fetch_kind == "feed" and items_seen == 0:
                error = "no feed entries in response"
        elif response.status != 304:
            error = f"HTTP {response.status}"
        # Validators are kept only after a complete poll: storing them after a partial one would make the
        # next poll a 304 and lose the entries not yet stored.
        if error is None:
            etag, last_modified = response.etag, response.last_modified
    except Exception as exc:
        logger.exception("discover %s failed", channel.key)
        error = f"{type(exc).__name__}: {exc}"

    with transaction() as cur:
        fail_streak = store.record_poll(
            cur,
            channel.channel_id,
            # The start, not the end: the candidates are stored first, and the monitor counts only articles
            # first seen at or after the earliest poll.
            polled_at=now,
            ok=error is None,
            http_status=http_status,
            items_seen=items_seen,
            items_new=items_new,
            error=error,
            etag=etag,
            last_modified=last_modified,
        )
    if fail_streak == _ALERT_STREAK:
        _send(
            os.getenv("ADMIN_EMAIL", ""),
            f"[TechNews] 渠道 {channel.key} 连续 {_ALERT_STREAK} 次轮询失败",
            f"<p>渠道 {html.escape(channel.key)}（{html.escape(channel.endpoint)}）连续 {_ALERT_STREAK} 次轮询失败。</p>"
            f"<p>最近一次的错误：{html.escape(error)}</p>",
        )


def run_discover(config: IngestConfig, *, transaction: store.Transaction, channel_key: str | None = None) -> None:
    with transaction() as cur:
        if channel_key is None:
            channels = store.due_channels(cur)
        else:
            channel = store.enabled_channel(cur, channel_key)
            if channel is None:
                raise ValueError(f"no enabled channel {channel_key!r}")
            channels = [channel]
    with ThreadPoolExecutor(config.discover_workers) as pool:
        futures = {pool.submit(discover_channel, c, transaction=transaction, config=config): c for c in channels}
    for future, channel in futures.items():
        if future.exception() is not None:
            logger.error("discover %s: recording the poll failed", channel.key, exc_info=future.exception())
