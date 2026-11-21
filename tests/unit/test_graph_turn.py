from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent import Answer, EvidenceFound, Final, Progress, run_turn
from agent import prompts
from agent.citations import BLOCKED_TEXT, ERROR_TEXT, NOT_FOUND_TEXT
from agent.config import AgentConfig
from agent.graph import nodes
from agent.llm import ModelHandle
from agent.retrieval.search import SearchResult
from agent.tools import read, registry
from agent.types import Article, Source, StoryHit, StoryStats

QUESTION = "Genie 4 有什么新功能？"
CORPUS = "新闻库范围：测试。"
BODY = "谷歌发布 Genie 4 世界模型，新增音频。"
PUBLISHED = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
ARTICLE = Article(
    article_id=30, url="https://techcrunch.com/genie-4", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=PUBLISHED, body_level="full", hn_score=None, hn_comments=None, hn_sighting_id=None,
    story_id=5,
)
STATS = StoryStats(story_id=5, report_count=1, publisher_count=1, publisher_kinds=("media",), hn_score=None,
                   hn_comments=None, hn_sighting_id=None, first_at=PUBLISHED, last_at=PUBLISHED)
SOURCE = Source(n=1, url=ARTICLE.url, title="Genie 4 adds audio", title_zh="", source="TechCrunch",
                published_at="2026-11-12")

DONE = AIMessage(content="DONE")
ANSWERED = AIMessage(content="STATUS: answered\nGenie 4 新增了音频 [E1]。")
UNDERSTANDING = Progress("understanding", "正在理解问题")
RETRIEVING = Progress("retrieving", "正在检索相关新闻", "Genie 4 最近 14 天")
FOUND = EvidenceFound("E1", "Genie 4 adds audio", "TechCrunch", ARTICLE.url, "2026-11-12")
SYNTHESIZING = Progress("synthesizing", "正在生成回答")


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeConn:
    def cursor(self):
        return self

    def close(self):
        pass


def _calls(*calls) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}
                                             for call_id, name, args in calls])


def _search(*queries) -> AIMessage:
    return _calls(*((f"c{i}", "search_news", {"query": query}) for i, query in enumerate(queries, 1)))


def _answered(**fields) -> Answer:
    return Answer(status="answered", text="Genie 4 新增了音频 [1]。", sources=(SOURCE,), story_ids=(5,),
                  request_id="r1", **fields)


