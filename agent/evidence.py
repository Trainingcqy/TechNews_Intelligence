from __future__ import annotations

from agent.config import BEIJING
from agent.types import Article, Discussion, Evidence, StoryStats
from services.urls import url_key

_LEVEL_RANK = {"listed": 0, "summary": 1, "fulltext": 2}
PUBLISHER_KIND_LABELS = {
    "official": "官方",
    "media": "媒体",
    "community": "社区",
    "code_host": "代码与模型托管",
    "paper_host": "论文托管",
    "social": "社交与视频平台",
    "personal": "个人博客",
    "unknown": "未分类",
}


class EvidenceRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}
        self._eid_by_key: dict[tuple[str, str], str] = {}

    def add_article(self, article: Article, level: str, stats: StoryStats | None = None) -> Evidence:
        key = ("article", url_key(article.url))
        eid = self._eid_by_key.get(key)
        if eid is None:
            return self._new(key, doc="article", article=article, discussion=None, level=level, stats=stats)
        evidence = self._items[eid]
        if _LEVEL_RANK[level] > _LEVEL_RANK[evidence.level]:
            evidence.level = level
        if stats is not None:
            evidence.stats = stats
        return evidence

    def add_discussion(self, discussion: Discussion) -> Evidence:
        key = ("discussion", url_key(discussion.url))
        eid = self._eid_by_key.get(key)
        if eid is None:
            return self._new(key, doc="discussion", article=None, discussion=discussion, level="listed", stats=None)
        return self._items[eid]

    def get(self, eid: str) -> Evidence | None:
        return self._items.get(eid)

    def by_url(self, url: str) -> Evidence | None:
        eid = self._eid_by_key.get(("article", url_key(url)))
        return self._items[eid] if eid else None

    def set_fulltext(self, eid: str, text: str, total_chars: int, truncated: bool) -> None:
        evidence = self._items[eid]
        evidence.fulltext = text
        evidence.fulltext_total_chars = total_chars
        evidence.truncated = truncated
        evidence.level = "fulltext"

    def citable_ids(self) -> set[str]:
        return {eid for eid, evidence in self._items.items() if evidence.level != "listed"}

    def items(self) -> list[Evidence]:
        return list(self._items.values())

    def _new(self, key: tuple[str, str], **fields) -> Evidence:
        eid = f"E{len(self._items) + 1}"
        evidence = Evidence(eid=eid, **fields)
        self._items[eid] = evidence
        self._eid_by_key[key] = eid
        return evidence


def render_for_synthesis(registry: EvidenceRegistry, max_chars: int) -> str:
    citable = [evidence for evidence in registry.items() if evidence.level != "listed"]
    article_eids = {e.article.article_id: e.eid for e in citable if e.doc == "article"}
    with_body = [e.eid for e in citable if e.level == "fulltext"]
    while True:
        shown = set(with_body)
        text = "\n\n".join(_render(e, e.eid in shown, article_eids) for e in citable)
        if len(text) <= max_chars or not with_body:
            return text
        with_body.pop()


def _render(evidence: Evidence, with_body: bool, article_eids: dict[int, str]) -> str:
    if evidence.doc == "discussion":
        lines = _render_discussion(evidence, article_eids)
        body_label = "评论节选"
    else:
        lines = _render_article(evidence)
        body_label = "正文"
    if with_body:
        text = evidence.fulltext or ""
        if evidence.truncated:
            lines.append(f"{body_label}，前 {len(text)} 字符，全文 {evidence.fulltext_total_chars} 字符：")
        else:
            lines.append(f"{body_label}，全文 {evidence.fulltext_total_chars} 字符：")
        lines.append(text)
    return "\n".join(lines)


def _render_article(evidence: Evidence) -> list[str]:
    article = evidence.article
    lines = [f"[{evidence.eid}] 标题：{article.title}"]
    if article.title_zh:
        lines.append(f"中文标题：{article.title_zh}")
    meta = [
        f"发布方：{article.publisher}，{PUBLISHER_KIND_LABELS[article.publisher_kind]}",
        f"时间：{article.published_at.astimezone(BEIJING):%Y-%m-%d}",
    ]
    if article.topics:
        meta.append("领域：" + "、".join(article.topics))
    if article.kind:
        meta.append(f"类型：{article.kind}")
    lines.append("｜".join(meta))
    if evidence.stats is not None:
        lines.append(_render_stats(evidence.stats))
    lines.append(f"摘要：{article.summary}")
    if article.body_level != "full":
        lines.append("本文没有入库正文，摘要只依据标题和摘录生成")
    return lines


def _render_stats(stats: StoryStats) -> str:
    parts = [f"{stats.report_count} 篇报道", f"{stats.publisher_count} 个发布方"]
    if "official" in stats.publisher_kinds:
        parts.append("含官方发布")
    if stats.hn_score is not None:
        hn = f"HN 最高 {stats.hn_score} 分"
        if stats.hn_comments is not None:
            hn += f"、{stats.hn_comments} 条评论"
        parts.append(hn)
    parts.append(f"首次 {stats.first_at.astimezone(BEIJING):%Y-%m-%d}")
    parts.append(f"最近 {stats.last_at.astimezone(BEIJING):%Y-%m-%d}")
    return "同一故事：" + "，".join(parts)


def _render_discussion(evidence: Evidence, article_eids: dict[int, str]) -> list[str]:
    discussion = evidence.discussion
    counts = f"{discussion.comment_total} 条评论"
    if discussion.score is not None:
        counts = f"{discussion.score} 分，{counts}"
    meta = ["性质：社区评论，每条是发言者的个人观点", counts]
    article_eid = article_eids.get(discussion.article_id)
    if article_eid:
        meta.append(f"对应文章 [{article_eid}]")
    return [f"[{evidence.eid}] HackerNews 讨论：{discussion.title}", "｜".join(meta)]
