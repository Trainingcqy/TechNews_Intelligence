"""MCP server: tools, the corpus resource, call results and stdio dispatch."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent import trace_store
from agent.config import using_zone
from agent.mcp import server as server_mod
from agent.mcp.server import InProcessMCPServer, build_newsdb_server
from agent.mcp.stdio_server import _handle_request
from agent.retrieval import corpus
from agent.retrieval.search import SearchResult
from agent.tools import read, registry
from agent.trace import request_trace
from agent.types import Article, StoryHit, StoryStats
from common.urls import url_key

ARTICLE = Article(
    article_id=30, url="https://www.techcrunch.com/genie-4/", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc), body_level="full", story_id=5,
)
SECOND = replace(ARTICLE, article_id=31, url="https://www.theverge.com/genie-4", title="Genie 4 hands-on",
                 publisher="The Verge")


@pytest.fixture()
def fake_db(monkeypatch):
    class Conn:
        def cursor(self):
            return self

        def close(self):
            pass

    monkeypatch.setattr(read, "get_conn", Conn)
    monkeypatch.setattr(read, "put_conn", lambda conn: None)
    monkeypatch.setattr(read, "fetch_articles_by_url_keys",
                        lambda cur, keys: [a for a in (ARTICLE, SECOND) if url_key(a.url) in keys])
    monkeypatch.setattr(read, "fetch_bodies", lambda cur, ids: {i: "文章正文" for i in ids})


def _newsdb_server(monkeypatch) -> InProcessMCPServer:
    monkeypatch.setattr(server_mod, "corpus_info", lambda: "Range: 3 articles")
    return build_newsdb_server()


def test_corpus_lookup_failure_returns_the_unavailable_text_and_warns(monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(corpus, "_cache", None)
    monkeypatch.setattr(corpus, "get_conn", broken)
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    with request_trace(question="q", channel="web") as trace:
        assert corpus.corpus_info() == corpus.UNAVAILABLE
    assert trace.warnings == ["corpus_unavailable"]


def test_corpus_range_is_rendered_in_the_user_zone(monkeypatch):
    # 11-20 00:00 and 11-24 20:00 UTC: 11-20 and 11-25 in Beijing, 11-19 and 11-24 in New York.
    rows = [("media", 3, datetime(2026, 11, 20, tzinfo=timezone.utc), datetime(2026, 11, 24, 20, tzinfo=timezone.utc))]
    monkeypatch.setattr(corpus, "_cache", (time.monotonic(), (rows, [])))
    assert "from 2026-11-20 to 2026-11-25" in corpus.corpus_info()
    with using_zone(ZoneInfo("America/New_York")):
        assert "from 2026-11-19 to 2026-11-24" in corpus.corpus_info()


def test_corpus_roles_name_other_publishers_only_when_there_are_some(monkeypatch):
    day = datetime(2026, 11, 20, tzinfo=timezone.utc)
    rows = [("media", 3, day, day), ("official", 2, day, day)]
    monkeypatch.setattr(corpus, "_cache", (time.monotonic(), (rows, [])))
    assert "Articles by publisher role: official 2, media 3." in corpus.corpus_info()
    monkeypatch.setattr(corpus, "_cache", (time.monotonic(), ([*rows, ("unknown", 1, day, day)], [])))
    assert "Articles by publisher role: official 2, media 3, other 1." in corpus.corpus_info()


def test_newsdb_server_exposes_three_tools_and_one_resource(monkeypatch):
    server = _newsdb_server(monkeypatch)
    assert [row["qualified_name"] for row in server.list_tools()] == [
        "mcp__newsdb__browse_news", "mcp__newsdb__read_articles", "mcp__newsdb__search_news"]
    assert [row["uri"] for row in server.list_resources()] == ["news://corpus"]
    assert server.read_resource("news://corpus") == {
        "uri": "news://corpus", "mimeType": "text/plain", "text": "Range: 3 articles"}


def test_read_unknown_resource_raises(monkeypatch):
    with pytest.raises(KeyError):
        _newsdb_server(monkeypatch).read_resource("news://stats")


def test_call_tool_reads_library_url_without_user_provided_check(monkeypatch, fake_db):
    result = _newsdb_server(monkeypatch).call_tool("read_articles", {"urls": ["https://techcrunch.com/genie-4"]})
    assert result == {
        "status": "ok",
        "text": ('read_articles results:\n[E1] "Genie 4 adds audio" TechCrunch · 2026-11-12 | 4 characters in full; '
                 'added to the evidence'),
        "evidence": [{"eid": "E1", "url": ARTICLE.url, "title": "Genie 4 adds audio",
                      "source": "TechCrunch", "published_at": "2026-11-12"}],
    }


@pytest.mark.parametrize("tool, args, header", [
    ("search_news", {"query": "Genie 4"}, 'search_news results, query="Genie 4", last 14 days, by relevance'),
    ("browse_news", {"topic": "ai"}, "browse_news results, topic=ai, last 14 days, by importance"),
])
def test_call_tool_search_and_browse_list_every_registered_evidence(monkeypatch, tool, args, header):
    related = replace(ARTICLE, article_id=31, url="https://example.com/other", title="Other report",
                      publisher="Example", published_at=datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc))
    stats = StoryStats(story_id=5, report_count=2, publisher_count=2, publisher_kinds=("media",),
                       first_at=related.published_at, last_at=ARTICLE.published_at)
    hit = StoryHit(lead=ARTICLE, stats=stats, related=(related,), match_score=None)
    monkeypatch.setattr(registry, f"run_{tool}", lambda args: SearchResult(hits=[hit], weak_match=False))

    result = _newsdb_server(monkeypatch).call_tool(tool, args)

    assert result["status"] == "ok"
    assert result["text"].startswith(header + ": 1 story\n[E1] Genie 4 adds audio")
    assert [(row["eid"], row["source"], row["published_at"]) for row in result["evidence"]] == [
        ("E1", "TechCrunch", "2026-11-12"), ("E2", "Example", "2026-11-10")]


def test_call_tool_errors_have_status_error(monkeypatch, fake_db):
    server = _newsdb_server(monkeypatch)
    assert server.call_tool("read_articles", {"urls": ["https://example.com/other"]}) == {
        "status": "error",
        "text": "read_articles results:\n[https://example.com/other] read failed: the link is not in the news database.",
        "evidence": [],
    }
    assert server.call_tool("read_articles", {})["status"] == "error"
    unknown = server.call_tool("compare_sources", {})
    assert (unknown["status"], unknown["text"], unknown["evidence"]) == (
        "error",
        "Unknown tool: compare_sources. Available tools: search_news, browse_news, read_articles, check_follows.", [])


def test_each_call_starts_a_new_registry(monkeypatch, fake_db):
    server = _newsdb_server(monkeypatch)
    first = server.call_tool("read_articles", {"urls": [ARTICLE.url]})
    second = server.call_tool("read_articles", {"urls": [SECOND.url]})
    assert [row["eid"] for row in first["evidence"]] == ["E1"]
    assert [row["eid"] for row in second["evidence"]] == ["E1"]
    again = server.call_tool("read_articles", {"urls": [ARTICLE.url]})
    assert again["status"] == "ok"


def test_stdio_tools_call_maps_result_fields(monkeypatch, fake_db):
    server = _newsdb_server(monkeypatch)

    def call(arguments):
        return _handle_request(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "read_articles", "arguments": arguments}})["result"]

    ok = call({"urls": [ARTICLE.url]})
    assert ok["isError"] is False
    assert ok["structuredContent"]["status"] == "ok"
    assert ok["content"] == [{"type": "text", "text": ok["structuredContent"]["text"]}]
    assert ok["structuredContent"]["evidence"][0]["eid"] == "E1"

    failed = call({"urls": ["https://example.com/other"]})
    assert failed["isError"] is True
    assert failed["content"][0]["text"].endswith("read failed: the link is not in the news database.")


def test_stdio_resources_list_and_read(monkeypatch):
    server = _newsdb_server(monkeypatch)
    listed = _handle_request(server, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert listed["result"]["resources"][0]["uri"] == "news://corpus"
    read_result = _handle_request(
        server,
        {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "news://corpus"}},
    )
    assert read_result["result"]["contents"][0]["text"] == "Range: 3 articles"


def test_initialize_advertises_resources(monkeypatch):
    resp = _handle_request(_newsdb_server(monkeypatch), {"jsonrpc": "2.0", "id": 3, "method": "initialize"})
    assert "resources" in resp["result"]["capabilities"]
