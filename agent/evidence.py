from __future__ import annotations

from datetime import date

from agent.config import user_zone
from agent.types import Article, Evidence, StoryStats
from common.urls import url_key

_LEVEL_RANK = {"listed": 0, "summary": 1, "fulltext": 2}
PUBLISHER_KIND_LABELS = {
    "official": "official",
    "media": "media",
    "community": "community",
    "code_host": "code and model hosting",
    "paper_host": "paper hosting",
    "social": "social and video platform",
    "personal": "personal blog",
    "unknown": "unclassified",
}


def counted(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class EvidenceRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}
        self._eid_by_key: dict[str, str] = {}
        # url_key to the day of the latest past answer that cites it; empty unless a tool read the past answers.
        self.told: dict[str, date] = {}
        # Written by check_follows: the follow and window of each story lead it found, by evidence id, and the follows
        # it checked without finding new reports.
        self.follows: dict[str, str] = {}
        self.follows_without_news: list[str] = []

    def add_article(self, article: Article, level: str, stats: StoryStats | None = None, *,
                    history: bool = False) -> Evidence:
        key = url_key(article.url)
        eid = self._eid_by_key.get(key)
        if eid is None:
            return self._new(key, article=article, level=level, stats=stats, history=history)
        evidence = self._items[eid]
        evidence.history = evidence.history and history
        if _LEVEL_RANK[level] > _LEVEL_RANK[evidence.level]:
            evidence.level = level
        if stats is not None:
            evidence.stats = stats
        return evidence

    def get(self, eid: str) -> Evidence | None:
        return self._items.get(eid)

    def by_url(self, url: str) -> Evidence | None:
        eid = self._eid_by_key.get(url_key(url))
        return self._items[eid] if eid else None

    def set_fulltext(self, eid: str, text: str, total_chars: int, truncated: bool) -> None:
        evidence = self._items[eid]
        evidence.fulltext = text
        evidence.fulltext_total_chars = total_chars
        evidence.truncated = truncated
        evidence.level = "fulltext"
        evidence.history = False

    def citable_ids(self) -> set[str]:
        return {eid for eid, evidence in self._items.items() if evidence.level != "listed"}

    def items(self) -> list[Evidence]:
        return list(self._items.values())

    def _new(self, key: str, **fields) -> Evidence:
        eid = f"E{len(self._items) + 1}"
        evidence = Evidence(eid=eid, **fields)
        self._items[eid] = evidence
        self._eid_by_key[key] = eid
        return evidence


def render_for_synthesis(registry: EvidenceRegistry, max_chars: int) -> tuple[str, list[str]]:
    """The evidence brief, and the ids left out of it: a whole history item, or the body of a read.

    This turn's evidence comes first. Over max_chars, history items go first, last registered first, then the bodies
    of this turn's reads, last read first. The line of the follows checked without new reports leads the brief outside
    max_chars.
    """
    citable = [evidence for evidence in registry.items() if evidence.level != "listed"]
    current = [e for e in citable if not e.history]
    history = [e for e in citable if e.history]
    fulltext = [e.eid for e in current if e.level == "fulltext"]
    with_body = list(fulltext)
    kept = list(history)
    while True:
        shown = set(with_body)
        text = "\n\n".join(_render(e, e.eid in shown, registry.told.get(url_key(e.article.url)),
                                   registry.follows.get(e.eid))
                           for e in current + kept)
        if len(text) <= max_chars or not (kept or with_body):
            left_out = [e.eid for e in history[len(kept):]]
            if registry.follows_without_news:
                text = f"Follows without new reports: {', '.join(registry.follows_without_news)}\n\n{text}"
            return text, left_out + [eid for eid in fulltext if eid not in shown]
        if kept:
            kept.pop()
        else:
            with_body.pop()


def _render(evidence: Evidence, with_body: bool, told_on: date | None, follow: str | None) -> str:
    lines = _render_article(evidence, told_on, follow)
    if with_body:
        text = evidence.fulltext or ""
        if evidence.truncated:
            lines.append(f"Full text, first {len(text)} of {evidence.fulltext_total_chars} characters:")
        else:
            lines.append(f"Full text, {evidence.fulltext_total_chars} characters:")
        lines.append(text)
    return "\n".join(lines)


def _render_article(evidence: Evidence, told_on: date | None, follow: str | None) -> list[str]:
    article = evidence.article
    lines = [f"[{evidence.eid}] Title: {article.title}"]
    if article.title_zh:
        lines.append(f"Chinese title: {article.title_zh}")
    meta = [
        f"Publisher: {article.publisher}, {PUBLISHER_KIND_LABELS[article.publisher_kind]}",
        f"Date: {article.published_at.astimezone(user_zone()):%Y-%m-%d}",
    ]
    if article.topics:
        meta.append("Topics: " + ", ".join(article.topics))
    if article.kind:
        meta.append(f"Type: {article.kind}")
    lines.append(" | ".join(meta))
    if evidence.stats is not None:
        lines.append(_render_stats(evidence.stats))
    if follow is not None:
        lines.append(f"Follow: {follow}")
    if told_on is not None:
        lines.append(f"Cited in the answer on {told_on}")
    lines.append(f"Summary: {article.summary}")
    if article.body_level != "full":
        lines.append("No full text stored: the summary is based only on the title and an excerpt.")
    return lines


def _render_stats(stats: StoryStats) -> str:
    parts = [counted(stats.report_count, "report", "reports"), counted(stats.publisher_count, "publisher", "publishers")]
    if "official" in stats.publisher_kinds:
        parts.append("including an official publisher")
    parts.append(f"first {stats.first_at.astimezone(user_zone()):%Y-%m-%d}")
    parts.append(f"latest {stats.last_at.astimezone(user_zone()):%Y-%m-%d}")
    return "Coverage: " + ", ".join(parts)
