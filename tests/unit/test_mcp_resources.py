"""MCP server: tools, the corpus resource, call results and stdio dispatch."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent.mcp import server as server_mod
from agent.mcp.server import InProcessMCPServer, build_newsdb_server
from agent.mcp.stdio_server import _handle_request
from agent.retrieval.search import SearchResult
from agent.tools import read, registry
from agent.types import Article, Discussion, StoryHit, StoryStats
from common.urls import url_key

ARTICLE = Article(
    article_id=30, url="https://www.techcrunch.com/genie-4/", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc), body_level="full",
    hn_score=None, hn_comments=None, hn_sighting_id=None, story_id=5,
)
DISCUSSION = Discussion(sighting_id=70, article_id=30, url="https://news.ycombinator.com/item?id=1",
                        title="Genie 4 adds audio", score=955, comment_total=312)
DISCUSSION_URL = "http://news.ycombinator.com/item?id=1"


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
                        lambda cur, keys: [ARTICLE] if url_key(ARTICLE.url) in keys else [])
    monkeypatch.setattr(read, "fetch_discussions_by_url_keys",
                        lambda cur, keys: [DISCUSSION] if url_key(DISCUSSION.url) in keys else [])
    monkeypatch.setattr(read, "fetch_bodies", lambda cur, ids: {30: "文章正文"})
    monkeypatch.setattr(read, "fetch_discussion_texts", lambda cur, ids: {70: "评论正文"})


def _newsdb_server(monkeypatch) -> InProcessMCPServer:
    monkeypatch.setattr(server_mod, "corpus_info", lambda: "新闻库范围：共 3 篇")
    return build_newsdb_server()


def test_newsdb_server_exposes_two_tools_and_one_resource(monkeypatch):
    server = _newsdb_server(monkeypatch)
    assert [row["qualified_name"] for row in server.list_tools()] == [
        "mcp__newsdb__read_articles", "mcp__newsdb__search_news"]
    assert [row["uri"] for row in server.list_resources()] == ["news://corpus"]
    assert server.read_resource("news://corpus") == {
        "uri": "news://corpus", "mimeType": "text/plain", "text": "新闻库范围：共 3 篇"}


def test_read_unknown_resource_raises(monkeypatch):
    with pytest.raises(KeyError):
        _newsdb_server(monkeypatch).read_resource("news://stats")


def test_call_tool_reads_library_url_without_user_provided_check(monkeypatch, fake_db):
    result = _newsdb_server(monkeypatch).call_tool("read_articles", {"urls": ["https://techcrunch.com/genie-4"]})
    assert result == {
        "status": "ok",
        "text": "read_articles 结果：\n[E1] 《Genie 4 adds audio》TechCrunch · 2026-11-12｜全文 4 字符\n开头：文章正文",
        "evidence": [{"eid": "E1", "url": ARTICLE.url, "title": "Genie 4 adds audio",
                      "source": "TechCrunch", "published_at": "2026-11-12"}],
    }


def test_call_tool_reads_discussion_page_url(monkeypatch, fake_db):
    result = _newsdb_server(monkeypatch).call_tool("read_articles", {"urls": [DISCUSSION_URL]})
    assert result == {
        "status": "ok",
        "text": "read_articles 结果：\n[E1] HackerNews 讨论｜全文 4 字符\n开头：评论正文",
        "evidence": [{"eid": "E1", "url": DISCUSSION.url, "title": "Genie 4 adds audio",
                      "source": "HackerNews 讨论", "published_at": ""}],
    }


def test_call_tool_search_news_lists_every_registered_evidence(monkeypatch):
    related = replace(ARTICLE, article_id=31, url="https://example.com/other", title="Other report",
                      publisher="Example", published_at=datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc))
    stats = StoryStats(story_id=5, report_count=2, publisher_count=2, publisher_kinds=("media",), hn_score=955,
                       hn_comments=312, hn_sighting_id=70, first_at=related.published_at,
                       last_at=ARTICLE.published_at)
    hit = StoryHit(lead=ARTICLE, stats=stats, related=(related,), discussion=DISCUSSION, match_score=None)
    monkeypatch.setattr(registry, "run_search_news", lambda args: SearchResult(hits=[hit], weak_match=False, meta={}))

    result = _newsdb_server(monkeypatch).call_tool("search_news", {"topic": "ai"})

    assert result["status"] == "ok"
    assert result["text"].startswith("search_news 结果，topic=ai，最近 14 天，按重要性：1 个故事\n[E1] Genie 4 adds audio")
    assert [(row["eid"], row["source"], row["published_at"]) for row in result["evidence"]] == [
        ("E1", "TechCrunch", "2026-11-12"), ("E2", "Example", "2026-11-10"), ("E3", "HackerNews 讨论", "")]


def test_call_tool_errors_have_status_error(monkeypatch, fake_db):
    server = _newsdb_server(monkeypatch)
    assert server.call_tool("read_articles", {"urls": ["https://example.com/other"]}) == {
        "status": "error",
        "text": "read_articles 结果：\n[https://example.com/other] 读取失败：该链接不在新闻库内。",
        "evidence": [],
    }
    assert server.call_tool("read_articles", {})["status"] == "error"
    unknown = server.call_tool("compare_sources", {})
    assert (unknown["status"], unknown["text"], unknown["evidence"]) == (
        "error", "未知工具：compare_sources。可用工具：search_news、read_articles。", [])


def test_each_call_starts_a_new_registry(monkeypatch, fake_db):
    server = _newsdb_server(monkeypatch)
    first = server.call_tool("read_articles", {"urls": [ARTICLE.url]})
    second = server.call_tool("read_articles", {"urls": [DISCUSSION_URL]})
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
    assert failed["content"][0]["text"].endswith("读取失败：该链接不在新闻库内。")


def test_stdio_resources_list_and_read(monkeypatch):
    server = _newsdb_server(monkeypatch)
    listed = _handle_request(server, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert listed["result"]["resources"][0]["uri"] == "news://corpus"
    read_result = _handle_request(
        server,
        {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "news://corpus"}},
    )
    assert read_result["result"]["contents"][0]["text"] == "新闻库范围：共 3 篇"


def test_initialize_advertises_resources(monkeypatch):
    resp = _handle_request(_newsdb_server(monkeypatch), {"jsonrpc": "2.0", "id": 3, "method": "initialize"})
    assert "resources" in resp["result"]["capabilities"]
