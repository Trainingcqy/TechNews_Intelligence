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


@dataclass(frozen=True)
class ReaderResult:
    ok: bool
    error_kind: str | None
    title: str | None
    published_at: datetime | None
    body: str  # the article text when ok; otherwise Reader's whole content, for the failure detail
    warning: str | None
    retry_headers: dict[str, str]  # request headers that the warning suggests for one more request
    paywalled: bool = False  # the body is only the free part of a paywalled article


class Analysis(BaseModel):
    summary: str = Field(min_length=40, max_length=800)
    topics: list[str] = Field(min_length=1, max_length=3)
    kind: str


@dataclass(frozen=True)
class ReferenceHeadline:
    external_id: str
    title: str
    target_url: str | None
    outlet: str | None
    published_at: datetime | None
