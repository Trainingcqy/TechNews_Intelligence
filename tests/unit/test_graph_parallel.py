from __future__ import annotations

import threading
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, ToolMessage

from agent import EvidenceFound, Final, Progress, run_turn
from agent.config import AgentConfig
from agent.graph import nodes
from agent.llm import ModelHandle
from agent.retrieval.search import SearchResult
from agent.tools import registry
from agent.types import Article, StoryHit, StoryStats

PUBLISHED = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
# Each branch waits until the next one has finished, so the branches finish in reverse call order.
WAITS_FOR = {"Alpha": "Beta", "Beta": "Gamma", "Gamma": None}


def _hit(story_id: int, name: str) -> StoryHit:
    article = Article(
        article_id=story_id, url=f"https://example.com/{name}", title=f"{name} launches", title_zh="", summary="",
        topics=(), kind=None, publisher="TechCrunch", publisher_kind="media", lang="en", published_at=PUBLISHED,
        body_level="full", hn_score=None, hn_comments=None, hn_sighting_id=None, story_id=story_id,
    )
    stats = StoryStats(story_id=story_id, report_count=1, publisher_count=1, publisher_kinds=("media",),
                       hn_score=None, hn_comments=None, hn_sighting_id=None, first_at=PUBLISHED, last_at=PUBLISHED)
    return StoryHit(lead=article, stats=stats, related=(), discussion=None, match_score=0.9)


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        return self.replies.pop(0)


def test_parallel_calls_keep_call_order_and_trace(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setattr(nodes, "corpus_info", lambda: "")
    monkeypatch.setattr("agent.trace._persist_request_trace", lambda summary: persisted.append(summary) or True)
    done = {name: threading.Event() for name in WAITS_FOR}
    finished: list[str] = []

    def search(args):
        waits_for = WAITS_FOR[args.query]
        if waits_for and not done[waits_for].wait(timeout=5):
            raise TimeoutError(args.query)
        finished.append(args.query)
        done[args.query].set()
        return SearchResult(hits=[_hit(list(WAITS_FOR).index(args.query) + 1, args.query)], weak_match=False, meta={})

    monkeypatch.setattr(registry, "run_search_news", search)
    # The rejected call sits between executed ones, so the ToolMessages must merge both by call index.
    arguments = [{"query": "Alpha"}, {"query": "Delta", "days": 0}, {"query": "Beta"}, {"query": "Gamma"}]
    calls = [{"name": "search_news", "args": args, "id": f"c{i}"} for i, args in enumerate(arguments, 1)]
    gather = FakeChat([AIMessage(content="", tool_calls=calls), AIMessage(content="DONE")])
    final = FakeChat([AIMessage(content="STATUS: answered\n三家都有发布 [E1][E2][E3]。")])
    events: list = []

    answer = run_turn("Alpha、Beta、Gamma 发布了什么？", request_id="r1", on_event=events.append,
                      config=AgentConfig(),
                      models=(ModelHandle("gather", "fake", "g", gather), ModelHandle("final", "fake", "f", final)))

    assert finished == ["Gamma", "Beta", "Alpha"]
    tool_messages = [m for m in gather.calls[1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2", "c3", "c4"]
    assert tool_messages[1].content == "参数错误：days：Input should be greater than or equal to 1"
    assert [tool_messages[i].content.splitlines()[1] for i in (0, 2, 3)] == [
        "[E1] Alpha launches", "[E2] Beta launches", "[E3] Gamma launches",
    ]
    assert events == [
        Progress("understanding", "正在理解问题"),
        *(Progress("retrieving", "正在检索相关新闻", f"{name} 最近 14 天") for name in WAITS_FOR),
        *(EvidenceFound(f"E{i}", f"{name} launches", "TechCrunch", f"https://example.com/{name}", "2026-11-12")
          for i, name in enumerate(WAITS_FOR, 1)),
        Progress("synthesizing", "正在生成回答"),
        Final(answer),
    ]
    assert answer.status == "answered"
    assert answer.tool_calls == ("search_news", "search_news", "search_news")

    spans = persisted[0]["spans"]
    run_tool_ids = {s["span_id"] for s in spans if s["span_type"] == "graph_node" and s["name"] == "run_tool"}
    tool_spans = [s for s in spans if s["span_type"] == "tool_call"]
    assert len(run_tool_ids) == 3 and len(tool_spans) == 3
    assert {s["request_id"] for s in tool_spans} == {"r1"}
    assert {s["parent_span_id"] for s in tool_spans} == run_tool_ids
