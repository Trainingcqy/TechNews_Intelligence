from __future__ import annotations

import re
from dataclasses import dataclass, replace

from agent.config import user_zone
from agent.evidence import EvidenceRegistry
from agent.types import Answer, Evidence, Source
from common.urls import _URL_RE

NOT_FOUND_TEXT = {
    "zh": "新闻库中没有找到相关报道。新闻库只收录固定的科技媒体和公司官方渠道的文章，"
    "这件事可能不在收录范围内。也可以换一种说法或放宽时间范围再问。",
    "en": "No relevant reports were found in the news library. The library only covers a fixed set of tech media "
    "and official company channels, so this may be outside its coverage. "
    "You can also rephrase the question or widen the time range and ask again.",
}
BLOCKED_TEXT = {
    "zh": "抱歉，我没能基于现有证据给出有把握的回答。可能数据库中暂无与此直接相关的信息，"
    "建议换一个更具体的实体、时间范围或来源再试。",
    "en": "Sorry, I couldn't produce a well-grounded answer from the available evidence. "
    "There may be no directly relevant information in the database; "
    "try a more specific entity, time range, or source.",
}
ERROR_TEXT = {
    "zh": "抱歉，本次分析未能完成，请稍后重试。",
    "en": "Sorry, this analysis could not be completed. Please try again later.",
}
GREETING_TEXT = {
    "zh": "我可以帮助你基于新闻证据做科技情报分析。你可以直接提出公司、产品、主题、时间范围或要比较的对象。",
    "en": "I can help you analyze tech news based on reported evidence. "
    "Ask about a company, product, topic, time range, or things to compare.",
}

_CJK_BASIC_RE = re.compile(r"[一-龥]")
_EMOJI_RE = re.compile(
    r"(?:[0-9#*]️?⃣)"
    r"|(?:[\U0001F1E6-\U0001F1FF]{2})"
    r"|(?:[\U0001F300-\U0001FAFF☀-➿]️?"
    r"(?:‍[\U0001F300-\U0001FAFF☀-➿]️?)*)"
    r"|[︎️‍]"
)
_SOURCE_HEADER_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:来源|证据来源|sources?|evidence\s+sources?)\s*:?.*",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(r"^\s*STATUS:\s*(answered|insufficient)\s*$", re.IGNORECASE)
_NUMERIC_CITATION_RE = re.compile(r"\[(\d{1,3})\]")
_EID_LIST_RE = re.compile(r"\[(E\d{1,3}(?:\s*[,，、]\s*E\d{1,3})+)\]")
_EID_RE = re.compile(r"\[E(\d{1,3})\]")
_EID_MARKER_RE = re.compile(r"\[E\d{1,3}(?:\s*[,，、]\s*E\d{1,3})*\]")
# Deletions leave a placeholder so that the cleanup in step 5 touches only the spots
# where something was removed, never brackets or spacing the model wrote itself.
_DELETED = "\x00"
_DUPLICATE_MARKER_RE = re.compile(r"(\[\d{1,3}\])(?:[ \t\x00]*\1)+")
_EMPTY_BRACKETS_RE = re.compile(r"[(（\[][ \t\x00]*\x00[ \t\x00]*[)）\]]")
_DELETED_RUN_RE = re.compile(r"[ \t]*\x00[ \t\x00]*")
_NO_SPACE_BEFORE = frozenset("\n，。、；：！？）,.;:!?)")


@dataclass(frozen=True)
class CitationCheck:
    """What citation checking found in the synthesis output."""

    status_line: str | None = None  # answered | insufficient；没有 STATUS 行时为空
    cited: tuple[tuple[str, int], ...] = ()  # 证据编号与重编号后的序号，按首次出现的顺序
    removed_eids: tuple[str, ...] = ()  # 不可引用而被删除的编号
    removed_urls: int = 0
    removed_numeric: int = 0

    @property
    def removed_any(self) -> bool:
        return bool(self.removed_eids or self.removed_urls or self.removed_numeric)


def contains_cjk(text: str) -> bool:
    return bool(_CJK_BASIC_RE.search(text or ""))


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text or "")


