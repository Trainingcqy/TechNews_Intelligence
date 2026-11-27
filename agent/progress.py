"""What the chat page shows of a turn: the status line while it runs, and its checks, searches and reads above the
answer."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlsplit

from agent.config import user_zone
from agent.evidence import EvidenceRegistry
from agent.tools.follows import FollowCheck
from agent.tools.registry import PreparedCall, ReadArticlesArgs, SearchNewsArgs, ToolPayload, story_hits
from agent.types import Article, Process, ProcessItem, Progress
from common.taxonomy import KINDS, TOPICS

UNDERSTANDING = Progress("正在理解问题")

CHECK, SEARCH, READ = "检查", "检索", "阅读"


def window(args: SearchNewsArgs, today: date) -> str:
    start, end = args.start_date, args.end_date
    if start is None:
        return f"最近 {args.days} 天"
    if end is None:
        return "今天" if start == today else f"{start} 至今"
    if start == end:
        return "今天" if start == today else str(start)
    return f"{start} 至 {end}"


def subject(args: SearchNewsArgs) -> str:
    if args.query:
        return args.query
    topic = TOPICS[args.topic] if args.topic else ""
    return topic + ("最新新闻" if args.sort == "time" else "要闻")


def step(calls: list[PreparedCall], registry: EvidenceRegistry, follows: list[str], today: date) -> Progress:
    """The status line of a step's calls while they run."""
    searches = [call.args for call in calls if call.name == "search_news"]
    reads = [call.args for call in calls if call.name == "read_articles"]
    checking = any(call.name == "check_follows" for call in calls)
    actions = []
    if checking:
        actions.append(f"检查 {len(follows)} 个关注项" if follows else "检查关注项")
    if searches:
        actions.append("检索")
    if reads:
        actions.append(f"阅读 {sum(len(args.ids) + len(args.urls) for args in reads)} 篇报道")
    if searches:
        detail = _searches(searches, today)
    elif checking:
        detail = "、".join(follows)
    else:
        detail = "、".join(_read_titles(reads, registry))
    return Progress("正在" + _joined(actions), detail)


def found(payloads: list[ToolPayload]) -> str:
    """What a step's calls found, shown while the gather model weighs it.

    A read, or a search without query, finishes in about no time, so the status line of its step is replaced before it
    is seen; its result shows here instead.
    """
    stories: set[int] | None = None
    reads: set[int] | None = None
    for payload in payloads:
        if payload.name == "read_articles":
            reads = set() if reads is None else reads
            if payload.ok:
                reads.update(item.article.article_id for item in payload.data.items if item.error is None)
        elif payload.name in ("search_news", "check_follows"):
            stories = set() if stories is None else stories
            if payload.ok:
                stories.update(hit.stats.story_id for hit in story_hits(payload))
    parts = []
    if stories is not None:
        parts.append(f"找到 {len(stories)} 条新闻" if stories else "没有找到新闻")
    if reads is not None:
        parts.append(f"读到 {len(reads)} 篇全文" if reads else "没有读到全文")
    return "，".join(parts)


def analyzing(found_text: str) -> Progress:
    return Progress("正在分析结果", found_text)


def writing(reports: int) -> Progress:
    return Progress("正在撰写回答", f"依据 {reports} 篇报道")


def record(payload: ToolPayload, today: date, read_ids: set[int]) -> list[ProcessItem]:
    """The process items of an executed call. read_ids holds the articles listed so far, so a second read of one is
    left out."""
    if payload.name == "search_news":
        args = payload.args
        meta = [*_filters(args), window(args, today), _outcome(payload.data.hits if payload.ok else [], not payload.ok)]
        return [ProcessItem(SEARCH, subject(args), "，".join(meta))]
    if not payload.ok:
        return []
    if payload.name == "check_follows":
        return [_checked(check) for check in payload.data.checks]
    items = []
    if payload.name == "read_articles":
        for item in payload.data.items:
            if item.error is None and item.article.article_id not in read_ids:
                read_ids.add(item.article.article_id)
                items.append(ProcessItem(READ, _title(item.article), _published(item.article)))
    return items


def process(items: list[ProcessItem]) -> Process | None:
    if not items:
        return None
    counts = {action: sum(item.action == action for item in items) for action in (CHECK, SEARCH, READ)}
    parts = []
    if counts[CHECK]:
        parts.append(f"检查 {counts[CHECK]} 个关注项")
    if counts[SEARCH]:
        parts.append(f"检索 {counts[SEARCH]} 次")
    if counts[READ]:
        parts.append(f"阅读 {counts[READ]} 篇报道")
    return Process("，".join(parts), tuple(items))


def _joined(actions: list[str]) -> str:
    if len(actions) <= 2:
        return "并".join(actions)
    return "、".join(actions[:-1]) + "并" + actions[-1]


def _searches(searches: list[SearchNewsArgs], today: date) -> str:
    subjects = [subject(args) for args in searches]
    windows = [window(args, today) for args in searches]
    if len(set(windows)) == 1:
        return "、".join(subjects) + "，" + windows[0]
    return "、".join(f"{name} {span}" for name, span in zip(subjects, windows))


def _read_titles(reads: list[ReadArticlesArgs], registry: EvidenceRegistry) -> list[str]:
    titles = [_title(evidence.article) for args in reads for evidence in map(registry.get, args.ids) if evidence]
    return titles + [(urlsplit(url).hostname or url).removeprefix("www.") for args in reads for url in args.urls]


def _filters(args: SearchNewsArgs) -> list[str]:
    parts = []
    if args.query and args.topic:
        parts.append(f"仅{TOPICS[args.topic]}领域")
    if args.kind:
        parts.append(f"仅{KINDS[args.kind]}类")
    if args.publisher_kind:
        parts.append("仅官方渠道" if args.publisher_kind == "official" else "仅媒体报道")
    return parts


def _outcome(hits: list, failed: bool) -> str:
    if failed:
        return "失败"
    return f"{len(hits)} 条新闻" if hits else "无结果"


def _checked(check: FollowCheck) -> ProcessItem:
    meta = [] if check.query.lower() == check.follow.query.lower() else [check.query]
    meta += [f"最近 {check.days} 天", _outcome(check.result.hits if check.result else [], check.error is not None)]
    return ProcessItem(CHECK, check.follow.query, "，".join(meta))


def _title(article: Article) -> str:
    return article.title_zh or article.title


def _published(article: Article) -> str:
    return f"{article.publisher}，{article.published_at.astimezone(user_zone()):%Y-%m-%d}"
