"""read_articles: bodies of articles and discussions, their registration and text for the gather model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.config import BEIJING, AgentConfig
from agent.retrieval.stories import (
    fetch_articles_by_url_keys,
    fetch_bodies,
    fetch_discussion_texts,
    fetch_discussions_by_url_keys,
)
from agent.types import Article, Discussion
from services.db import get_conn, put_conn
from services.urls import url_key

if TYPE_CHECKING:
    from .registry import ReadArticlesArgs, ToolOutcome

PREVIEW_CHARS = AgentConfig().gather_read_preview_chars


@dataclass(frozen=True)
class ReadItem:
    target: str  # 证据编号或地址
    article: Article | None
    discussion: Discussion | None
    text: str | None
    total_chars: int
    error: str | None


@dataclass(frozen=True)
class ReadResult:
    items: list[ReadItem]


def run_read_articles(args: ReadArticlesArgs, *, registry, state) -> ReadResult:
    # (target, article, discussion, error) in request order: ids first, then urls.
    resolved: list[tuple[str, Article | None, Discussion | None, str | None]] = []
    for eid in args.ids:
        evidence = registry.get(eid)
        if evidence is None:
            resolved.append((eid, None, None, "未知编号"))
        else:
            resolved.append((eid, evidence.article, evidence.discussion, None))
    url_keys = {url: url_key(url) for url in args.urls}
    allowed = {url: key for url, key in url_keys.items() if state.allow_any_library_url or key in state.user_urls}

    conn = get_conn()
    try:
        cur = conn.cursor()
        by_key = {url_key(a.url): a for a in fetch_articles_by_url_keys(cur, list(allowed.values()))}
        # A HackerNews discussion page is not an article URL, so keys without an article are looked up as discussions.
        missing = [key for key in allowed.values() if key not in by_key]
        discussion_by_key = {url_key(d.url): d for d in fetch_discussions_by_url_keys(cur, missing)}
        for url, key in url_keys.items():
            if url not in allowed:
                resolved.append((url, None, None, "该链接不是用户提供的"))
            elif key in by_key:
                resolved.append((url, by_key[key], None, None))
            elif key in discussion_by_key:
                resolved.append((url, None, discussion_by_key[key], None))
            else:
                resolved.append((url, None, None, "该链接不在新闻库内"))
        bodies = fetch_bodies(cur, [a.article_id for _, a, _, _ in resolved if a and a.body_level == "full"])
        texts = fetch_discussion_texts(cur, [d.sighting_id for _, _, d, _ in resolved if d])
        cur.close()
    finally:
        put_conn(conn)

    items = []
    for target, article, discussion, error in resolved:
        if error is None:
            full = texts.get(discussion.sighting_id) if discussion else bodies.get(article.article_id)
            if full is None:
                error = "没有入库正文"
        if error is not None:
            items.append(ReadItem(target, article, discussion, None, 0, error))
        else:
            items.append(ReadItem(target, article, discussion, full[:args.max_chars], len(full), None))
    return ReadResult(items=items)


def commit_read_articles(args: ReadArticlesArgs, result: ReadResult, *, registry) -> ToolOutcome:
    # registry.py imports this module, so the import is deferred to call time.
    from .registry import ToolOutcome

    lines = ["read_articles 结果："]
    # A search lead was already reported when it became citable; only items that become citable here are new.
    citable_before = registry.citable_ids()
    read = []
    for item in result.items:
        evidence = registry.get(item.target)
        if evidence is None and item.article is not None:
            evidence = registry.add_article(item.article, "summary")
        elif evidence is None and item.discussion is not None:
            evidence = registry.add_discussion(item.discussion)
        label = evidence.eid if evidence else item.target
        if item.error is not None:
            lines.append(f"[{label}] 读取失败：{item.error}。")
            continue
        truncated = item.total_chars > len(item.text)
        registry.set_fulltext(evidence.eid, item.text, item.total_chars, truncated)
        read.append(evidence)
        if item.discussion is not None:
            head = f"[{label}] HackerNews 讨论"
        else:
            article = item.article
            head = f"[{label}] 《{article.title}》{article.publisher} · {article.published_at.astimezone(BEIJING):%Y-%m-%d}"
        size = f"全文 {item.total_chars} 字符"
        if truncated:
            size += f"，已读取前 {len(item.text)} 字符"
        lines.append(f"{head}｜{size}")
        lines.append(f"开头：{item.text[:PREVIEW_CHARS]}")
    text = "\n".join(lines)
    ok = bool(read)
    return ToolOutcome(index=0, call_id="", name="read_articles", ok=ok, gather_text=text,
                       new_evidence=tuple(e for e in read if e.eid not in citable_before),
                       error=None if ok else text)
