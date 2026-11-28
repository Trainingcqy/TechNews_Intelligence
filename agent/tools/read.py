"""read_articles: article bodies, their registration and text for the gather model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.config import user_zone
from agent.retrieval.stories import fetch_articles_by_url_keys, fetch_bodies
from agent.trace import warn
from agent.types import Article
from common.db import get_conn, put_conn
from common.urls import url_key

if TYPE_CHECKING:
    from .registry import PreparedCall, ReadArticlesArgs, ToolOutcome

OVER_QUOTA = "over the read limit of this request"


@dataclass(frozen=True)
class ReadItem:
    target: str  # 证据编号或地址
    article: Article | None
    text: str | None
    total_chars: int
    error: str | None


@dataclass(frozen=True)
class ReadResult:
    items: list[ReadItem]


def run_read_articles(prepared: PreparedCall, *, registry, state) -> ReadResult:
    args = prepared.args
    # (target, article, error) in request order: ids first, then urls.
    resolved: list[tuple[str, Article | None, str | None]] = []
    for eid in args.ids:
        evidence = registry.get(eid)
        if evidence is None:
            resolved.append((eid, None, "unknown id"))
        else:
            resolved.append((eid, evidence.article, None))
    url_keys = {url: url_key(url) for url in args.urls}
    allowed = {url: key for url, key in url_keys.items() if state.allow_any_library_url or key in state.user_urls}

    conn = get_conn()
    try:
        cur = conn.cursor()
        by_key = {url_key(a.url): a for a in fetch_articles_by_url_keys(cur, list(allowed.values()))}
        for url, key in url_keys.items():
            if url not in allowed:
                resolved.append((url, None, "the link was not provided by the user"))
            elif key in by_key:
                resolved.append((url, by_key[key], None))
            else:
                resolved.append((url, None, "the link is not in the news database"))
        bodies = fetch_bodies(cur, [a.article_id for _, a, _ in resolved if a and a.body_level == "full"])
        cur.close()
    finally:
        put_conn(conn)

    items = []
    for target, article, error in resolved:
        if error is None:
            full = bodies.get(article.article_id)
            if full is None:
                error = "no full text stored"
        if error is not None:
            items.append(ReadItem(target, article, None, 0, error))
        else:
            items.append(ReadItem(target, article, full[:args.max_chars], len(full), None))
    if prepared.dropped:
        warn("read_targets_dropped")
        items.extend(ReadItem(target, None, None, 0, OVER_QUOTA) for target in prepared.dropped)
    return ReadResult(items=items)


def read_trace_output(result: ReadResult) -> dict:
    return {"items": [
        {
            "target": item.target,
            "article_id": item.article.article_id if item.article else None,
            "chars": len(item.text) if item.text is not None else 0,
            "total_chars": item.total_chars,
            "error": item.error,
        }
        for item in result.items
    ]}


def commit_read_articles(args: ReadArticlesArgs, result: ReadResult, *, registry) -> ToolOutcome:
    # registry.py imports this module, so the import is deferred to call time.
    from .registry import ToolOutcome

    lines = ["read_articles results:"]
    read = []
    for item in result.items:
        evidence = registry.get(item.target)
        if evidence is None and item.article is not None:
            evidence = registry.add_article(item.article, "summary")
        label = evidence.eid if evidence else item.target
        if item.error is not None:
            lines.append(f"[{label}] read failed: {item.error}.")
            continue
        truncated = item.total_chars > len(item.text)
        registry.set_fulltext(evidence.eid, item.text, item.total_chars, truncated)
        read.append(evidence)
        article = item.article
        published = article.published_at.astimezone(user_zone())
        head = f'[{label}] "{article.title}" {article.publisher} · {published:%Y-%m-%d}'
        size = f"{item.total_chars} characters in full"
        if truncated:
            size += f", first {len(item.text)} read"
        lines.append(f"{head} | {size}; added to the evidence")
        lines.append(item.text)
    text = "\n".join(lines)
    ok = bool(read)
    return ToolOutcome(index=0, call_id="", name="read_articles", ok=ok, gather_text=text,
                       error=None if ok else text)
