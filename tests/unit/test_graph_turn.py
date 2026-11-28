from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent import Answer, Progress, run_turn
from agent import context, memory, prompts, trace_store
from agent.citations import BLOCKED_TEXT, ERROR_TEXT, NOT_FOUND_TEXT
from agent.config import AgentConfig
from agent.graph import nodes
from agent.llm import ModelHandle
from agent.memory import Follow
from agent.retrieval.search import SearchResult
from agent.tools import follows, read, registry
from agent.types import Article, Process, ProcessItem, Source, StoryHit, StoryStats
from common import prompt_text

QUESTION = "Genie 4 有什么新功能？"
CORPUS = "新闻库范围：测试。"
BODY = "谷歌发布 Genie 4 世界模型，新增音频。"
PUBLISHED = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
ARTICLE = Article(
    article_id=30, url="https://techcrunch.com/genie-4", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=PUBLISHED, first_seen_at=PUBLISHED, body_level="full", story_id=5,
)
STATS = StoryStats(story_id=5, report_count=1, publisher_count=1, publisher_kinds=("media",),
                   first_at=PUBLISHED, last_at=PUBLISHED)
SOURCE = Source(n=1, url=ARTICLE.url, title="Genie 4 adds audio", title_zh="", source="TechCrunch",
                published_at="2026-11-12")

DONE = AIMessage(content="DONE")
ANSWERED = AIMessage(content="STATUS: answered\nGenie 4 新增了音频 [E1]。")
UNDERSTANDING = Progress("正在理解问题")
SEARCHING = Progress("正在检索", "Genie 4")
FOUND = Progress("正在分析结果", "找到 1 条新闻")
WRITING = Progress("正在撰写回答", "依据 1 篇报道")
SEARCHED = ProcessItem("检索", "Genie 4", "最近 14 天内找到 1 条新闻")
READ = ProcessItem("阅读", "Genie 4 adds audio", "TechCrunch 2026-11-12")
SEARCH_ONLY = Process("检索 1 次", (SEARCHED,))
SEARCH_AND_READ = Process("检索 1 次，阅读 1 篇报道", (SEARCHED, READ))


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
    return Answer(status="answered", text="Genie 4 新增了音频 [1]。", sources=(SOURCE,),
                  request_id="r1", **fields)


@pytest.fixture
def turn(monkeypatch):
    persisted: list = []
    monkeypatch.setattr(nodes, "corpus_info", lambda: CORPUS)
    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    monkeypatch.setattr(trace_store, "persist", persisted.append)

    def search(args):
        hits = [StoryHit(lead=ARTICLE, stats=STATS, related=(), match_score=0.9)]
        return SearchResult(hits=hits if args.query == "Genie 4" else [], weak_match=False)

    monkeypatch.setattr(registry, "run_search_news", search)
    monkeypatch.setattr(read, "get_conn", FakeConn)
    monkeypatch.setattr(read, "put_conn", lambda conn: None)
    monkeypatch.setattr(read, "fetch_articles_by_url_keys", lambda cur, keys: [])
    monkeypatch.setattr(read, "fetch_bodies", lambda cur, ids: {30: BODY} if 30 in ids else {})

    def run(gather_replies, final_replies=(), *, config=None, history=None, user_key=None, zone=None):
        gather, final = FakeChat(gather_replies), FakeChat(final_replies)
        events: list = []
        answer = run_turn(
            QUESTION, history, channel="web", request_id="r1", user_key=user_key, zone=zone, on_event=events.append,
            config=config or AgentConfig(),
            models=(ModelHandle("gather", "fake", "g", gather), ModelHandle("final", "fake", "f", final)),
        )
        return SimpleNamespace(answer=answer, events=events, gather=gather, final=final, trace=persisted[-1])

    run.persisted = persisted
    return run


def _spans(trace, kind):
    return [span for span in trace.spans if span.kind == kind]


def _turn_now(trace) -> datetime:
    context = _spans(trace, "context")[0].output
    return datetime.fromisoformat(context["now"]).astimezone(ZoneInfo(context["timezone"]))


def test_direct_reply(turn):
    r = turn([AIMessage(content="你好！有什么想了解的科技新闻？😀")])

    expected = Answer(status="direct", text="你好！有什么想了解的科技新闻？", request_id="r1")
    assert r.answer == expected
    assert r.events == [UNDERSTANDING]
    system = prompt_text.render("gather.system", clock=prompts.gather_clock(_turn_now(r.trace)), corpus=CORPUS,
                                follows="")
    assert r.gather.calls == [[SystemMessage(system), HumanMessage(QUESTION)]]
    assert r.final.calls == []


