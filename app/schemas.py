"""HTTP request and response models for the public API."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class AccessRequest(BaseModel):
    email: EmailStr


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    timezone: str | None = Field(default=None, max_length=64)  # the browser's IANA zone, e.g. Asia/Shanghai


class FollowRequest(BaseModel):
    query: str


class SourceItem(BaseModel):
    n: int
    url: str
    title: str
    title_zh: str = ""
    source: str = ""
    published_at: str = ""


class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    kind: str = "answered"             # Answer.status 的六个取值
    sources: list[SourceItem] = Field(default_factory=list)
    remaining: int
    quota: int
    unlimited: bool = False
    status: str = "active"             # 额度状态，含义不变


class QuotaResponse(BaseModel):
    quota: int
    used: int
    remaining: int
    status: str
    unlimited: bool = False
