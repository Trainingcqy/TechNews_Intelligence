"""search_news: story-level search and its registration and text for the gather model."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from agent.config import user_zone
from agent.evidence import PUBLISHER_KIND_LABELS, counted, seen_note
from agent.retrieval.search import SearchResult, search_stories
from agent.types import StoryHit, StoryStats
from common import prompt_text

if TYPE_CHECKING:
    from .registry import SearchNewsArgs, ToolOutcome

INDENT = "     "
SORT_LABELS = {"relevance": "by relevance", "importance": "by importance", "time": "by time"}


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
        text = f"search_news found no results, {scope}. " + prompt_text.text("tools.search_news.no_result_hint")
        return ToolOutcome(index=0, call_id="", name="search_news", ok=True, gather_text=text)

    stories = counted(len(result.hits), "story", "stories")
    lines = [f"search_news results, {scope}, {SORT_LABELS[args.sort]}: {stories}"]
    leads = []
    for hit in result.hits:
        lead = registry.add_article(hit.lead, "summary", hit.stats)
        related = [registry.add_article(article, "listed") for article in hit.related]
        leads.append(lead)
        lines.extend(_render_hit(hit, lead.eid, [e.eid for e in related], registry.seen_dates.get(hit.stats.story_id)))
    if result.weak_match:
        lines.append("Note: the top result is a weak match and may be off topic.")
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
        parts.append(f"last {counted(args.days, 'day', 'days')}")
    elif args.end_date is None:
        parts.append(f"{args.start_date} to today")
    else:
        parts.append(f"{args.start_date} to {args.end_date}")
    return ", ".join(parts)


def _render_hit(hit: StoryHit, lead_eid: str, related_eids: list[str], seen: date | None) -> list[str]:
    lead = hit.lead
    lines = [f"[{lead_eid}] {lead.title}"]
    if lead.title_zh:
        lines.append(INDENT + lead.title_zh)
    meta = [lead.publisher, PUBLISHER_KIND_LABELS[lead.publisher_kind],
            f"{lead.published_at.astimezone(user_zone()):%Y-%m-%d}"]
    if lead.topics:
        meta.append(", ".join(lead.topics))
    if lead.kind:
        meta.append(lead.kind)
    lines.append(INDENT + " · ".join(meta) + " | " + _render_stats(hit.stats) + seen_note(seen))
    lines.append(f"{INDENT}Summary: {lead.summary}")
    if hit.related:
        others = [
            f"[{eid}] {a.publisher} · {PUBLISHER_KIND_LABELS[a.publisher_kind]} · "
            f'{a.published_at.astimezone(user_zone()):%m-%d} "{a.title}"'
            for eid, a in zip(related_eids, hit.related)
        ]
        lines.append(f"{INDENT}Other reports in the story: " + "; ".join(others))
    return lines


def _render_stats(stats: StoryStats) -> str:
    text = (f"Coverage: {counted(stats.report_count, 'report', 'reports')}, "
            f"{counted(stats.publisher_count, 'publisher', 'publishers')}")
    if "official" in stats.publisher_kinds:
        text += ", including an official publisher"
    return text
