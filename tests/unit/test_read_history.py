"""read_history: its arguments, the answers it selects, the lines it takes, the evidence it registers and its text."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent import trace_store
from agent.config import AgentConfig, using_zone
from agent.evidence import EvidenceRegistry
from agent.tools import history
from agent.tools.registry import PreparedCall, ReadHistoryArgs, TurnState, commit_call, prepare_call, run_call
from agent.trace import request_trace
from agent.types import Article
from common import prompt_text
from common.urls import url_key

RAISED = Article(
    article_id=41, url="https://www.datacenterdynamics.com/megaspeed/", title="Megaspeed raises funds", title_zh="",
    summary="Megaspeed raised funds.", topics=("chips",), kind="news", publisher="Data Center Dynamics",
    publisher_kind="media", lang="en",
    published_at=datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc),  # 11-24 in Beijing, 11-23 in New York
    first_seen_at=datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc), body_level="full", story_id=7,
)
BOUGHT = replace(RAISED, article_id=42, url="https://techcrunch.com/megaspeed-chips", title="Megaspeed buys chips",
                 publisher="TechCrunch", story_id=8,
                 published_at=datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc))  # 11-25 in Beijing, 11-24 in UTC
NVIDIA = "https://nvidia.com/news/b300"


def _message(message_id: int, thread_id: str, role: str, text: str, day: int, sources=None) -> dict:
    message = {"role": role, "parts": [{"text": text}], "id": message_id, "thread_id": thread_id,
               "created_at": datetime(2026, 11, day, 2, 0, tzinfo=timezone.utc)}
    if sources is not None:
        message["sources"] = [{"n": n, "url": url} for n, url in enumerate(sources, 1)]
    return message


def _turn(first_id: int, thread_id: str, question: str, answer: str, day: int, sources) -> list[dict]:
    return [_message(first_id, thread_id, "user", question, day),
            _message(first_id + 1, thread_id, "model", answer, day, sources)]


@pytest.fixture
def db(monkeypatch):
    lookups: list[list[str]] = []

    class Conn:
        def cursor(self):
            return self

        def close(self):
            pass

    def by_keys(cur, keys):
        lookups.append(list(keys))
        return [a for a in (RAISED, BOUGHT) if url_key(a.url) in keys]

    monkeypatch.setattr(history, "get_conn", Conn)
    monkeypatch.setattr(history, "put_conn", lambda conn: None)
    monkeypatch.setattr(history, "fetch_articles_by_url_keys", by_keys)
    return lookups


def _call(registry: EvidenceRegistry, messages: list[dict] | None, **fields):
    reader = None if messages is None else (lambda: messages)
    prepared = PreparedCall(0, "c0", "read_history", ReadHistoryArgs(**fields))
    result = history.run_read_history(prepared, registry=registry, state=TurnState(user_urls=set(),
                                                                                   history_reader=reader))
    return result, history.commit_read_history(prepared.args, result, registry=registry)


def test_args_defaults_bounds_and_blank_keywords():
    assert (ReadHistoryArgs().keywords, ReadHistoryArgs().limit) == ([], 5)
    assert ReadHistoryArgs(keywords=[" Megaspeed ", "兆速"], limit=10).keywords == ["Megaspeed", "兆速"]
    assert ReadHistoryArgs(limit=1).limit == 1

    def invalid(fields):
        return prepare_call(0, {"name": "read_history", "args": fields, "id": "c"}, state=TurnState(user_urls=set()),
                            config=AgentConfig()).gather_text

    assert invalid({"keywords": ["Megaspeed", "  "]}) == (
        "Invalid arguments: keywords: Value error, keywords must not be blank")
    assert invalid({"keywords": [f"k{i}" for i in range(6)]}).startswith(
        "Invalid arguments: keywords: List should have at most 5 items")
    assert invalid({"limit": 0}) == "Invalid arguments: limit: Input should be greater than or equal to 1"
    assert invalid({"limit": 11}) == "Invalid arguments: limit: Input should be less than or equal to 10"
    assert invalid({"query": "x"}) == "Invalid arguments: query: Extra inputs are not permitted"


def test_keywords_select_answers_by_question_or_text_ignoring_case_newest_first_up_to_limit(db):
    messages = [
        *_turn(1, "t1", "Megaspeed 是什么", "一家公司", 20, [NVIDIA]),
        *_turn(3, "t1", "今天", "- 兆速融资", 21, [NVIDIA]),
        *_turn(5, "t2", "其他", "无关", 22, [NVIDIA]),
        *_turn(7, "t2", "再问", "MEGASPEED news", 23, [NVIDIA]),
    ]
    keywords = ["megaspeed", "兆速"]

    def selected(**fields):
        result, _ = _call(EvidenceRegistry(), messages, keywords=keywords, **fields)
        return [item.answer.text for item in result.items]

    assert selected(limit=2) == ["- 兆速融资", "MEGASPEED news"]
    assert selected(limit=10) == ["一家公司", "- 兆速融资", "MEGASPEED news"]


def test_without_keywords_the_most_recent_answers_are_selected_oldest_first(db):
    messages = [*_turn(1, "t1", "a", "甲", 20, [NVIDIA]), *_turn(3, "t2", "b", "乙", 21, [NVIDIA]),
                *_turn(5, "t1", "c", "丙", 22, [NVIDIA])]

    result, _ = _call(EvidenceRegistry(), messages, limit=2)

    assert [item.answer.text for item in result.items] == ["乙", "丙"]


@pytest.mark.parametrize("text, expected", [
    # A heading line that mentions a keyword brings its section, deeper headings included, up to a heading of the
    # same level; blank lines are not lines.
    ("# 总览\n\n## Megaspeed 动态\n- 融资\n\n### 细节\n- 收购\n## 其他\n- 无关",
     ["## Megaspeed 动态", "- 融资", "### 细节", "- 收购"]),
    # A higher heading ends the section too; a heading line may be indented.
    ("  ## megaspeed 动态\n- 融资\n# 下一节\n- 无关", ["  ## megaspeed 动态", "- 融资"]),
    # Any other line brings the heading line nearest before it.
    ("# 总览\n## 芯片\n- 英伟达\n- MEGASPEED 融资\n## 云\n- 甲骨文", ["## 芯片", "- MEGASPEED 融资"]),
    # Without a heading line before it, the line comes alone.
    ("开头一句。\n兆速融资\n## 云\n- 甲骨文", ["兆速融资"]),
    # Lines keep the order of the text and come once.
    ("## 芯片\n- 甲\n- Megaspeed 融资\n- 乙\n- 兆速收购\n## Megaspeed 后续\n- 扩产",
     ["## 芯片", "- Megaspeed 融资", "- 兆速收购", "## Megaspeed 后续", "- 扩产"]),
])
def test_excerpt_takes_matching_lines_with_their_headings(text, expected):
    assert history._excerpt(text, ["Megaspeed", "兆速"]) == expected


def test_excerpt_keeps_at_most_8_lines_or_takes_the_head_when_no_line_matches():
    section = "## Megaspeed\n" + "\n".join(f"- 第 {i} 条" for i in range(1, 11))
    assert history._excerpt(section, ["megaspeed"]) == ["## Megaspeed", *(f"- 第 {i} 条" for i in range(1, 8))]

    text = "a" * 1000 + "\n\n" + "b" * 1000
    assert history._excerpt(text, ["megaspeed"]) == ["a" * 1000, "b" * 498]


def test_keywords_register_the_cited_reports_and_rewrite_the_lines(db):
    first = "## 芯片\n- 英伟达发布新卡[2]\n- Megaspeed[3]获得融资[1][3][4]\n\n## 云\n- 甲骨文扩建[4]"
    second = "兆速收购芯片[2][1]\n它还计划扩产[3]"
    messages = [
        *_turn(1, "t1", "今天发生了什么", first, 25, [RAISED.url, NVIDIA, "https://gone.com/1", "https://gone.com/2"]),
        *_turn(3, "t2", "说说最近\n\n   发生的事情", second, 27, ["https://datacenterdynamics.com/megaspeed",
                                                         BOUGHT.url, "https://gone.com/3"]),
    ]
    registry = EvidenceRegistry()

    result, outcome = _call(registry, messages, keywords=["Megaspeed", "兆速"])

    assert outcome.ok
    assert outcome.gather_text.splitlines() == [
        'read_history results, keywords="Megaspeed, 兆速": 2 answers',
        "Answer on 2026-11-25, question: 今天发生了什么",
        "  ## 芯片",
        "  - Megaspeed获得融资[E1]",
        '  Cited: [E1] "Megaspeed raises funds" Data Center Dynamics · 2026-11-24',
        "  2 cited reports are no longer in the database.",
        "Answer on 2026-11-27, question: 说说最近 发生的事情",
        "  兆速收购芯片[E2][E1]",
        '  Cited: [E2] "Megaspeed buys chips" TechCrunch · 2026-11-25; [E1] "Megaspeed raises funds" Data Center '
        "Dynamics · 2026-11-24",
        "Newest report cited: 2026-11-25. To look for later reports, search from 2026-11-23.",
    ]
    # Only the sources the taken lines cite are looked up, in order of first citation.
    assert db == [["gone.com/1", url_key(RAISED.url), "gone.com/2", url_key(BOUGHT.url)]]
    assert [(e.eid, e.article.article_id, e.level, e.stats, e.history) for e in registry.items()] == [
        ("E1", 41, "summary", None, False), ("E2", 42, "summary", None, False)]
    assert history.history_trace_output(result) == {"answers": 2, "articles": [41, 42]}


def test_dates_of_cited_reports_are_in_the_user_zone(db):
    messages = _turn(1, "t1", "q", "Megaspeed[1][2]", 25, [RAISED.url, BOUGHT.url])

    with using_zone(ZoneInfo("America/New_York")):
        _, outcome = _call(EvidenceRegistry(), messages, keywords=["Megaspeed"])

    assert outcome.gather_text.splitlines()[-2:] == [
        '  Cited: [E1] "Megaspeed raises funds" Data Center Dynamics · 2026-11-23; [E2] "Megaspeed buys chips" '
        "TechCrunch · 2026-11-24",
        "Newest report cited: 2026-11-24. To look for later reports, search from 2026-11-22.",
    ]


def test_an_answer_matched_by_its_question_alone_gives_its_head_and_no_newest_line_without_registered_reports(db):
    messages = _turn(1, "t1", "Megaspeed 后来怎么样了", "之前没有提到。[1]\n\n下文", 25, ["https://gone.com/1"])
    registry = EvidenceRegistry()

    result, outcome = _call(registry, messages, keywords=["megaspeed"])

    assert outcome.gather_text == (
        'read_history results, keywords="megaspeed": 1 answer\n'
        "Answer on 2026-11-25, question: Megaspeed 后来怎么样了\n"
        "  之前没有提到。\n"
        "  下文\n"
        "  1 cited report is no longer in the database."
    )
    assert registry.items() == []
    assert history.history_trace_output(result) == {"answers": 1, "articles": []}


def test_without_keywords_the_recent_answers_are_listed_without_registering_evidence(monkeypatch):
    monkeypatch.setattr(history, "get_conn", lambda: pytest.fail("a listing does not look up the database"))
    head = "## 芯片\n\n- Megaspeed 融资[1]，英伟达[2]\n"
    messages = [
        *_turn(1, "t1", "最早", "不会列出", 20, [NVIDIA]),
        *_turn(3, "t1", "  今天\n发生了   什么 ", head + "x" * 400, 25, [RAISED.url, NVIDIA]),
        *_turn(5, "t2", "问" * 250, "单行回答[1]", 27, [BOUGHT.url]),
    ]
    registry = EvidenceRegistry()
    registry.add_article(RAISED, "summary", history=True)

    result, outcome = _call(registry, messages, limit=2)

    assert outcome.gather_text.splitlines() == [
        "read_history results: 2 most recent answers",
        "Answer on 2026-11-25, question: 今天 发生了 什么, 2 reports cited",
        "  ## 芯片",
        "  - Megaspeed 融资[E1]，英伟达",
        "  " + "x" * (300 - len(head)),
        f"Answer on 2026-11-27, question: {'问' * 200}, 1 report cited",
        "  单行回答",
    ]
    assert [e.eid for e in registry.items()] == ["E1"]
    assert history.history_trace_output(result) == {"answers": 2, "articles": []}
    _, single = _call(EvidenceRegistry(), messages, limit=1)
    assert single.gather_text.splitlines()[0] == "read_history results: 1 most recent answer"


def test_told_is_written_to_the_registry_whether_or_not_answers_match(db):
    messages = [*_turn(1, "t1", "q1", "Megaspeed[1]", 20, [RAISED.url, NVIDIA]),
                *_turn(3, "t2", "q2", "其他[1]", 25, [RAISED.url])]
    told = {url_key(RAISED.url): date(2026, 11, 25), url_key(NVIDIA): date(2026, 11, 20)}

    for fields in ({}, {"keywords": ["Megaspeed"]}, {"keywords": ["没有的事"]}):
        registry = EvidenceRegistry()
        _call(registry, messages, **fields)
        assert registry.told == told


def test_no_matching_answer_and_no_answers_on_record(db):
    messages = _turn(1, "t1", "q", "a[1]", 25, [NVIDIA])
    registry = EvidenceRegistry()

    _, outcome = _call(registry, messages, keywords=["Megaspeed", "兆速"])

    assert (outcome.ok, outcome.gather_text) == (True, 'read_history found no answers containing "Megaspeed, 兆速". '
                                                 + prompt_text.text("tools.read_history.no_result_hint"))
    no_sources = [_message(1, "t1", "user", "q", 25), _message(2, "t1", "model", "你好", 25, []),
                  {"role": "model", "parts": [{"text": "旧格式"}], "id": 3, "thread_id": "t1",
                   "created_at": datetime(2026, 11, 25, tzinfo=timezone.utc)}]
    for messages, fields in [(None, {}), (None, {"keywords": ["Megaspeed"]}), ([], {}), (no_sources, {}),
                             (no_sources, {"keywords": ["你好"]})]:
        registry = EvidenceRegistry()
        result, outcome = _call(registry, messages, **fields)
        assert (outcome.ok, outcome.gather_text) == (True, "read_history: no past answers on record.")
        assert (result.items, registry.told) == ([], {})


def test_a_failing_reader_fails_the_call(monkeypatch):
    def broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(trace_store, "insert_running", lambda trace: None)
    state = TurnState(user_urls=set(), history_reader=broken)
    prepared = prepare_call(0, {"name": "read_history", "args": {}, "id": "c"}, state=state, config=AgentConfig())
    with request_trace(question="q", channel="web"):
        payload = run_call(prepared, registry=EvidenceRegistry(), state=state)
    outcome = commit_call(payload, registry=EvidenceRegistry(), state=state, config=AgentConfig())

    assert (payload.ok, outcome.ok, outcome.gather_text) == (False, False, "Tool failed: RuntimeError")
    assert payload.span.error_code == "tool_runtimeerror"
