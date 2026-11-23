"""search_news: story-level search and its registration and text for the gather model."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from agent.config import BEIJING
from agent.evidence import PUBLISHER_KIND_LABELS, seen_note
from agent.retrieval.search import SearchResult, search_stories
from agent.types import StoryHit, StoryStats

if TYPE_CHECKING:
    from .registry import SearchNewsArgs, ToolOutcome

INDENT = "     "
SORT_LABELS = {"relevance": "按相关性", "importance": "按重要性", "time": "按时间", "hn": "按 HackerNews 分数"}


def run_search_news(args: SearchNewsArgs) -> SearchResult:
    return search_stories(
        query=args.query, days=args.days, start_date=args.start_date, end_date=args.end_date,
        topic=args.topic, kinds=(args.kind,) if args.kind else None,
        publisher_kind=args.publisher_kind, sort=args.sort, limit=args.limit,
    )


def search_trace_output(result: SearchResult) -> dict:
    return {"stories": [
        {
            "story_id": hit.stats.story_id,
            "lead": hit.lead.article_id,
            "related": [article.article_id for article in hit.related],
            "discussion": hit.discussion.sighting_id if hit.discussion else None,
            "reports": hit.stats.report_count,
            "publishers": hit.stats.publisher_count,
            "match_score": hit.match_score,
        }
        for hit in result.hits
    ]}


def commit_search_news(args: SearchNewsArgs, result: SearchResult, *, registry) -> ToolOutcome:
    # registry.py imports this module, so the import is deferred to call time.
    from .registry import ToolOutcome

    scope = _scope(args)
    if not result.hits:
        text = f"search_news 没有找到结果，{scope}。可以放宽时间窗、换一种说法，中英文都可以，或去掉过滤条件。"
        return ToolOutcome(index=0, call_id="", name="search_news", ok=True, gather_text=text)

    lines = [f"search_news 结果，{scope}，{SORT_LABELS[args.sort]}：{len(result.hits)} 个故事"]
    leads = []
    for hit in result.hits:
        lead = registry.add_article(hit.lead, "summary", hit.stats)
        related = [registry.add_article(article, "listed") for article in hit.related]
        discussion = registry.add_discussion(hit.discussion) if hit.discussion else None
        leads.append(lead)
        lines.extend(_render_hit(hit, lead.eid, [e.eid for e in related], discussion.eid if discussion else None,
                                 registry.seen_dates.get(hit.stats.story_id)))
    if result.weak_match:
        lines.append("提示：最相关的结果匹配度偏低，可能不切题。")
    return ToolOutcome(index=0, call_id="", name="search_news", ok=True, gather_text="\n".join(lines),
                       new_evidence=tuple(leads))


def _scope(args: SearchNewsArgs) -> str:
    parts = []
    if args.query is not None:
        parts.append(f'query="{args.query}"')
    for name in ("topic", "kind", "publisher_kind"):
        value = getattr(args, name)
        if value is not None:
            parts.append(f"{name}={value}")
    if args.start_date is None:
        parts.append(f"最近 {args.days} 天")
    elif args.end_date is None:
        parts.append(f"{args.start_date} 至今")
    else:
        parts.append(f"{args.start_date} 至 {args.end_date}")
    return "，".join(parts)


def _render_hit(hit: StoryHit, lead_eid: str, related_eids: list[str], discussion_eid: str | None,
                seen: date | None) -> list[str]:
    lead = hit.lead
    lines = [f"[{lead_eid}] {lead.title}"]
    if lead.title_zh:
        lines.append(INDENT + lead.title_zh)
    meta = [lead.publisher, PUBLISHER_KIND_LABELS[lead.publisher_kind], f"{lead.published_at.astimezone(BEIJING):%Y-%m-%d}"]
    if lead.topics:
        meta.append("、".join(lead.topics))
    if lead.kind:
        meta.append(lead.kind)
    lines.append(INDENT + " · ".join(meta) + "｜" + _render_stats(hit.stats) + seen_note(seen))
    lines.append(f"{INDENT}摘要：{lead.summary}")
    if hit.related:
        others = [
            f"[{eid}] {a.publisher} · {PUBLISHER_KIND_LABELS[a.publisher_kind]} · "
            f"{a.published_at.astimezone(BEIJING):%m-%d}《{a.title}》"
            for eid, a in zip(related_eids, hit.related)
        ]
        lines.append(f"{INDENT}同故事其他报道：" + "；".join(others))
    if discussion_eid:
        score = f"{hit.discussion.score} 分，" if hit.discussion.score is not None else ""
        lines.append(f"{INDENT}社区讨论：[{discussion_eid}] HackerNews 讨论，{score}"
                     f"{hit.discussion.comment_total} 条评论，可用 read_articles 读取")
    return lines


def _render_stats(stats: StoryStats) -> str:
    text = f"同一故事 {stats.report_count} 篇报道、{stats.publisher_count} 个发布方"
    if "official" in stats.publisher_kinds:
        text += "，含官方发布"
    if stats.hn_score is not None:
        text += f"，HN 最高 {stats.hn_score} 分"
        if stats.hn_comments is not None:
            text += f"、{stats.hn_comments} 条评论"
    return text
