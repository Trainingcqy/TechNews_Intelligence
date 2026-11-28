from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from functools import partial

import pytest
from pydantic import ValidationError

from agent import trace_store
from agent.config import AgentConfig
from agent.trace import request_trace
from agent.evidence import EvidenceRegistry
from agent.retrieval.search import SearchResult
from agent.tools import registry, search
from agent.tools.registry import (
    BrowseNewsArgs,
    PreparedCall,
    ReadArticlesArgs,
    SearchNewsArgs,
    ToolOutcome,
    ToolPayload,
    TurnState,
    commit_call,
    prepare_call,
    run_call,
    story_hits,
)
from agent.types import Article, StoryHit, StoryStats

PUBLISHED = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
ARTICLE = Article(
    article_id=30, url="https://techcrunch.com/genie-4", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=PUBLISHED, first_seen_at=PUBLISHED, body_level="full", story_id=5,
)
STATS = StoryStats(story_id=5, report_count=1, publisher_count=1, publisher_kinds=("media",),
                   first_at=PUBLISHED, last_at=PUBLISHED)


def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"name": name, "args": args, "id": call_id}


def _prepare(name: str, args: dict, state: TurnState | None = None, config: AgentConfig | None = None):
    return prepare_call(2, _call(name, args), state=state or TurnState(user_urls=set()),
                        config=config or AgentConfig())


def test_search_and_browse_args_defaults():
    search = SearchNewsArgs(query="Genie 4")
    assert (search.query, search.days, search.start_date, search.end_date, search.topic, search.kind,
            search.publisher_kind, search.sort, search.limit) == ("Genie 4", 14, None, None, None, None, None,
                                                                  "relevance", 8)
    browse = BrowseNewsArgs()
    assert (browse.days, browse.start_date, browse.end_date, browse.topic, browse.kind, browse.publisher_kind,
            browse.sort, browse.limit) == (14, None, None, None, None, None, "importance", 20)
    assert SearchNewsArgs(query=" Genie 4 ").query == " Genie 4 "
    assert SearchNewsArgs(query="Genie 4", sort="time").sort == "time"
    assert SearchNewsArgs(query="Genie 4", sort="importance").sort == "importance"
    assert BrowseNewsArgs(sort="time").sort == "time"


@pytest.mark.parametrize("model, field, good, bad", [
    (SearchNewsArgs, "days", (1, 365), (0, 366)),
    (SearchNewsArgs, "limit", (1, 20), (0, 21)),
    (BrowseNewsArgs, "days", (1, 365), (0, 366)),
    (BrowseNewsArgs, "limit", (1, 20), (0, 21)),
])
def test_search_and_browse_args_bounds(model, field, good, bad):
    required = {"query": "Genie 4"} if model is SearchNewsArgs else {}
    for value in good:
        assert getattr(model(**required, **{field: value}), field) == value
    for value in bad:
        with pytest.raises(ValidationError):
            model(**required, **{field: value})


SHARED_ERRORS = [
    {"end_date": date(2026, 10, 31)},
    {"start_date": date(2026, 11, 1), "end_date": date(2026, 10, 31)},
    {"topic": "hardware"},
    {"kind": "event"},
    {"publisher_kind": "community"},
]


@pytest.mark.parametrize("model, fields", [
    (SearchNewsArgs, {}),
    (SearchNewsArgs, {"query": ""}),
    (SearchNewsArgs, {"query": "  "}),
    (SearchNewsArgs, {"query": "x", "sort": "newest"}),
    (BrowseNewsArgs, {"query": "x"}),
    (BrowseNewsArgs, {"sort": "relevance"}),
    *((SearchNewsArgs, {"query": "x", **fields}) for fields in SHARED_ERRORS),
    *((BrowseNewsArgs, fields) for fields in SHARED_ERRORS),
])
def test_search_and_browse_args_reject(model, fields):
    with pytest.raises(ValidationError):
        model(**fields)


@pytest.mark.parametrize("model", [BrowseNewsArgs, partial(SearchNewsArgs, query="x")])
def test_search_and_browse_args_accept_date_range_and_single_day(model):
    args = model(start_date="2026-10-01", end_date="2026-10-01")
    assert (args.start_date, args.end_date) == (date(2026, 10, 1), date(2026, 10, 1))
    assert model(start_date="2026-10-01").end_date is None


@pytest.mark.parametrize("model", [SearchNewsArgs, BrowseNewsArgs])
def test_search_and_browse_args_literals_follow_taxonomy(model):
    schema = model.model_json_schema()["properties"]
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


UNKNOWN_TOOL_TEXT = ("Unknown tool: query_news. "
                     "Available tools: search_news, browse_news, read_articles, check_follows.")