def strip_source_section(text: str) -> str:
    lines = (text or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SOURCE_HEADER_RE.match(line.strip()):
            start = i
            break
    if start is None:
        return (text or "").rstrip()
    return "\n".join(lines[:start]).rstrip()


def split_status(raw: str) -> tuple[str | None, str]:
    lines = raw.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        match = _STATUS_RE.match(line)
        if match is None:
            break
        return match.group(1).lower(), "".join(lines[:i] + lines[i + 1 :])
    return None, raw


def resolve_citations(body: str, registry: EvidenceRegistry) -> tuple[str, tuple[Source, ...], CitationCheck]:
    text, removed_numeric = _NUMERIC_CITATION_RE.subn(_DELETED, body)
    text = _EID_LIST_RE.sub(lambda m: "".join(f"[{eid.strip()}]" for eid in re.split(r"[,，、]", m.group(1))), text)
    text, removed_urls = _URL_RE.subn(_DELETED, text)

    citable = registry.citable_ids()
    numbers: dict[str, int] = {}
    removed: dict[str, None] = {}

    def renumber(match: re.Match[str]) -> str:
        eid = f"E{match.group(1)}"
        if eid not in citable:
            removed[eid] = None
            return _DELETED
        numbers.setdefault(eid, len(numbers) + 1)
        return f"[{numbers[eid]}]"

    text = _EID_RE.sub(renumber, text)
    text = _DUPLICATE_MARKER_RE.sub(r"\1", text)
    text = _EMPTY_BRACKETS_RE.sub(_DELETED, text)
    text = _DELETED_RUN_RE.sub(_close_gap, text)

    sources = []
    for eid, n in numbers.items():
        evidence = registry.get(eid)
        sources.append(_source(n, evidence))
    check = CitationCheck(cited=tuple(numbers.items()), removed_eids=tuple(removed),
                          removed_urls=removed_urls, removed_numeric=removed_numeric)
    return text, tuple(sources), check


def finalize_answer(raw: str, registry: EvidenceRegistry, question: str) -> tuple[Answer, CitationCheck]:
    lang = "zh" if contains_cjk(question) else "en"
    if not raw.strip():
        return Answer(status="error", text=ERROR_TEXT[lang], error_code="empty_synthesis"), CitationCheck()
    status, body = split_status(raw)
    text, sources, check = resolve_citations(strip_emoji(body), registry)
    check = replace(check, status_line=status)
    text = text.strip()
    if status == "insufficient":
        return Answer(status="insufficient", text=text, sources=sources), check
    if not sources:
        return Answer(status="blocked", text=BLOCKED_TEXT[lang], error_code="no_valid_citation"), check
    return Answer(status="answered", text=text, sources=sources), check


def clean_direct_reply(text: str, question: str) -> str:
    cleaned = _URL_RE.sub("", strip_emoji(text))
    cleaned = _EID_MARKER_RE.sub("", cleaned).strip()
    if cleaned:
        return cleaned
    return GREETING_TEXT["zh" if contains_cjk(question) else "en"]


def rewrite_history_citations(text: str, sources: list[dict], registry: EvidenceRegistry) -> str:
    def rewrite(match: re.Match[str]) -> str:
        n = int(match.group(1))
        if not 1 <= n <= len(sources):
            return ""
        evidence = registry.by_url(sources[n - 1]["url"])
        return f"[{evidence.eid}]" if evidence else ""

    return _NUMERIC_CITATION_RE.sub(rewrite, text)


def _close_gap(match: re.Match[str]) -> str:
    start, end = match.span()
    before = match.string[start - 1] if start else "\n"
    after = match.string[end] if end < len(match.string) else "\n"
    spaced = any(char in " \t" for char in match.group(0))
    return " " if spaced and before != "\n" and after not in _NO_SPACE_BEFORE else ""


def _source(n: int, evidence: Evidence) -> Source:
    article = evidence.article
    return Source(
        n=n,
        url=article.url,
        title=article.title,
        title_zh=article.title_zh,
        source=article.publisher,
        published_at=f"{article.published_at.astimezone(user_zone()):%Y-%m-%d}",
    )
