from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Article:
    article_id: int
    url: str
    title: str  # 原标题
    title_zh: str  # 中文标题，可为空串
    summary: str
    topics: tuple[str, ...]
    kind: str | None
    publisher: str  # 发布方名称
    publisher_kind: str  # official | media | community | ... | unknown
    lang: str | None
    published_at: datetime  # news.article.event_at
    first_seen_at: datetime
    body_level: str  # full | excerpt | none
    story_id: int


@dataclass(frozen=True)
class StoryStats:
    story_id: int
    report_count: int
    publisher_count: int
    publisher_kinds: tuple[str, ...]
    first_at: datetime
    last_at: datetime


@dataclass(frozen=True)
class StoryHit:
    lead: Article
    stats: StoryStats
    related: tuple[Article, ...]
    match_score: float | None


@dataclass
class Evidence:
    eid: str  # "E1"
    article: Article
    level: Literal["listed", "summary", "fulltext"]
    stats: StoryStats | None = None
    fulltext: str | None = None
    fulltext_total_chars: int = 0
    truncated: bool = False
    history: bool = False  # registered from a past answer and not found again in this turn


@dataclass(frozen=True)
class Source:
    n: int
    url: str
    title: str
    title_zh: str
    source: str  # 发布方名称
    published_at: str  # YYYY-MM-DD


@dataclass(frozen=True)
class ProcessItem:
    action: str  # 检查, 检索 or 阅读
    subject: str
    meta: str


@dataclass(frozen=True)
class Process:
    """The checks, searches and reads of a turn, listed above its answer."""

    summary: str
    items: tuple[ProcessItem, ...]


AnswerStatus = Literal["answered", "insufficient", "direct", "not_found", "blocked", "error", "stopped"]


@dataclass(frozen=True)
class Answer:
    status: AnswerStatus
    text: str  # 正文，引用形如 [1]
    sources: tuple[Source, ...] = ()
    tool_calls: tuple[str, ...] = ()
    followed_queries: tuple[str, ...] = ()  # 本轮检索命中的关注项，供更新查看时间使用
    process: Process | None = None  # None when no tool call was executed
    request_id: str = ""
    error_code: str | None = None


@dataclass(frozen=True)
class Progress:
    """The status line the chat page shows while a turn runs."""

    title: str
    detail: str = ""
