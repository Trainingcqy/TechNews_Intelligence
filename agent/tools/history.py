"""read_history: the answers the user received before, the lines of them that mention keywords, and the reports those
lines cite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from agent.citations import _NUMERIC_CITATION_RE, rewrite_history_citations
from agent.config import user_zone
from agent.evidence import counted
from agent.memory import PastAnswer, past_answers, told_dates
from agent.retrieval.stories import fetch_articles_by_url_keys
from agent.types import Article
from common import prompt_text
from common.db import get_conn, put_conn
from common.urls import url_key

if TYPE_CHECKING:
    from .registry import PreparedCall, ReadHistoryArgs, ToolOutcome

HEAD_CHARS = 300  # of each answer listed without keywords
FALLBACK_CHARS = 1500  # of a matched answer in which no line mentions a keyword
MAX_LINES = 8
QUESTION_CHARS = 200


@dataclass(frozen=True)
class HistoryItem:
    answer: PastAnswer
    lines: list[str]  # taken from the answer text
    cited: list[str]  # url_keys of the sources the lines cite, in order of first citation; empty without keywords


@dataclass(frozen=True)
class HistoryResult:
    items: list[HistoryItem]  # the answers listed or matched, oldest first
    articles: dict[str, Article]  # the cited sources found in the database, by url_key
    told: dict[str, date]


def run_read_history(prepared: PreparedCall, *, registry, state) -> HistoryResult:
    args = prepared.args
    if state.history_reader is None:
        return HistoryResult([], {}, {})
    answers = past_answers(state.history_reader())
    told = told_dates(answers)
    if not args.keywords:
        return HistoryResult([HistoryItem(a, _head(a.text, HEAD_CHARS), []) for a in answers[-args.limit:]], {}, told)

    matched = [a for a in answers if _mentions(a.question, args.keywords) or _mentions(a.text, args.keywords)]
    items = []
    for answer in matched[-args.limit:]:
        lines = _excerpt(answer.text, args.keywords)
        items.append(HistoryItem(answer, lines, _cited(lines, answer.sources)))
    keys = list(dict.fromkeys(key for item in items for key in item.cited))
    conn = get_conn()
    try:
        cur = conn.cursor()
        by_key = {url_key(a.url): a for a in fetch_articles_by_url_keys(cur, keys)}
        cur.close()
    finally:
        put_conn(conn)
    return HistoryResult(items, {key: by_key[key] for key in keys if key in by_key}, told)


def history_trace_output(result: HistoryResult) -> dict:
    return {"answers": len(result.items), "articles": [article.article_id for article in result.articles.values()]}


def commit_read_history(args: ReadHistoryArgs, result: HistoryResult, *, registry) -> ToolOutcome:
    # registry.py imports this module, so the import is deferred to call time.
    from .registry import ToolOutcome

    registry.told.update(result.told)
    keywords = ", ".join(args.keywords)
    if not result.told:
        text = "read_history: no past answers on record."
    elif not result.items:
        text = (f'read_history found no answers containing "{keywords}". '
                + prompt_text.text("tools.read_history.no_result_hint"))
    elif not args.keywords:
        text = _render_recent(result, registry)
    else:
        text = _render_matched(keywords, result, registry)
    return ToolOutcome(index=0, call_id="", name="read_history", ok=True, gather_text=text)


def _render_recent(result: HistoryResult, registry) -> str:
    lines = [f"read_history results: {counted(len(result.items), 'most recent answer', 'most recent answers')}"]
    for item in result.items:
        answer = item.answer
        cited = counted(len(answer.sources), "report", "reports")
        lines.append(f"Answer on {answer.day}, question: {_question(answer)}, {cited} cited")
        lines.extend("  " + rewrite_history_citations(line, answer.sources, registry) for line in item.lines)
    return "\n".join(lines)


def _render_matched(keywords: str, result: HistoryResult, registry) -> str:
    lines = [f'read_history results, keywords="{keywords}": {counted(len(result.items), "answer", "answers")}']
    for item in result.items:
        answer = item.answer
        cited = [registry.add_article(result.articles[key], "summary") for key in item.cited if key in result.articles]
        lines.append(f"Answer on {answer.day}, question: {_question(answer)}")
        lines.extend("  " + rewrite_history_citations(line, answer.sources, registry) for line in item.lines)
        if cited:
            lines.append("  Cited: " + "; ".join(
                f'[{e.eid}] "{e.article.title}" {e.article.publisher} · '
                f"{e.article.published_at.astimezone(user_zone()):%Y-%m-%d}" for e in cited))
        missing = sum(key not in result.articles for key in item.cited)
        if missing:
            lines.append(f"  {counted(missing, 'cited report is', 'cited reports are')} no longer in the database.")
    if result.articles:
        newest = max(a.published_at for a in result.articles.values()).astimezone(user_zone()).date()
        lines.append(f"Newest report cited: {newest}. To look for later reports, search from "
                     f"{newest - timedelta(days=2)}.")
    return "\n".join(lines)


def _mentions(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _head(text: str, chars: int) -> list[str]:
    return [line for line in text[:chars].splitlines() if line.strip()]


def _excerpt(text: str, keywords: list[str]) -> list[str]:
    """The lines that mention a keyword: a heading line with its section, any other line with its heading line."""
    lines = [line for line in text.splitlines() if line.strip()]
    levels = [_level(line) for line in lines]
    taken: set[int] = set()
    for i, line in enumerate(lines):
        if not _mentions(line, keywords):
            continue
        if levels[i]:
            end = next((j for j in range(i + 1, len(lines)) if 0 < levels[j] <= levels[i]), len(lines))
            taken.update(range(i, end))
        else:
            taken.add(i)
            heading = next((j for j in range(i - 1, -1, -1) if levels[j]), None)
            if heading is not None:
                taken.add(heading)
    if not taken:
        return _head(text, FALLBACK_CHARS)
    return [lines[i] for i in sorted(taken)][:MAX_LINES]


def _level(line: str) -> int:
    """The number of # that start a heading line; 0 for other lines."""
    stripped = line.lstrip()
    return len(stripped) - len(stripped.lstrip("#"))


def _cited(lines: list[str], sources: list[dict]) -> list[str]:
    # [n] is the n-th source, as rewrite_history_citations reads it.
    numbers = [int(n) for n in _NUMERIC_CITATION_RE.findall("\n".join(lines))]
    return list(dict.fromkeys(url_key(sources[n - 1]["url"]) for n in numbers if 1 <= n <= len(sources)))


def _question(answer: PastAnswer) -> str:
    return " ".join(answer.question.split())[:QUESTION_CHARS]
