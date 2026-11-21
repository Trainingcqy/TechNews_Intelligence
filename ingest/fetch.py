"""fetch stage: article bodies from Jina Reader."""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

from ingest import http, store
from ingest.config import IngestConfig
from ingest.discover.sitemap import placeholder_title
from ingest.reader import parse_reader_response

logger = logging.getLogger(__name__)

READER_URL = "https://r.jina.ai/"
_DETAIL_BODY_CHARS = 500

_IMAGE_RE = re.compile(r"!\[.*?\]\(.*?\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(http.*?\)")


def clean_body(text: str) -> str:
    # PostgreSQL text cannot hold NUL; the old workflow removed it before storing the raw Reader output.
    return _LINK_RE.sub(r"\1", _IMAGE_RE.sub("", text)).replace("\x00", "")


def _reader_headers() -> dict[str, str]:
    headers = {"X-Retain-Images": "none", "X-Retain-Links": "text"}
    api_key = os.getenv("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _fetch(item: store.FetchItem, transaction: store.Transaction, config: IngestConfig) -> None:
    try:
        response = http.get(
            READER_URL + item.url,
            user_agent=http.USER_AGENT,
            timeout=config.reader_timeout,
            headers=_reader_headers(),
        )
    except http.TransportError as exc:
        with transaction() as cur:
            store.record_failure(cur, item.article_id, "fetch", exc.kind, str(exc), config)
        return

    result = parse_reader_response(response.status, response.content.decode("utf-8", errors="replace"))
    with transaction() as cur:
        if result.ok:
            store.record_fetch_success(
                cur,
                item.article_id,
                body=clean_body(result.body),
                published_at=result.published_at,
                title=result.title if item.title == placeholder_title(item.url) else None,
            )
        else:
            detail = f"status: {response.status}\nwarning: {result.warning}\n{result.body[:_DETAIL_BODY_CHARS]}"
            store.record_failure(cur, item.article_id, "fetch", result.error_kind, detail.replace("\x00", ""), config)


def fetch_article(item: store.FetchItem, *, transaction: store.Transaction, config: IngestConfig) -> None:
    try:
        _fetch(item, transaction, config)
    except Exception as exc:
        # Recorded so that a repeating error backs off and degrades instead of keeping the article first in the queue.
        logger.exception("fetch %s failed", item.url)
        with transaction() as cur:
            store.record_failure(cur, item.article_id, "fetch", "unexpected", f"{type(exc).__name__}: {exc}", config)


def run_fetch(config: IngestConfig, *, transaction: store.Transaction) -> None:
    with transaction() as cur:
        items = store.take_fetch(cur, limit=config.fetch_limit)
    with ThreadPoolExecutor(config.fetch_workers) as pool:
        futures = {pool.submit(fetch_article, item, transaction=transaction, config=config): item for item in items}
    for future, item in futures.items():
        if future.exception() is not None:
            logger.error("fetch %s: recording the failure failed", item.url, exc_info=future.exception())
