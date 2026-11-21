"""Data classes shared by the ingest stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    published_at: datetime | None
    external_id: str | None
    feed_text: str | None  # feed entry's own body or excerpt, HTML tags removed
    score: int | None
    comment_count: int | None
    channel_time: datetime | None  # when the entry appeared in the channel; submission time for HackerNews


@dataclass(frozen=True)
class ReaderResult:
    ok: bool
    error_kind: str | None
    title: str | None
    published_at: datetime | None
    body: str
    warning: str | None


class Analysis(BaseModel):
    title_zh: str = Field(min_length=2, max_length=80)
    summary: str = Field(min_length=10, max_length=220)
    topics: list[str] = Field(min_length=1, max_length=3)
    kind: str


@dataclass(frozen=True)
class ReferenceHeadline:
    external_id: str
    title: str
    target_url: str | None
    outlet: str | None
    published_at: datetime | None
