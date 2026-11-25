from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from agent import trace_store
from agent.config import AgentConfig
from agent.trace import request_trace
from agent.evidence import EvidenceRegistry
from agent.retrieval.search import SearchResult
from agent.tools import registry
from agent.tools.registry import (
    PreparedCall,
    ReadArticlesArgs,
    SearchNewsArgs,
    ToolOutcome,
    TurnState,
    commit_call,
    prepare_call,
    run_call,
)
from agent.types import Article, StoryHit, StoryStats

PUBLISHED = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
ARTICLE = Article(
    article_id=30, url="https://techcrunch.com/genie-4", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=PUBLISHED, body_level="full", hn_score=None, hn_comments=None, hn_sighting_id=None,
    story_id=5,
)
STATS = StoryStats(story_id=5, report_count=1, publisher_count=1, publisher_kinds=("media",), hn_score=None,
                   hn_comments=None, hn_sighting_id=None, first_at=PUBLISHED, last_at=PUBLISHED)


def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"name": name, "args": args, "id": call_id}


def _prepare(name: str, args: dict, state: TurnState | None = None, config: AgentConfig | None = None):
    return prepare_call(2, _call(name, args), state=state or TurnState(user_urls=set()),
                        config=config or AgentConfig())


def test_search_args_defaults_and_derived_sort():
    browse = SearchNewsArgs()
    assert (browse.query, browse.days, browse.start_date, browse.end_date, browse.topic, browse.kind,
            browse.publisher_kind, browse.sort, browse.limit) == (None, 14, None, None, None, None, None, "importance", 8)
    assert SearchNewsArgs(query="  ").query is None
    assert SearchNewsArgs(query="  ").sort == "importance"
    assert SearchNewsArgs(query="Genie 4").sort == "relevance"
    assert SearchNewsArgs(query="Genie 4", sort="time").sort == "time"
    assert SearchNewsArgs(query="Genie 4", sort="importance").sort == "importance"


@pytest.mark.parametrize("field, good, bad", [
    ("days", (1, 365), (0, 366)),
    ("limit", (1, 20), (0, 21)),
])
def test_search_args_bounds(field, good, bad):
    for value in good:
        assert getattr(SearchNewsArgs(**{field: value}), field) == value
    for value in bad:
        with pytest.raises(ValidationError):
            SearchNewsArgs(**{field: value})


@pytest.mark.parametrize("fields", [
    {"sort": "relevance"},
    {"query": "Genie 4", "sort": "hn"},
    {"end_date": date(2026, 10, 31)},
    {"start_date": date(2026, 11, 1), "end_date": date(2026, 10, 31)},
    {"topic": "hardware"},
    {"kind": "event"},
    {"publisher_kind": "community"},
])
def test_search_args_rejects(fields):
    with pytest.raises(ValidationError):
        SearchNewsArgs(**fields)


def test_search_args_accepts_date_range_and_single_day():
    args = SearchNewsArgs(start_date="2026-10-01", end_date="2026-10-01")
    assert (args.start_date, args.end_date) == (date(2026, 10, 1), date(2026, 10, 1))
    assert SearchNewsArgs(start_date="2026-10-01").end_date is None


def test_search_args_literals_follow_taxonomy():
    schema = SearchNewsArgs.model_json_schema()["properties"]
    topics = schema["topic"]["anyOf"][0]["enum"]
    assert topics[0] == "ai" and topics[-1] == "general" and len(topics) == 12
    assert len(schema["kind"]["anyOf"][0]["enum"]) == 9


def test_read_args_defaults_and_bounds():
    args = ReadArticlesArgs(ids=["E1"])
    assert (args.ids, args.urls, args.max_chars) == (["E1"], [], 4000)
    assert ReadArticlesArgs(urls=["https://a.com/x"]).max_chars == 4000
    assert ReadArticlesArgs(ids=["E1"], max_chars=800).max_chars == 800
    assert ReadArticlesArgs(ids=["E1"], max_chars=12000).max_chars == 12000
    assert len(ReadArticlesArgs(ids=[f"E{i}" for i in range(5)]).ids) == 5
    for fields in ({}, {"ids": [], "urls": []}, {"ids": ["E1"], "max_chars": 799},
                   {"ids": ["E1"], "max_chars": 12001}, {"ids": [f"E{i}" for i in range(6)]},
                   {"urls": [f"https://a.com/{i}" for i in range(6)]}):
        with pytest.raises(ValidationError):
            ReadArticlesArgs(**fields)


def test_prepare_unknown_tool():
    outcome = _prepare("query_news", {"query": "x"})
    assert outcome == ToolOutcome(index=2, call_id="c1", name="query_news", ok=False,
                                  gather_text="未知工具：query_news。可用工具：search_news、read_articles。",
                                  error="未知工具：query_news。可用工具：search_news、read_articles。",
                                  reason="unknown_tool")