def test_prepare_unknown_tool():
    outcome = _prepare("query_news", {"query": "x"})
    assert outcome == ToolOutcome(index=2, call_id="c1", name="query_news", ok=False,
                                  gather_text=UNKNOWN_TOOL_TEXT, error=UNKNOWN_TOOL_TEXT, reason="unknown_tool")


def test_prepare_duplicate_call_ignores_key_order():
    state = TurnState(user_urls=set())
    first = _prepare("search_news", {"query": "Genie 4", "days": 7}, state)
    second = _prepare("search_news", {"days": 7, "query": "Genie 4"}, state)
    other = _prepare("search_news", {"query": "Genie 4", "days": 8}, state)
    defaults = _prepare("search_news", {"query": "Genie 4", "days": 7, "limit": 8, "sort": "relevance"}, state)
    assert isinstance(first, PreparedCall) and isinstance(other, PreparedCall)
    assert (second.ok, second.reason, second.gather_text) == (
        False, "duplicate", "Identical to an earlier call; see its result above.")
    # The same call as the first once the defaults are filled in.
    assert (defaults.ok, defaults.reason) == (False, "duplicate")
    # A browse with the same window is a different call.
    browse = _prepare("browse_news", {"days": 7}, state)
    browse_defaults = _prepare("browse_news", {"days": 7, "limit": 20, "sort": "importance"}, state)
    assert isinstance(browse, PreparedCall)
    assert (browse_defaults.ok, browse_defaults.reason) == (False, "duplicate")


def test_prepare_validation_error_text_lists_fields():
    outcome = _prepare("search_news", {"query": "x", "days": 0, "limit": 21})
    assert (outcome.ok, outcome.reason) == (False, "invalid_args")
    assert outcome.gather_text == (
        "Invalid arguments: days: Input should be greater than or equal to 1; "
        "limit: Input should be less than or equal to 20"
    )
    combined = _prepare("search_news", {"query": "x", "end_date": "2026-10-31"})
    assert combined.gather_text == "Invalid arguments: Value error, end_date requires start_date"
    later = _prepare("browse_news", {"start_date": "2026-11-01", "end_date": "2026-10-31"})
    assert later.gather_text == "Invalid arguments: Value error, start_date must not be later than end_date"
    unknown = _prepare("search_news", {"query": "x", "query_placeholder": "x"})
    assert (unknown.reason, unknown.gather_text) == (
        "invalid_args", "Invalid arguments: query_placeholder: Extra inputs are not permitted")
    assert _prepare("search_news", {}).gather_text == "Invalid arguments: query: Field required"
    assert _prepare("search_news", {"query": " "}).gather_text == (
        "Invalid arguments: query: Value error, query must not be blank")
    assert _prepare("browse_news", {"query": "x"}).gather_text == (
        "Invalid arguments: query: Extra inputs are not permitted")


def test_prepare_returns_validated_args():
    prepared = _prepare("search_news", {"query": "Genie 4", "start_date": "2026-10-01"})
    assert prepared == PreparedCall(index=2, call_id="c1", name="search_news",
                                    args=SearchNewsArgs(query="Genie 4", start_date=date(2026, 10, 1)))
    prepared = _prepare("browse_news", {"topic": "ai"})
    assert prepared == PreparedCall(index=2, call_id="c1", name="browse_news", args=BrowseNewsArgs(topic="ai"))


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
        2, "c1", False, None, "Tool failed: RuntimeError")
    (span,) = trace.spans
    assert payload.span is span
    assert (span.kind, span.name, span.status, span.error_code, span.error_message) == (
        "tool", "search_news", "error", "tool_runtimeerror", "db down")
    assert (span.input["query"], span.attributes) == ("Genie 4", {"call_id": "c1", "index": 2})

    outcome = commit_call(payload, registry=EvidenceRegistry(), state=TurnState(user_urls=set()),
                          config=AgentConfig())
    assert (outcome.index, outcome.ok, outcome.gather_text) == (2, False, "Tool failed: RuntimeError")


def test_run_call_records_stories_and_weak_match(monkeypatch):
    hit = StoryHit(lead=ARTICLE, stats=STATS, related=(replace(ARTICLE, article_id=31),), match_score=0.2)
    monkeypatch.setattr(registry, "run_search_news", lambda args: SearchResult(hits=[hit], weak_match=True))
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    with request_trace(question="q", channel="cli") as trace:
        run_call(_prepare("search_news", {"query": "Genie 4"}), registry=EvidenceRegistry(),
                 state=TurnState(user_urls=set()))

    (span,) = trace.spans
    assert span.output == {"stories": [{"story_id": 5, "lead": 30, "related": [31], "reports": 1, "publishers": 1, "match_score": 0.2}]}
    assert span.warnings == ["low_relevance"]


