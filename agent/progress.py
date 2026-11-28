"""What the chat page shows of a turn: the status line while it runs, and its checks, searches and reads above the
answer."""

from __future__ import annotations

from collections import Counter
from datetime import date
from urllib.parse import urlsplit

from agent.config import user_zone
from agent.evidence import EvidenceRegistry
from agent.tools.follows import FollowCheck
from agent.tools.history import HistoryResult
from agent.tools.registry import (
    BrowseNewsArgs,
    PreparedCall,
    ReadArticlesArgs,
    ReadHistoryArgs,
    SearchNewsArgs,
    ToolPayload,
    story_hits,
)
from agent.types import Article, Process, ProcessItem, Progress
from common.taxonomy import KINDS, TOPICS

UNDERSTANDING = Progress("正在理解问题")

CHECK, SEARCH, READ = "查看", "检索", "阅读"
SEARCH_TOOLS = ("search_news", "browse_news")


def window(args: SearchNewsArgs | BrowseNewsArgs, today: date) -> str:
    start, end = args.start_date, args.end_date
    if start is None:
        return f"最近 {args.days} 天"
    if end is None:
        return "今天" if start == today else f"{start} 至今"
    if start == end:
        return "今天" if start == today else str(start)
    return f"{start} 至 {end}"


def subject(args: SearchNewsArgs | BrowseNewsArgs, today: date) -> str:
    """What a search looked for: its query, or for a browse, the window and topic it browsed."""
    if isinstance(args, SearchNewsArgs):
        return args.query
    span = window(args, today)
    topic = TOPICS[args.topic] if args.topic else ""
    return f"{span}{_joint(span)}的{topic}{'最新新闻' if args.sort == 'time' else '要闻'}"


def step(calls: list[PreparedCall], registry: EvidenceRegistry, today: date) -> Progress:
    """The status line of a step's calls while they run."""
    searches = [call.args for call in calls if call.name in SEARCH_TOOLS]
    reads = [call.args for call in calls if call.name == "read_articles"]
    histories = [call.args for call in calls if call.name == "read_history"]
    checking = any(call.name == "check_follows" for call in calls)
    keywords = [keyword for args in histories for keyword in args.keywords]
    actions = []
    if checking:
        actions.append("查看关注项" + ("、之前的回答" if histories else ""))
    elif histories:
        actions.append("查看之前的回答")
    if searches:
        actions.append("检索")
    if reads:
        actions.append(f"阅读 {sum(len(args.ids) + len(args.urls) for args in reads)} 篇报道")
    if searches:
        # A query's window is left to the process list, where it reads with the result.
        detail = "、".join(subject(args, today) for args in searches)
    elif keywords:
        detail = "、".join(keywords)
    else:
        detail = "、".join(_read_titles(reads, registry))
    return Progress("正在" + _joined(actions), detail)


def found(payloads: list[ToolPayload]) -> str:
    """What a step's calls found, shown while the gather model weighs it.

    A read, or a browse, finishes in about no time, so the status line of its step is replaced before it is seen; its
    result shows here instead.
    """
    stories: set[int] | None = None
    reads: set[int] | None = None
    for payload in payloads:
        if payload.name == "read_articles":
            reads = set() if reads is None else reads
            if payload.ok:
                reads.update(item.article.article_id for item in payload.data.items if item.error is None)
        elif payload.name in (*SEARCH_TOOLS, "check_follows"):
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
    if payload.name in SEARCH_TOOLS:
        args = payload.args
        # A browse names its window in the subject.
        span = window(args, today) if isinstance(args, SearchNewsArgs) else ""
        outcome = _outcome(span, payload.data.hits if payload.ok else [], not payload.ok)
        filters = _filters(args)
        meta = f"仅{'、'.join(filters)}，{outcome}" if filters else outcome
        return [ProcessItem(SEARCH, subject(args, today), meta)]
    if not payload.ok:
        return []
    if payload.name == "check_follows":
        return [_checked(check) for check in payload.data.checks]
    if payload.name == "read_history":
        return [_recalled(payload.args, payload.data)]
    items = []
    if payload.name == "read_articles":
        for item in payload.data.items:
            if item.error is None and item.article.article_id not in read_ids:
                read_ids.add(item.article.article_id)
                items.append(ProcessItem(READ, _title(item.article), _published(item.article)))
    return items