def test_clock_lines_name_the_zone_and_its_offset():
    now = datetime(2026, 11, 25, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    moment = "2026-11-25 Wednesday 09:15"
    assert prompts.gather_clock(now) == prompt_text.render("gather.clock", now=moment, zone="Asia/Shanghai, UTC+08:00")
    assert prompts.final_clock(now) == prompt_text.render("final.clock", now=moment, zone="Asia/Shanghai, UTC+08:00")
    # The CLI passes the machine's zone as a fixed offset, which has no name.
    fixed = now.astimezone(timezone(timedelta(hours=8)))
    assert prompts.gather_clock(fixed) == prompt_text.render("gather.clock", now=moment, zone="UTC+08:00")


def test_turn_runs_in_the_user_zone(turn, monkeypatch):
    # 16:30 UTC on 11-11 is 11-12 in Beijing and 11-11 in New York; 17:00 UTC on 11-09 is 11-10 and 11-09.
    related = replace(ARTICLE, article_id=31, url="https://www.theverge.com/genie-4", title="Genie 4 hands-on",
                      publisher="The Verge")
    hit = StoryHit(lead=ARTICLE, stats=STATS, related=(related,), match_score=0.9)
    monkeypatch.setattr(registry, "run_search_news", lambda args: SearchResult(hits=[hit], weak_match=False))
    monkeypatch.setattr(nodes.memory, "list_follows", lambda user_key, limit: FOLLOWS)

    r = turn([_search("Genie 4"), _calls(("c2", "read_articles", {"ids": ["E1"]})), DONE], [ANSWERED],
             user_key="tg:1", zone=ZoneInfo("America/New_York"))

    assert _spans(r.trace, "context")[0].output["timezone"] == "America/New_York"
    now = _turn_now(r.trace)
    system = r.gather.calls[0][0].content
    assert prompts.gather_clock(now) in system and "- Genie 4: last checked 2026-11-09, " in system
    search_text, read_text = (m.content for m in r.gather.calls[2] if isinstance(m, ToolMessage))
    assert "TechCrunch · media · 2026-11-11" in search_text
    assert '[E2] The Verge · media · 11-11 "Genie 4 hands-on"' in search_text
    assert '[E1] "Genie 4 adds audio" TechCrunch · 2026-11-11 | ' in read_text
    last = r.final.calls[0][-1].content
    assert last.startswith(prompts.final_clock(now) + "\n\nUser question:\n")
    assert "Date: 2026-11-11" in last and "first 2026-11-11, latest 2026-11-11" in last
    assert r.answer.process.items[1] == replace(READ, meta="TechCrunch 2026-11-11")
    assert r.answer.sources[0].published_at == "2026-11-11"


def test_browse_turn(turn, monkeypatch):
    hit = StoryHit(lead=ARTICLE, stats=STATS, related=(), match_score=None)
    monkeypatch.setattr(registry, "run_browse_news", lambda args: SearchResult(hits=[hit], weak_match=False))

    r = turn([_calls(("c1", "browse_news", {"topic": "ai", "days": 7})), DONE], [ANSWERED])

    browsed = ProcessItem("检索", "最近 7 天的人工智能要闻", "找到 1 条新闻")
    assert r.answer == _answered(tool_calls=("browse_news",), process=Process("检索 1 次", (browsed,)))
    assert r.events == [UNDERSTANDING, Progress("正在检索", "最近 7 天的人工智能要闻"), FOUND, WRITING]
    result = next(m for m in r.gather.calls[1] if isinstance(m, ToolMessage))
    assert result.content.startswith("browse_news results, topic=ai, last 7 days, by importance: 1 story\n")
    (tool,) = _spans(r.trace, "tool")
    assert (tool.name, tool.output["stories"][0]["lead"]) == ("browse_news", 30)


def test_tool_call_without_evidence_is_not_found(turn):
    r = turn([_search("不存在的产品"), DONE])

    expected = Answer(status="not_found", text=NOT_FOUND_TEXT["zh"], tool_calls=("search_news",),
                      process=Process("检索 1 次", (ProcessItem("检索", "不存在的产品", "最近 14 天内没有找到"),)),
                      request_id="r1")
    assert r.answer == expected
    assert r.events == [UNDERSTANDING, Progress("正在检索", "不存在的产品"),
                        Progress("正在分析结果", "没有找到新闻")]
    assert r.final.calls == []


def test_normal_answer(turn):
    history = [{"role": "user", "parts": [{"text": "上一个问题"}]},
               {"role": "model", "parts": [{"text": "上一个回答"}]}]
    r = turn([_search("Genie 4"), _calls(("c2", "read_articles", {"ids": ["E1"]})), DONE], [ANSWERED],
             history=history)

    expected = _answered(tool_calls=("search_news", "read_articles"), process=SEARCH_AND_READ)
    assert r.answer == expected
    assert r.events == [UNDERSTANDING, SEARCHING, FOUND, Progress("正在阅读 1 篇报道", "Genie 4 adds audio"),
                        Progress("正在分析结果", "读到 1 篇全文"), WRITING]
    assert [(type(m).__name__, getattr(m, "tool_call_id", None)) for m in r.gather.calls[2]] == [
        ("SystemMessage", None), ("HumanMessage", None), ("AIMessage", None), ("HumanMessage", None),
        ("AIMessage", None), ("ToolMessage", "c1"), ("AIMessage", None), ("ToolMessage", "c2"),
    ]

    (synthesis,) = r.final.calls
    system, *context, last = synthesis
    assert system == SystemMessage(prompts.FINAL_SYSTEM_PROMPT)
    assert context == [HumanMessage("上一个问题"), AIMessage("上一个回答")]
    assert "\n\nEvidence brief:\n[E1] Title: Genie 4 adds audio\n" in last.content
    assert BODY in last.content
    assert last.content.endswith("Write the final answer now.")

    trace = r.trace
    assert all(span.status == "ok" and span.finished_at_ms for span in trace.spans)
    steps = _spans(trace, "step")
    assert [step.attributes["step"] for step in steps] == [1, 2, 3]
    assert steps[2].output == {"calls": [], "reply": "DONE"}
    assert [(span.name, span.parent_id) for span in _spans(trace, "llm")] == [
        ("gather", steps[0].span_id), ("gather", steps[1].span_id), ("gather", steps[2].span_id), ("final", None)]
    search, read_call = _spans(trace, "tool")
    assert (search.name, search.parent_id, read_call.name, read_call.parent_id) == (
        "search_news", steps[0].span_id, "read_articles", steps[1].span_id)
    assert search.output["stories"][0]["lead"] == 30
    assert search.output["text"] == next(m for m in r.gather.calls[1] if isinstance(m, ToolMessage)).content
    assert _spans(trace, "context")[0].output["turns_used"] == 1
    assert trace.output["route"] == {"decision": "synthesize", "tools_called": True, "citable": 1}
    assert trace.output["citations"]["cited"] == {"E1": 1}
    assert trace.output["evidence"] == [{
        "eid": "E1", "level": "fulltext", "article_id": 30, "story_id": 5,
        "registered": {"step": 1, "call": 0}, "upgraded": [{"step": 2, "call": 0, "level": "fulltext"}],
        "body_in_block": True, "cited": 1,
    }]
    assert (trace.status, trace.channel, trace.all_warnings()) == ("answered", "web", [])


@pytest.mark.parametrize("raw, expected", [
    ("STATUS: answered\nGenie 4 新增了音频 [E9]。",
     Answer(status="blocked", text=BLOCKED_TEXT["zh"], tool_calls=("search_news",), process=SEARCH_ONLY,
            request_id="r1", error_code="no_valid_citation")),
    ("STATUS: insufficient\n材料没有提到价格 [E1]。",
     Answer(status="insufficient", text="材料没有提到价格 [1]。", sources=(SOURCE,), tool_calls=("search_news",),
            process=SEARCH_ONLY, request_id="r1")),
], ids=["blocked", "insufficient"])
def test_synthesis_outcome(turn, raw, expected):
    r = turn([_search("Genie 4"), DONE], [AIMessage(content=raw)])

    assert r.answer == expected
    assert r.events == [UNDERSTANDING, SEARCHING, FOUND, WRITING]
    assert r.trace.status == expected.status
    # The model cited E9, which was never registered.
    assert ("citations_removed" in r.trace.warnings) == (expected.status == "blocked")


def test_body_dropped_from_the_evidence_block_is_recorded(turn):
    r = turn([_search("Genie 4"), _calls(("c2", "read_articles", {"ids": ["E1"]})), DONE], [ANSWERED],
             config=AgentConfig(synthesis_evidence_chars=10))

    assert BODY not in r.final.calls[0][-1].content
    assert r.trace.warnings == ["evidence_truncated"]
    assert r.trace.output["evidence"][0]["body_in_block"] is False
    # The entry stays in the brief without its body, so it still counts.
    assert r.events[-1] == WRITING


def test_history_evidence_left_out_of_the_brief_is_recorded(turn, monkeypatch):
    earlier = replace(ARTICLE, article_id=31, url="https://www.theverge.com/genie-4", title="Genie 4 hands-on")
    monkeypatch.setattr(context, "get_conn", FakeConn)
    monkeypatch.setattr(context, "put_conn", lambda conn: None)
    monkeypatch.setattr(context, "fetch_articles_by_url_keys", lambda cur, keys: [earlier])
    history = [{"role": "user", "parts": [{"text": "上一个问题"}]},
               {"role": "model", "parts": [{"text": "上一个回答 [1]"}], "citation_urls": [earlier.url]}]

    r = turn([_search("Genie 4"), DONE], [ANSWERED], history=history, config=AgentConfig(synthesis_evidence_chars=10))

    assert "Genie 4 hands-on" not in r.final.calls[0][-1].content
    assert r.trace.warnings == ["evidence_truncated"]
    assert [(row["eid"], row.get("in_block")) for row in r.trace.output["evidence"]] == [("E1", False), ("E2", None)]
    assert r.events[-1] == WRITING


def test_step_limit_goes_to_synthesis(turn):
    r = turn([_search("Genie 4")], [ANSWERED], config=AgentConfig(max_steps=1))

    expected = _answered(tool_calls=("search_news",), process=SEARCH_ONLY)
    assert r.answer == expected
    assert len(r.gather.calls) == 1
    assert r.events == [UNDERSTANDING, SEARCHING, WRITING]
    assert len(_spans(r.trace, "step")) == 1
    assert r.trace.warnings == ["step_budget_exhausted"]


def test_calls_over_step_limit_are_rejected(turn):
    r = turn([_search("Genie 4", "Gemini", "Veo"), DONE], [ANSWERED], config=AgentConfig(max_calls_per_step=2))

    expected = _answered(tool_calls=("search_news", "search_news"),
                         process=Process("检索 2 次", (SEARCHED, ProcessItem("检索", "Gemini", "最近 14 天内没有找到"))))
    assert r.answer == expected
    tool_messages = [m for m in r.gather.calls[1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2", "c3"]
    assert tool_messages[2].content.startswith("This step exceeded its call limit; this call was not run.\n\n")
    assert r.events == [UNDERSTANDING, Progress("正在检索", "Genie 4、Gemini"), FOUND, WRITING]
    step = _spans(r.trace, "step")[0]
    assert [(call["disposition"], call.get("reason")) for call in step.output["calls"]] == [
        ("executed", None), ("executed", None), ("rejected", "call_limit")]
    assert step.warnings == ["calls_rejected"]


def test_argument_error_then_model_corrects(turn):
    r = turn([_calls(("c1", "search_news", {"query": "Genie 4", "days": 0})),
              _calls(("c2", "search_news", {"query": "Genie 4"})), DONE], [ANSWERED])

    expected = _answered(tool_calls=("search_news",), process=SEARCH_ONLY)
    assert r.answer == expected
    last = r.gather.calls[1][-1]
    assert (type(last), last.tool_call_id) == (ToolMessage, "c1")
    assert last.content.startswith("Invalid arguments: days: Input should be greater than or equal to 1\n\n")
    # The rejected call of step 1 shows nothing and finds nothing.
    assert r.events == [UNDERSTANDING, Progress("正在分析结果"), SEARCHING, FOUND, WRITING]


def test_each_step_result_ends_with_the_remaining_budget(turn):
    r = turn([_search("Genie 4"), _calls(("c2", "read_articles", {"ids": ["E1"]})), DONE], [ANSWERED],
             config=AgentConfig(max_steps=3))

    def note(step, steps_left, reads_left):
        return prompt_text.render("gather.budget", step=step, max_steps="3", steps_left=steps_left, max_calls="5",
                                  reads_left=reads_left)

    after_first, after_second = (r.gather.calls[i][-1].content for i in (1, 2))
    assert after_first.endswith("\n\n" + note("1", "2 steps", "10 reads"))
    assert after_second.endswith("\n\n" + note("2", "1 step", "9 reads") + " " + prompt_text.text("gather.last_step"))
    read_span = _spans(r.trace, "tool")[1]
    assert read_span.output["text"] == after_second


def test_failed_reads_are_refunded_in_the_budget_after_the_step(turn):
    r = turn([_search("Genie 4"),
              _calls(("c2", "read_articles", {"ids": ["E1", "E9"]}), ("c3", "read_articles", {"ids": ["E2"]})), DONE],
             [ANSWERED], config=AgentConfig(max_steps=3, max_read_articles=2))

    after_second = r.gather.calls[2][-1].content
    # c3 is prepared while c2 still holds both reads, so it is rejected; the note counts the refund of E9.
    assert after_second.startswith("The read limit of this request has been reached.\n\n")
    assert after_second.endswith(prompt_text.render(
        "gather.budget", step="2", max_steps="3", steps_left="1 step", max_calls="5", reads_left="1 read")
        + " " + prompt_text.text("gather.last_step"))


def test_model_call_failure(turn):
    r = turn([_search("Genie 4"), DONE], [TimeoutError("slow")])

    expected = Answer(status="error", text=ERROR_TEXT["zh"], tool_calls=("search_news",), process=SEARCH_ONLY,
                      request_id="r1", error_code="model_timeouterror")
    assert r.answer == expected
    assert r.events == [UNDERSTANDING, SEARCHING, FOUND, WRITING]
    assert (r.trace.status, r.trace.error_code, r.trace.error_message) == ("error", "model_timeouterror", "slow")
    final = _spans(r.trace, "llm")[-1]
    assert (final.name, final.status, final.error_code) == ("final", "error", "model_timeouterror")
    assert r.trace.output["route"]["decision"] == "synthesize" and r.trace.output["citations"] is None


class StoppingChat(FakeChat):
    """Sets the turn's stop event on its call number `at`, as a stop that arrives while that model call runs."""

    def __init__(self, replies, stop, at=1):
        super().__init__(replies)
        self.stop = stop
        self.at = at

    def invoke(self, messages):
        if len(self.calls) + 1 == self.at:
            self.stop.set()
        return super().invoke(messages)


def _run_stoppable(turn, gather, final, stop):
    answer = run_turn(QUESTION, channel="web", request_id="r1", stop=stop, config=AgentConfig(),
                      models=(ModelHandle("gather", "fake", "g", gather), ModelHandle("final", "fake", "f", final)))
    return answer, turn.persisted[-1]


def test_stop_before_the_turn_calls_no_model(turn):
    stop = threading.Event()
    stop.set()
    gather, final = FakeChat([DONE]), FakeChat([ANSWERED])

    answer, trace = _run_stoppable(turn, gather, final, stop)

    assert answer == Answer(status="stopped", text="", request_id="r1")
    assert (gather.calls, final.calls) == ([], [])
    assert trace.status == "stopped"


def test_stop_during_a_step_skips_the_next_model_call(turn):
    stop = threading.Event()
    gather, final = StoppingChat([_search("Genie 4")], stop), FakeChat([ANSWERED])

    answer, trace = _run_stoppable(turn, gather, final, stop)

    assert answer == Answer(status="stopped", text="", tool_calls=("search_news",), process=SEARCH_ONLY,
                            request_id="r1")
    assert (len(gather.calls), final.calls) == (1, [])
    assert trace.status == "stopped"


def test_stop_during_the_last_step_skips_synthesis(turn):
    stop = threading.Event()
    gather, final = StoppingChat([_search("Genie 4"), DONE], stop, at=2), FakeChat([ANSWERED])

    answer, trace = _run_stoppable(turn, gather, final, stop)

    assert (answer.status, len(gather.calls), final.calls) == ("stopped", 2, [])
    assert trace.status == "stopped"


def test_stop_during_synthesis_drops_its_answer(turn):
    stop = threading.Event()
    gather, final = FakeChat([_search("Genie 4"), DONE]), StoppingChat([ANSWERED], stop)

    answer, trace = _run_stoppable(turn, gather, final, stop)

    assert (answer.status, answer.text, answer.sources) == ("stopped", "", ())
    assert len(final.calls) == 1
    assert trace.status == "stopped"


FOLLOWS = [
    Follow(follow_id=1, query="Genie 4", last_checked_at=datetime(2026, 11, 9, 17, 0, tzinfo=timezone.utc)),
    Follow(follow_id=2, query="具身智能", last_checked_at=None),
]


def test_followed_turn_reads_memory(turn, monkeypatch):
    lookups: list[tuple] = []
    monkeypatch.setattr(nodes.memory, "list_follows",
                        lambda user_key, limit: lookups.append(("list", user_key, limit)) or FOLLOWS)

    searched: list[tuple] = []

    def search_stories(*, query, days, sort, limit, **filters):
        searched.append((query, days, sort, limit))
        if query == "embodied AI":
            raise RuntimeError("search failed")
        return SearchResult(hits=[StoryHit(lead=ARTICLE, stats=STATS, related=(), match_score=0.9)], weak_match=False)

    monkeypatch.setattr(follows, "search_stories", search_stories)
    monkeypatch.setattr(registry, "run_search_news", lambda args: SearchResult(hits=[], weak_match=False))
    gather = FakeChat([
        _calls(("c1", "check_follows", {"queries": [{"follow": "genie 4", "query": "Genie 4 world model"},
                                                    {"follow": "具身智能", "query": "embodied AI"}]}),
               ("c2", "search_news", {"query": "具身智能"})),
        _calls(("c3", "read_articles", {"ids": ["E1"]})),
        DONE,
    ])
    final = FakeChat([ANSWERED])

    answer = run_turn("我关注的有什么新进展", channel="telegram", user_key="tg:1", request_id="r1",
                      config=AgentConfig(),
                      models=(ModelHandle("gather", "fake", "g", gather), ModelHandle("final", "fake", "f", final)))

    trace = turn.persisted[-1]
    now = _turn_now(trace)
    # Each follow is searched with its English terms over the days since its last check. Only the follow whose search
    # succeeded counts as checked; c2, a search for a follow's own words, does not.
    assert sorted(searched) == [
        ("Genie 4 world model", memory.check_window(FOLLOWS[0], now.date()), "importance", follows.STORY_LIMIT),
        ("embodied AI", 14, "importance", follows.STORY_LIMIT)]
    assert answer.followed_queries == ("Genie 4",)
    days = memory.check_window(FOLLOWS[0], now.date())
    assert answer.process == Process("查看 2 个关注项，检索 1 次，阅读 1 篇报道", (
        ProcessItem("查看", "Genie 4 的新动态", f"最近 {days} 天内找到 1 条新闻"),
        ProcessItem("查看", "具身智能的新动态", "检索失败"),
        ProcessItem("检索", "具身智能", "最近 14 天内没有找到"),
        READ,
    ))
    assert lookups == [("list", "tg:1", memory.MAX_FOLLOWS)]
    assert _spans(trace, "context")[0].output["follows"] == [
        {"query": "Genie 4", "last_checked_at": "2026-11-09T17:00:00+00:00"},
        {"query": "具身智能", "last_checked_at": None}]
    check = next(span for span in _spans(trace, "tool") if span.name == "check_follows")
    assert [(item["follow"], item["error"]) for item in check.output["follows"]] == [
        ("Genie 4", None), ("具身智能", "RuntimeError")]
    system = gather.calls[0][0]
    assert system == SystemMessage(prompt_text.render(
        "gather.system", clock=prompts.gather_clock(now), corpus=CORPUS, follows=memory.render_follows(FOLLOWS, now.date())))
    assert "- Genie 4: last checked 2026-11-10, " in system.content
    first_result = next(m for m in gather.calls[1] if isinstance(m, ToolMessage))
    assert " | Coverage: 1 report, 1 publisher\n" in first_result.content
    assert 'Follow "具身智能", query="embodied AI", last 14 days: search failed: RuntimeError.' in first_result.content
    brief = final.calls[0][-1].content
    assert "Coverage: 1 report, 1 publisher, first 2026-11-12, latest 2026-11-12\n" in brief


def test_follow_lookup_failure_runs_the_turn_without_follows(turn, monkeypatch):
    def broken(user_key, limit):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(nodes.memory, "list_follows", broken)

    r = turn([_search("Genie 4"), DONE], [ANSWERED], user_key="tg:1")

    assert r.answer.status == "answered"
    assert r.gather.calls[0][0] == SystemMessage(prompt_text.render(
        "gather.system", clock=prompts.gather_clock(_turn_now(r.trace)), corpus=CORPUS, follows=""))
    assert _spans(r.trace, "context")[0].warnings == ["follows_unavailable"]


def test_turn_without_user_key_skips_memory(turn, monkeypatch):
    monkeypatch.setattr(nodes.memory, "list_follows", lambda *_a: pytest.fail("memory read without user_key"))

    r = turn([_search("Genie 4"), DONE], [ANSWERED])

    assert r.answer == _answered(tool_calls=("search_news",), process=SEARCH_ONLY)
