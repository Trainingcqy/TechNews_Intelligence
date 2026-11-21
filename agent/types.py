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
    body_level: str  # full | excerpt | none
    hn_score: int | None
    hn_comments: int | None
    hn_sighting_id: int | None
    story_id: int


@dataclass(frozen=True)
class Discussion:
    sighting_id: int
    article_id: int
    url: str  # HackerNews 讨论页地址
    title: str  # 对应文章的标题
    score: int | None
    comment_total: int
    char_count: int


@dataclass(frozen=True)
class StoryStats:
    story_id: int
    report_count: int
    publisher_count: int
    publisher_kinds: tuple[str, ...]
    hn_score: int | None
    hn_comments: int | None
    hn_sighting_id: int | None
    first_at: datetime
    last_at: datetime


@dataclass(frozen=True)
class StoryHit:
    lead: Article
    stats: StoryStats
    related: tuple[Article, ...]
    discussion: Discussion | None
    match_score: float | None


@dataclass
class Evidence:
    eid: str  # "E1"
    doc: Literal["article", "discussion"]
    article: Article | None
    discussion: Discussion | None
    level: Literal["listed", "summary", "fulltext"]
    stats: StoryStats | None = None
    fulltext: str | None = None
    fulltext_total_chars: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class Source:
    n: int
    url: str
    title: str
    title_zh: str
    source: str  # 发布方名称；讨论为 "HackerNews 讨论"
    published_at: str  # YYYY-MM-DD


AnswerStatus = Literal["answered", "insufficient", "direct", "not_found", "blocked", "error"]


@dataclass(frozen=True)
class Answer:
    status: AnswerStatus
    text: str  # 正文，引用形如 [1]
    sources: tuple[Source, ...] = ()
    tool_calls: tuple[str, ...] = ()
    story_ids: tuple[int, ...] = ()  # 被引用来源所属的故事，供阅读记录使用
    followed_queries: tuple[str, ...] = ()  # 本轮检索命中的关注项，供更新查看时间使用
    request_id: str = ""
    error_code: str | None = None


@dataclass(frozen=True)
class Progress:
    stage: str  # understanding | retrieving | reading | synthesizing
    title: str
    detail: str = ""


@dataclass(frozen=True)
class EvidenceFound:
    eid: str
    title: str
    source: str
    url: str
    published_at: str


@dataclass(frozen=True)
class Delta:  # 逐字流式启用后使用，见第 30 节
    text: str


@dataclass(frozen=True)
class Final:
    answer: Answer


Event = Progress | EvidenceFound | Delta | Final