def process(items: list[tuple[str, ProcessItem]]) -> Process | None:
    """The process of a turn from its items, each with the tool that produced it: the summary counts by tool, since
    the lines of read_history do not share one action."""
    if not items:
        return None
    counts = Counter(name for name, _ in items)
    follows, histories = counts["check_follows"], counts["read_history"]
    searches = sum(counts[name] for name in SEARCH_TOOLS)
    parts = []
    if follows:
        parts.append(f"查看 {follows} 个关注项" + (f"、之前的回答 {histories} 次" if histories else ""))
    elif histories:
        parts.append(f"查看之前的回答 {histories} 次")
    if searches:
        parts.append(f"检索 {searches} 次")
    if counts["read_articles"]:
        parts.append(f"阅读 {counts['read_articles']} 篇报道")
    return Process("，".join(parts), tuple(item for _, item in items))


def _joined(actions: list[str]) -> str:
    if len(actions) <= 2:
        return "并".join(actions)
    return "、".join(actions[:-1]) + "并" + actions[-1]


def _read_titles(reads: list[ReadArticlesArgs], registry: EvidenceRegistry) -> list[str]:
    titles = [_title(evidence.article) for args in reads for evidence in map(registry.get, args.ids) if evidence]
    return titles + [(urlsplit(url).hostname or url).removeprefix("www.") for args in reads for url in args.urls]


def _filters(args: SearchNewsArgs | BrowseNewsArgs) -> list[str]:
    names = []
    if isinstance(args, SearchNewsArgs) and args.topic:
        names.append(f"{TOPICS[args.topic]}领域")
    if args.kind:
        names.append(f"{KINDS[args.kind]}类")
    if args.publisher_kind:
        names.append("官方渠道" if args.publisher_kind == "official" else "媒体报道")
    return names


def _outcome(span: str, hits: list, failed: bool) -> str:
    """What a search found in its window, such as 最近 7 天内找到 10 条新闻; span is empty when the subject names it."""
    if failed:
        return "检索失败"
    if span.startswith("最近"):
        span += "内"
    span += _joint(span)
    return f"{span}找到 {len(hits)} 条新闻" if hits else f"{span}没有找到"


def _checked(check: FollowCheck) -> ProcessItem:
    # The English search terms are a translation of the follow and stay in the trace.
    name, checked = check.follow.query, check.follow.last_checked_at
    span = f"{checked.astimezone(user_zone()):%Y-%m-%d} 以来" if checked else f"最近 {check.days} 天"
    outcome = _outcome(span, check.result.hits if check.result else [], check.error is not None)
    return ProcessItem(CHECK, f"{name}{_joint(name)}的新动态", outcome)


def _recalled(args: ReadHistoryArgs, result: HistoryResult) -> ProcessItem:
    count = len(result.items)
    if args.keywords:
        keywords = "、".join(args.keywords)
        return ProcessItem("回顾", keywords, f"找到 {count} 次相关回答") if count else ProcessItem("未找到", keywords, "相关内容")
    # Without keywords the line is one phrase: it has no action, and the page shows it across the row.
    return ProcessItem("", f"查看最近 {count} 次回答" if count else "未找到之前的回答", "")


def _joint(text: str) -> str:
    """The space that separates a date ending text from the Chinese that follows."""
    return " " if text and text[-1].isascii() else ""


def _title(article: Article) -> str:
    return article.title_zh or article.title


def _published(article: Article) -> str:
    return f"{article.publisher} {article.published_at.astimezone(user_zone()):%Y-%m-%d}"