@pytest.fixture
def turn(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setattr(nodes, "corpus_info", lambda: CORPUS)
    monkeypatch.setattr("agent.trace._persist_request_trace", lambda summary: persisted.append(summary) or True)

    def search(args):
        hits = [StoryHit(lead=ARTICLE, stats=STATS, related=(), discussion=None, match_score=0.9)]
        return SearchResult(hits=hits if args.query == "Genie 4" else [], weak_match=False, meta={})

    monkeypatch.setattr(registry, "run_search_news", search)
    monkeypatch.setattr(read, "get_conn", FakeConn)
    monkeypatch.setattr(read, "put_conn", lambda conn: None)
    monkeypatch.setattr(read, "fetch_articles_by_url_keys", lambda cur, keys: [])
    monkeypatch.setattr(read, "fetch_bodies", lambda cur, ids: {30: BODY} if 30 in ids else {})
    monkeypatch.setattr(read, "fetch_discussion_texts", lambda cur, ids: {})

    def run(gather_replies, final_replies=(), *, config=None, history=None):
        gather, final = FakeChat(gather_replies), FakeChat(final_replies)
        events: list = []
        answer = run_turn(
            QUESTION, history, request_id="r1", on_event=events.append, config=config or AgentConfig(),
            models=(ModelHandle("gather", "fake", "g", gather), ModelHandle("final", "fake", "f", final)),
        )
        return SimpleNamespace(answer=answer, events=events, gather=gather, final=final, trace=persisted[-1])

    return run


def test_direct_reply(turn):
    r = turn([AIMessage(content="你好！有什么想了解的科技新闻？😀")])

    expected = Answer(status="direct", text="你好！有什么想了解的科技新闻？", request_id="r1")
    assert r.answer == expected
    assert r.events == [UNDERSTANDING, Final(expected)]
    system = prompts.GATHER_SYSTEM_PROMPT.format(today=nodes._today(), corpus=CORPUS, follows="")
    assert r.gather.calls == [[SystemMessage(system), HumanMessage(QUESTION)]]
    assert r.final.calls == []


def test_tool_call_without_evidence_is_not_found(turn):
    r = turn([_search("不存在的产品"), DONE])

    expected = Answer(status="not_found", text=NOT_FOUND_TEXT["zh"], tool_calls=("search_news",), request_id="r1")
    assert r.answer == expected
    assert r.events == [UNDERSTANDING, Progress("retrieving", "正在检索相关新闻", "不存在的产品 最近 14 天"),
                        Final(expected)]
    assert r.final.calls == []


def test_normal_answer(turn):
    history = [{"role": "user", "parts": [{"text": "上一个问题"}]},
               {"role": "model", "parts": [{"text": "上一个回答"}]}]
    r = turn([_search("Genie 4"), _calls(("c2", "read_articles", {"ids": ["E1"]})), DONE], [ANSWERED],
             history=history)

    expected = _answered(tool_calls=("search_news", "read_articles"))
    assert r.answer == expected
    # Reading E1 does not report it again: it became citable, and was reported, when the search found it.
    assert r.events == [UNDERSTANDING, RETRIEVING, FOUND, Progress("reading", "正在读取正文"),
                        SYNTHESIZING, Final(expected)]
    assert [(type(m).__name__, getattr(m, "tool_call_id", None)) for m in r.gather.calls[2]] == [
        ("SystemMessage", None), ("HumanMessage", None), ("AIMessage", None), ("HumanMessage", None),
        ("AIMessage", None), ("ToolMessage", "c1"), ("AIMessage", None), ("ToolMessage", "c2"),
    ]

    (synthesis,) = r.final.calls
    system, *context, last = synthesis
    assert system == SystemMessage(prompts.FINAL_SYSTEM_PROMPT)
    assert context == [HumanMessage("上一个问题"), AIMessage("上一个回答")]
    assert "\n\nEvidence brief:\n[E1] 标题：Genie 4 adds audio\n" in last.content
    assert BODY in last.content
    assert last.content.endswith("Write the final answer now.")
    model_spans = [s["name"] for s in r.trace["spans"] if s["span_type"] == "model_call"]
    assert model_spans == ["gather", "gather", "gather", "final_synthesizer"]
    assert r.trace["tool_call_chain"] == ["search_news", "read_articles"]


@pytest.mark.parametrize("raw, expected", [
    ("STATUS: answered\nGenie 4 新增了音频 [E9]。",
     Answer(status="blocked", text=BLOCKED_TEXT["zh"], tool_calls=("search_news",), request_id="r1",
            error_code="no_valid_citation")),
    ("STATUS: insufficient\n材料没有提到价格 [E1]。",
     Answer(status="insufficient", text="材料没有提到价格 [1]。", sources=(SOURCE,), tool_calls=("search_news",),
            request_id="r1")),
], ids=["blocked", "insufficient"])
def test_synthesis_outcome(turn, raw, expected):
    r = turn([_search("Genie 4"), DONE], [AIMessage(content=raw)])

    assert r.answer == expected
    assert r.events == [UNDERSTANDING, RETRIEVING, FOUND, SYNTHESIZING, Final(expected)]
    assert r.trace["final_status"] == expected.status


def test_step_limit_goes_to_synthesis(turn):
    r = turn([_search("Genie 4")], [ANSWERED], config=AgentConfig(max_steps=1))

    expected = _answered(tool_calls=("search_news",))
    assert r.answer == expected
    assert len(r.gather.calls) == 1
    assert r.events == [UNDERSTANDING, RETRIEVING, FOUND, SYNTHESIZING, Final(expected)]


def test_calls_over_step_limit_are_rejected(turn):
    r = turn([_search("Genie 4", "Gemini", "Veo"), DONE], [ANSWERED], config=AgentConfig(max_calls_per_step=2))

    expected = _answered(tool_calls=("search_news", "search_news"))
    assert r.answer == expected
    tool_messages = [m for m in r.gather.calls[1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2", "c3"]
    assert tool_messages[2].content == "已超过单步调用上限，本次调用未执行。"
    assert r.events == [UNDERSTANDING, RETRIEVING, Progress("retrieving", "正在检索相关新闻", "Gemini 最近 14 天"),
                        FOUND, SYNTHESIZING, Final(expected)]


def test_argument_error_then_model_corrects(turn):
    r = turn([_calls(("c1", "search_news", {"query": "Genie 4", "days": 0})),
              _calls(("c2", "search_news", {"query": "Genie 4"})), DONE], [ANSWERED])

    expected = _answered(tool_calls=("search_news",))
    assert r.answer == expected
    last = r.gather.calls[1][-1]
    assert (type(last), last.tool_call_id, last.content) == (
        ToolMessage, "c1", "参数错误：days：Input should be greater than or equal to 1")
    assert r.events == [UNDERSTANDING, RETRIEVING, FOUND, SYNTHESIZING, Final(expected)]


def test_model_call_failure(turn):
    r = turn([_search("Genie 4"), DONE], [TimeoutError("slow")])

    expected = Answer(status="error", text=ERROR_TEXT["zh"], tool_calls=("search_news",), request_id="r1",
                      error_code="model_timeouterror")
    assert r.answer == expected
    assert r.events == [UNDERSTANDING, RETRIEVING, FOUND, SYNTHESIZING, Final(expected)]
    assert (r.trace["final_status"], r.trace["error_code"]) == ("error", "model_timeouterror")
