from __future__ import annotations

import threading
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, ToolMessage

from agent import EvidenceFound, Final, Progress, run_turn, trace_store
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
        body_level="full", story_id=story_id,
    )
    stats = StoryStats(story_id=story_id, report_count=1, publisher_count=1, publisher_kinds=("media",),
                       first_at=PUBLISHED, last_at=PUBLISHED)
    return StoryHit(lead=article, stats=stats, related=(), match_score=0.9)


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
    persisted: list = []
    monkeypatch.setattr(nodes, "corpus_info", lambda: "")
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    monkeypatch.setattr(trace_store, "persist", persisted.append)
    done = {name: threading.Event() for name in WAITS_FOR}
    finished: list[str] = []

    def search(args):
        waits_for = WAITS_FOR[args.query]
        if waits_for and not done[waits_for].wait(timeout=5):
            raise TimeoutError(args.query)
        finished.append(args.query)
        done[args.query].set()
        return SearchResult(hits=[_hit(list(WAITS_FOR).index(args.query) + 1, args.query)], weak_match=False)

    monkeypatch.setattr(registry, "run_search_news", search)
    # The rejected call sits between executed ones, so the ToolMessages must merge both by call index.
    arguments = [{"query": "Alpha"}, {"query": "Delta", "days": 0}, {"query": "Beta"}, {"query": "Gamma"}]
    calls = [{"name": "search_news", "args": args, "id": f"c{i}"} for i, args in enumerate(arguments, 1)]
    gather = FakeChat([AIMessage(content="", tool_calls=calls), AIMessage(content="DONE")])
    final = FakeChat([AIMessage(content="STATUS: answered\n三家都有发布 [E1][E2][E3]。")])
    events: list = []

    answer = run_turn("Alpha、Beta、Gamma 发布了什么？", channel="web", request_id="r1", on_event=events.append,
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

    (trace,) = persisted
    first_step, _ = [span for span in trace.spans if span.kind == "step"]
    tool_spans = sorted((span for span in trace.spans if span.kind == "tool"), key=lambda span: span.attributes["index"])
    assert {span.parent_id for span in tool_spans} == {first_step.span_id}
    assert [span.attributes["call_id"] for span in tool_spans] == ["c1", "c3", "c4"]
    assert [span.output["text"] for span in tool_spans] == [tool_messages[i].content for i in (0, 2, 3)]
    assert [(call["disposition"], call.get("reason")) for call in first_step.output["calls"]] == [
        ("executed", None), ("rejected", "invalid_args"), ("executed", None), ("executed", None)]
    assert first_step.finished_at_ms >= max(span.finished_at_ms for span in tool_spans)
