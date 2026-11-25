from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from agent import trace_store
from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.tools import read
from agent.trace import request_trace
from agent.tools.registry import PreparedCall, ReadArticlesArgs, TurnState, prepare_call
from agent.types import Article
from common.urls import url_key

ARTICLE = Article(
    article_id=30, url="https://www.techcrunch.com/genie-4/", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc), body_level="full", story_id=5,
)


def _fake_db(monkeypatch, *, articles=(), bodies=None):
    class Conn:
        def cursor(self):
            return self

        def close(self):
            pass

    def by_keys(cur, keys):
        return [a for a in articles if url_key(a.url) in keys]

    monkeypatch.setattr(read, "get_conn", Conn)
    monkeypatch.setattr(read, "put_conn", lambda conn: None)
    monkeypatch.setattr(read, "fetch_articles_by_url_keys", by_keys)
    monkeypatch.setattr(read, "fetch_bodies", lambda cur, ids: {i: t for i, t in (bodies or {}).items() if i in ids})


def _read(registry, state=None, **fields):
    prepared = PreparedCall(index=0, call_id="c", name="read_articles", args=ReadArticlesArgs(**fields))
    result = read.run_read_articles(prepared, registry=registry, state=state or TurnState(user_urls=set()))
    return result, read.commit_read_articles(prepared.args, result, registry=registry)


def test_unknown_id_fails_and_all_failed_is_not_ok(monkeypatch):
    _fake_db(monkeypatch)
    result, outcome = _read(EvidenceRegistry(), ids=["E9"])
    assert result.items[0].error == "unknown id"
    assert outcome.ok is False
    assert outcome.gather_text == "read_articles results:\n[E9] read failed: unknown id."


def test_url_must_come_from_user_unless_mcp(monkeypatch):
    _fake_db(monkeypatch, articles=[ARTICLE], bodies={30: "正文"})
    url = "https://techcrunch.com/genie-4"

    result, outcome = _read(EvidenceRegistry(), urls=[url])
    assert result.items[0].error == "the link was not provided by the user"
    assert outcome.gather_text.endswith(f"[{url}] read failed: the link was not provided by the user.")

    for state in (TurnState(user_urls={url_key(url)}), TurnState(user_urls=set(), allow_any_library_url=True)):
        result, outcome = _read(EvidenceRegistry(), state, urls=[url])
        assert result.items[0].error is None and outcome.ok is True


def test_url_not_in_library(monkeypatch):
    _fake_db(monkeypatch)
    url = "https://example.com/other"
    result, outcome = _read(EvidenceRegistry(), TurnState(user_urls={url_key(url)}), urls=[url])
    assert result.items[0].error == "the link is not in the news database"
    assert outcome.ok is False


def test_read_limit_exhausted():
    state = TurnState(user_urls=set(), read_count=8)
    outcome = prepare_call(0, {"name": "read_articles", "args": {"ids": ["E1"]}, "id": "c"},
                           state=state, config=AgentConfig(max_read_articles=8))
    assert (outcome.ok, outcome.reason, outcome.gather_text) == (False, "read_quota", "The read limit of this request has been reached.")
    assert state.read_count == 8


def test_targets_over_the_read_quota_are_reported(monkeypatch):
    _fake_db(monkeypatch, bodies={30: "正文"})
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")
    state = TurnState(user_urls=set(), read_count=7)
    prepared = prepare_call(0, {"name": "read_articles", "args": {"ids": ["E1", "E2"], "urls": ["https://a.com/x"]},
                                "id": "c"}, state=state, config=AgentConfig(max_read_articles=8))

    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    with request_trace(question="q", channel="web") as trace:
        result = read.run_read_articles(prepared, registry=registry, state=state)
    outcome = read.commit_read_articles(prepared.args, result, registry=registry)

    assert trace.warnings == ["read_targets_dropped"]
    assert [(item.target, item.error) for item in result.items] == [
        ("E1", None), ("E2", "over the read limit of this request"),
        ("https://a.com/x", "over the read limit of this request")]
    assert outcome.gather_text.splitlines()[-2:] == [
        "[E2] read failed: over the read limit of this request.",
        "[https://a.com/x] read failed: over the read limit of this request."]
    assert read.read_trace_output(result)["items"][1] == {
        "target": "E2", "article_id": None, "chars": 0, "total_chars": 0,
        "error": "over the read limit of this request"}


def test_article_without_body_registers_summary_and_skips_fetch(monkeypatch):
    excerpt = replace(ARTICLE, body_level="excerpt")
    _fake_db(monkeypatch, articles=[excerpt], bodies={30: "不应读到"})
    registry = EvidenceRegistry()
    url = "https://techcrunch.com/genie-4"

    result, outcome = _read(registry, TurnState(user_urls={url_key(url)}), urls=[url])

    assert (result.items[0].text, result.items[0].error) == (None, "no full text stored")
    assert registry.get("E1").level == "summary"
    assert outcome.gather_text == "read_articles results:\n[E1] read failed: no full text stored."
    assert outcome.ok is False


def test_new_evidence_leaves_out_items_already_citable(monkeypatch):
    _fake_db(monkeypatch, bodies={30: "正文", 31: "正文"})
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")
    registry.add_article(replace(ARTICLE, article_id=31, url="https://www.theverge.com/genie-4"), "listed")

    _, outcome = _read(registry, ids=["E1", "E2"])

    assert registry.get("E1").level == "fulltext"
    assert [evidence.eid for evidence in outcome.new_evidence] == ["E2"]


def test_truncation_and_preview(monkeypatch):
    body = "a" * 700 + "b" * 300 + "c" * 500
    _fake_db(monkeypatch, bodies={30: body})
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "listed")

    result, outcome = _read(registry, ids=["E1", "E5"], max_chars=1000)

    evidence = registry.get("E1")
    assert (evidence.level, evidence.fulltext, evidence.fulltext_total_chars, evidence.truncated) == (
        "fulltext", body[:1000], 1500, True)
    assert outcome.gather_text.splitlines() == [
        "read_articles results:",
        '[E1] "Genie 4 adds audio" TechCrunch · 2026-11-12 | 1500 characters in full, first 1000 read',
        "Opening: " + body[:800],
        "[E5] read failed: unknown id.",
    ]
    assert outcome.ok is True