def test_prepare_duplicate_call_ignores_key_order():
    state = TurnState(user_urls=set())
    first = _prepare("search_news", {"query": "Genie 4", "days": 7}, state)
    second = _prepare("search_news", {"days": 7, "query": "Genie 4"}, state)
    other = _prepare("search_news", {"query": "Genie 4", "days": 8}, state)
    assert isinstance(first, PreparedCall) and isinstance(other, PreparedCall)
    assert (second.ok, second.reason, second.gather_text) == (False, "duplicate", "与之前的调用完全相同，结果见上文。")


def test_prepare_validation_error_text_lists_fields():
    outcome = _prepare("search_news", {"query": "x", "days": 0, "limit": 21})
    assert (outcome.ok, outcome.reason) == (False, "invalid_args")
    assert outcome.gather_text == (
        "参数错误：days：Input should be greater than or equal to 1；"
        "limit：Input should be less than or equal to 20"
    )
    combined = _prepare("search_news", {"end_date": "2026-10-31"})
    assert combined.gather_text == "参数错误：Value error, end_date requires start_date"


def test_prepare_returns_validated_args():
    prepared = _prepare("search_news", {"query": "Genie 4", "start_date": "2026-10-01"})
    assert prepared == PreparedCall(index=2, call_id="c1", name="search_news",
                                    args=SearchNewsArgs(query="Genie 4", start_date=date(2026, 10, 1)))


def test_prepare_read_quota_truncates_ids_then_urls():
    state = TurnState(user_urls=set(), read_count=5)
    prepared = _prepare("read_articles", {"ids": ["E1", "E2"], "urls": ["https://a.com/1", "https://a.com/2"]},
                        state, AgentConfig(max_read_articles=8))
    assert (prepared.args.ids, prepared.args.urls, prepared.dropped) == (
        ["E1", "E2"], ["https://a.com/1"], ("https://a.com/2",))
    assert state.read_count == 8

    state = TurnState(user_urls=set(), read_count=6)
    prepared = _prepare("read_articles", {"ids": ["E1", "E2", "E3"], "urls": ["https://a.com/1"]},
                        state, AgentConfig(max_read_articles=8))
    assert (prepared.args.ids, prepared.args.urls, prepared.dropped) == (["E1", "E2"], [], ("E3", "https://a.com/1"))
    assert state.read_count == 8


def test_run_call_turns_handler_exception_into_error_payload(monkeypatch):
    def boom(args):
        raise RuntimeError("db down")

    monkeypatch.setattr(registry, "run_search_news", boom)
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    prepared = _prepare("search_news", {"query": "Genie 4"})
    with request_trace(question="q", channel="cli") as trace:
        payload = run_call(prepared, registry=EvidenceRegistry(), state=TurnState(user_urls=set()))

    assert (payload.index, payload.call_id, payload.ok, payload.data, payload.error) == (
        2, "c1", False, None, "工具执行失败：RuntimeError")
    (span,) = trace.spans
    assert payload.span is span
    assert (span.kind, span.name, span.status, span.error_code, span.error_message) == (
        "tool", "search_news", "error", "tool_runtimeerror", "db down")
    assert (span.input["query"], span.attributes) == ("Genie 4", {"call_id": "c1", "index": 2})

    outcome = commit_call(payload, registry=EvidenceRegistry(), state=TurnState(user_urls=set()),
                          config=AgentConfig())
    assert (outcome.index, outcome.ok, outcome.gather_text) == (2, False, "工具执行失败：RuntimeError")


def test_run_call_records_stories_and_weak_match(monkeypatch):
    hit = StoryHit(lead=ARTICLE, stats=STATS, related=(replace(ARTICLE, article_id=31),), discussion=None,
                   match_score=0.2)
    monkeypatch.setattr(registry, "run_search_news", lambda args: SearchResult(hits=[hit], weak_match=True))
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    with request_trace(question="q", channel="cli") as trace:
        run_call(_prepare("search_news", {"query": "Genie 4"}), registry=EvidenceRegistry(),
                 state=TurnState(user_urls=set()))

    (span,) = trace.spans
    assert span.output == {"stories": [{"story_id": 5, "lead": 30, "related": [31], "discussion": None,
                                        "reports": 1, "publishers": 1, "match_score": 0.2}]}
    assert span.warnings == ["low_relevance"]


def test_commit_call_carries_index_and_call_id(monkeypatch):
    monkeypatch.setattr(registry, "run_search_news", lambda args: SearchResult(hits=[], weak_match=False))
    prepared = _prepare("search_news", {"query": "Genie 4"})
    payload = run_call(prepared, registry=EvidenceRegistry(), state=TurnState(user_urls=set()))
    outcome = commit_call(payload, registry=EvidenceRegistry(), state=TurnState(user_urls=set()),
                          config=AgentConfig())
    assert (outcome.index, outcome.call_id, outcome.name, outcome.ok) == (2, "c1", "search_news", True)
