from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage

from agent import context
from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.types import Article
from services.urls import url_key

BASE = Article(
    article_id=0, url="", title="t", title_zh="", summary="", topics=(), kind=None,
    publisher="TechCrunch", publisher_kind="media", lang="en",
    published_at=datetime(2026, 11, 12, tzinfo=timezone.utc), body_level="full",
    hn_score=None, hn_comments=None, hn_sighting_id=None, story_id=1,
)
HN_URL = "https://news.ycombinator.com/item?id=42"


class _Conn:
    def cursor(self):
        return self

    def close(self):
        pass


def _patch(monkeypatch, urls=(), fail=False):
    stored = {url_key(url): replace(BASE, article_id=i, url=url) for i, url in enumerate(urls, 1)}
    lookups = []

    def fetch(cur, keys):
        lookups.append(list(keys))
        if fail:
            raise RuntimeError("db down")
        return [stored[key] for key in keys if key in stored]

    monkeypatch.setattr(context, "get_conn", _Conn)
    monkeypatch.setattr(context, "put_conn", lambda conn: None)
    monkeypatch.setattr(context, "fetch_articles_by_url_keys", fetch)
    return lookups


def _user(text):
    return {"role": "user", "parts": [{"text": text}]}


def _model(text, **fields):
    return {"role": "model", "parts": [{"text": text}], **fields}


def _pairs(ctx):
    return [(type(m), m.content) for m in ctx.messages]


def test_keeps_last_turns_but_collects_user_urls_from_all_history(monkeypatch):
    lookups = _patch(monkeypatch, urls=["https://a.com/old", "https://a.com/new"])
    history = [
        _user("q1 https://u.com/1"), _model("a1[1]", citation_urls=["https://a.com/old"]),
        _user("q2"), _model("a2"),
        _user("q3"), _model("a3[1]", citation_urls=["https://a.com/new"]),
    ]
    registry = EvidenceRegistry()

    ctx = context.build_context(history, registry, AgentConfig(history_turns=2))

    assert _pairs(ctx) == [(HumanMessage, "q2"), (AIMessage, "a2"), (HumanMessage, "q3"), (AIMessage, "a3[E1]")]
    assert lookups == [["a.com/new"]]
    assert ctx.user_urls == {"u.com/1"}


def test_new_format_registers_sources_once_skipping_discussions(monkeypatch):
    lookups = _patch(monkeypatch, urls=["https://a.com/1", HN_URL])
    sources = [
        {"n": 1, "url": "https://www.a.com/1?utm_source=x", "source": "TechCrunch"},
        {"n": 2, "url": HN_URL, "source": "HackerNews 讨论"},
        {"n": 3, "url": "https://gone.com/x", "source": "Wired"},
    ]
    history = [_user("q"), _model("甲[1]乙[2]丙[3]丁\n\n来源：\n[1] https://a.com/1", status="answered", sources=sources)]
    registry = EvidenceRegistry()

    ctx = context.build_context(history, registry, AgentConfig(history_answer_chars=7))

    assert lookups == [["a.com/1", "gone.com/x"]]
    assert [(e.eid, e.article.url, e.level) for e in registry.items()] == [("E1", "https://a.com/1", "summary")]
    assert ctx.messages[1].content == "甲[E1]乙丙"


def test_old_format_citation_urls_and_messages_without_sources(monkeypatch):
    _patch(monkeypatch, urls=["https://a.com/1", "https://a.com/2"])
    history = [
        _user("q1"), _model("x[2]y[1]z[3]", citation_urls=["https://a.com/1", "https://a.com/2"]),
        _user("q2"), _model("m[1]n\nSources:\n[1] https://a.com/1"),
    ]

    ctx = context.build_context(history, EvidenceRegistry(), AgentConfig())

    assert [m.content for m in ctx.messages[1::2]] == ["x[E2]y[E1]z", "mn"]


def test_lookup_failure_still_returns_messages(monkeypatch):
    lookups = _patch(monkeypatch, urls=["https://a.com/1"], fail=True)
    history = [_user("q"), _model("a[1]", sources=[{"n": 1, "url": "https://a.com/1", "source": "TechCrunch"}])]
    registry = EvidenceRegistry()

    ctx = context.build_context(history, registry, AgentConfig())

    assert lookups == [["a.com/1"]]
    assert registry.items() == []
    assert _pairs(ctx) == [(HumanMessage, "q"), (AIMessage, "a")]