def test_commit_call_carries_index_and_call_id(monkeypatch):
    monkeypatch.setattr(registry, "run_search_news", lambda args: SearchResult(hits=[], weak_match=False))
    prepared = _prepare("search_news", {"query": "Genie 4"})
    payload = run_call(prepared, registry=EvidenceRegistry(), state=TurnState(user_urls=set()))
    outcome = commit_call(payload, registry=EvidenceRegistry(), state=TurnState(user_urls=set()),
                          config=AgentConfig())
    assert (outcome.index, outcome.call_id, outcome.name, outcome.ok) == (2, "c1", "search_news", True)


def test_run_call_of_a_browse_records_its_stories(monkeypatch):
    hit = StoryHit(lead=ARTICLE, stats=STATS, related=(), match_score=None)
    monkeypatch.setattr(registry, "run_browse_news", lambda args: SearchResult(hits=[hit], weak_match=False))
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    with request_trace(question="q", channel="cli") as trace:
        payload = run_call(_prepare("browse_news", {"topic": "ai"}), registry=EvidenceRegistry(),
                           state=TurnState(user_urls=set()))

    (span,) = trace.spans
    assert (span.name, span.input["topic"], span.input["sort"]) == ("browse_news", "ai", "importance")
    assert span.output == {"stories": [{"story_id": 5, "lead": 30, "related": [], "reports": 1, "publishers": 1,
                                        "match_score": None}]}
    assert story_hits(payload) == [hit]


def test_search_and_browse_pass_their_arguments_to_search_stories(monkeypatch):
    calls = []
    monkeypatch.setattr(search, "search_stories",
                        lambda **kwargs: calls.append(kwargs) or SearchResult(hits=[], weak_match=False))
    search.run_search_news(SearchNewsArgs(query="Genie 4", days=3, start_date="2026-11-01", end_date="2026-11-02",
                                          topic="ai", kind="release", publisher_kind="media", sort="time", limit=5))
    search.run_browse_news(BrowseNewsArgs())
    search.run_browse_news(BrowseNewsArgs(kind="research", sort="time"))

    assert calls == [
        {"query": "Genie 4", "days": 3, "start_date": date(2026, 11, 1), "end_date": date(2026, 11, 2),
         "topic": "ai", "kinds": ("release",), "publisher_kind": "media", "sort": "time", "limit": 5},
        {"query": None, "days": 14, "start_date": None, "end_date": None, "topic": None, "kinds": None,
         "publisher_kind": None, "sort": "importance", "limit": 20},
        {"query": None, "days": 14, "start_date": None, "end_date": None, "topic": None, "kinds": ("research",),
         "publisher_kind": None, "sort": "time", "limit": 20},
    ]


def _commit(name: str, args, hits: list[StoryHit]) -> ToolOutcome:
    payload = ToolPayload(index=0, call_id="c0", name=name, args=args, ok=True,
                          data=SearchResult(hits=hits, weak_match=False))
    return commit_call(payload, registry=EvidenceRegistry(), state=TurnState(user_urls=set()), config=AgentConfig())


def test_search_and_browse_texts_name_their_tool():
    hit = StoryHit(lead=ARTICLE, stats=STATS, related=(), match_score=None)
    found = _commit("search_news", SearchNewsArgs(query="Genie 4", kind="release", start_date="2026-11-01",
                                                  sort="time"), [hit])
    assert (found.name, found.ok) == ("search_news", True)
    assert found.gather_text.startswith(
        'search_news results, query="Genie 4", kind=release, 2026-11-01 to today, by time: 1 story\n'
        "[E1] Genie 4 adds audio\n")
    browsed = _commit("browse_news", BrowseNewsArgs(topic="ai", days=7), [hit])
    assert (browsed.name, browsed.ok) == ("browse_news", True)
    assert browsed.gather_text.startswith(
        "browse_news results, topic=ai, last 7 days, by importance: 1 story\n[E1] Genie 4 adds audio\n")


def test_search_and_browse_each_have_their_no_result_text():
    nothing = _commit("search_news", SearchNewsArgs(query="Genie 4"), [])
    assert (nothing.name, nothing.ok, nothing.gather_text) == (
        "search_news", True,
        'search_news found no results, query="Genie 4", last 14 days. '
        "You can widen the time window, use different English wording, or remove filters, and try again.")
    nothing = _commit("browse_news", BrowseNewsArgs(start_date="2026-11-01", end_date="2026-11-02"), [])
    assert (nothing.name, nothing.ok, nothing.gather_text) == (
        "browse_news", True,
        "browse_news found no results, 2026-11-01 to 2026-11-02. "
        "You can widen the time window or remove filters, and try again.")
