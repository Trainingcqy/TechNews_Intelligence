"""check_follows: one search per follow of the user, run in parallel, and the results grouped by follow."""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.evidence import counted
from agent.memory import Follow, check_window
from agent.retrieval.search import SearchResult, search_stories
from agent.types import Evidence

from .search import render_hit

if TYPE_CHECKING:
    from .registry import CheckFollowsArgs, PreparedCall, ToolOutcome

logger = logging.getLogger(__name__)

STORY_LIMIT = 5
WORKERS = 3


@dataclass(frozen=True)
class FollowCheck:
    follow: Follow
    query: str
    days: int
    result: SearchResult | None
    error: str | None


@dataclass(frozen=True)
class FollowsResult:
    checks: list[FollowCheck]


def run_check_follows(prepared: PreparedCall, *, registry, state) -> FollowsResult:
    english = {item.follow.lower(): item.query for item in prepared.args.queries}
    plans = [(follow, english.get(follow.query.lower(), follow.query), check_window(follow, state.today))
             for follow in state.follows]
    with ThreadPoolExecutor(WORKERS) as pool:
        # Each search runs in a copy of this context, so its spans land under the check_follows span.
        futures = [pool.submit(contextvars.copy_context().run, _check, *plan) for plan in plans]
        return FollowsResult([future.result() for future in futures])


def _check(follow: Follow, query: str, days: int) -> FollowCheck:
    try:
        result = search_stories(query=query, days=days, start_date=None, end_date=None, topic=None, kinds=None,
                                publisher_kind=None, sort="importance", limit=STORY_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.exception("checking follow %r failed", follow.query)
        return FollowCheck(follow, query, days, None, type(exc).__name__)
    return FollowCheck(follow, query, days, result, None)


def follows_trace_output(result: FollowsResult) -> dict:
    return {"follows": [
        {"follow": check.follow.query, "query": check.query, "days": check.days, "error": check.error,
         "stories": [hit.stats.story_id for hit in check.result.hits] if check.result else []}
        for check in result.checks
    ]}


def commit_check_follows(args: CheckFollowsArgs, result: FollowsResult, *, registry) -> ToolOutcome:
    # registry.py imports this module, so the import is deferred to call time.
    from .registry import ToolOutcome

    if not result.checks:
        return ToolOutcome(index=0, call_id="", name="check_follows", ok=True,
                           gather_text="check_follows: the user has no follows.")
    lines = [f"check_follows results: {counted(len(result.checks), 'follow', 'follows')}"]
    leads: dict[str, Evidence] = {}
    for check in result.checks:
        head = f'Follow "{check.follow.query}", query="{check.query}", last {counted(check.days, "day", "days")}'
        if check.error is not None:
            lines.append(f"{head}: search failed: {check.error}.")
            continue
        if not check.result.hits:
            lines.append(f"{head}: no stories.")
            continue
        lines.append(f"{head}, by importance: {counted(len(check.result.hits), 'story', 'stories')}")
        for hit in check.result.hits:
            lead = registry.add_article(hit.lead, "summary", hit.stats)
            related = [registry.add_article(article, "listed") for article in hit.related]
            leads.setdefault(lead.eid, lead)
            lines.extend(render_hit(hit, lead.eid, [e.eid for e in related], registry.seen_dates.get(hit.stats.story_id)))
    text = "\n".join(lines)
    ok = any(check.error is None for check in result.checks)
    return ToolOutcome(index=0, call_id="", name="check_follows", ok=ok, gather_text=text,
                       new_evidence=tuple(leads.values()), error=None if ok else text)
