from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from agent import progress
from agent.evidence import EvidenceRegistry
from agent.memory import Follow, PastAnswer
from agent.retrieval.search import SearchResult
from agent.tools.follows import FollowCheck, FollowsResult
from agent.tools.history import HistoryItem, HistoryResult
from agent.tools.read import ReadItem, ReadResult
from agent.tools.registry import (
    BrowseNewsArgs,
    CheckFollowsArgs,
    PreparedCall,
    ReadArticlesArgs,
    ReadHistoryArgs,
    SearchNewsArgs,
    ToolPayload,
)
from agent.types import Article, Process, ProcessItem, Progress, StoryHit, StoryStats

TODAY = date(2026, 11, 27)
PUBLISHED = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
ARTICLE = Article(
    article_id=1, url="https://www.cnbc.com/huawei", title="U.S. jury convicts Huawei", title_zh="", summary="",
    topics=(), kind="news", publisher="CNBC", publisher_kind="media", lang="en", published_at=PUBLISHED,
    first_seen_at=PUBLISHED, body_level="full", story_id=10,
)
UNREAD = ReadItem("E2", replace(ARTICLE, article_id=2), None, 0, "no full text stored")


def _hit(story_id: int) -> StoryHit:
    stats = StoryStats(story_id=story_id, report_count=1, publisher_count=1, publisher_kinds=("media",),
                       first_at=PUBLISHED, last_at=PUBLISHED)
    return StoryHit(lead=replace(ARTICLE, story_id=story_id), stats=stats, related=(), match_score=0.9)


def _payload(name, args, data, ok=True, index=0) -> ToolPayload:
    return ToolPayload(index=index, call_id=f"c{index}", name=name, args=args, ok=ok, data=data)


@pytest.mark.parametrize("fields, expected", [
    ({"days": 7}, "最近 7 天"),
    ({"start_date": "2026-11-27"}, "今天"),
    ({"start_date": "2026-11-01"}, "2026-11-01 至今"),
    ({"start_date": "2026-11-27", "end_date": "2026-11-27"}, "今天"),
    ({"start_date": "2026-11-20", "end_date": "2026-11-20"}, "2026-11-20"),
    ({"start_date": "2026-11-01", "end_date": "2026-11-15"}, "2026-11-01 至 2026-11-15"),
])
def test_window(fields, expected):
    assert progress.window(BrowseNewsArgs(**fields), TODAY) == expected
    assert progress.window(SearchNewsArgs(query="x", **fields), TODAY) == expected


@pytest.mark.parametrize("args, expected", [
    (SearchNewsArgs(query="Huawei verdict"), "Huawei verdict"),
    (SearchNewsArgs(query="Huawei verdict", topic="ai", sort="time", start_date="2026-11-27"), "Huawei verdict"),
    (BrowseNewsArgs(), "最近 14 天的要闻"),
    (BrowseNewsArgs(topic="ai", start_date="2026-11-27"), "今天的人工智能要闻"),
    (BrowseNewsArgs(topic="ai", sort="time", start_date="2026-11-01", end_date="2026-11-15"),
     "2026-11-01 至 2026-11-15 的人工智能最新新闻"),
])
def test_subject(args, expected):
    assert progress.subject(args, TODAY) == expected


def test_step_merges_the_calls_of_a_step():
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")
    searches = [PreparedCall(0, "c0", "search_news", SearchNewsArgs(query="Huawei verdict", days=7)),
                PreparedCall(1, "c1", "search_news", SearchNewsArgs(query="China reaction", days=7))]
    # E9 is not registered: the read is counted but has no title to show.
    read = PreparedCall(2, "c2", "read_articles", ReadArticlesArgs(ids=["E1", "E9"], urls=["https://www.cnbc.com/x"]))

    assert progress.step([*searches, read], registry, TODAY) == Progress(
        "正在检索并阅读 3 篇报道", "Huawei verdict、China reaction")
    assert progress.step([read], registry, TODAY) == Progress("正在阅读 3 篇报道", "U.S. jury convicts Huawei、cnbc.com")


def test_step_of_a_follow_check_names_no_follows_and_what_it_searches():
    check = PreparedCall(0, "c0", "check_follows", CheckFollowsArgs())
    search = PreparedCall(1, "c1", "search_news", SearchNewsArgs(query="Nvidia"))
    browse = PreparedCall(2, "c2", "browse_news", BrowseNewsArgs(start_date="2026-11-27"))
    read = PreparedCall(3, "c3", "read_articles", ReadArticlesArgs(ids=["E1"]))

    assert progress.step([check], EvidenceRegistry(), TODAY) == Progress("正在查看关注项")
    # A browse carries its window in its subject.
    assert progress.step([check, search, browse, read], EvidenceRegistry(), TODAY) == Progress(
        "正在查看关注项、检索并阅读 1 篇报道", "Nvidia、今天的要闻")
    assert progress.step([browse], EvidenceRegistry(), TODAY) == Progress("正在检索", "今天的要闻")


def test_step_of_read_history_names_its_keywords_after_search_subjects():
    recall = PreparedCall(0, "c0", "read_history", ReadHistoryArgs(keywords=["Megaspeed", "兆速"]))
    recent = PreparedCall(1, "c1", "read_history", ReadHistoryArgs())
    check = PreparedCall(2, "c2", "check_follows", CheckFollowsArgs())
    search = PreparedCall(3, "c3", "search_news", SearchNewsArgs(query="Nvidia"))
    read = PreparedCall(4, "c4", "read_articles", ReadArticlesArgs(ids=["E1"]))
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")

    assert progress.step([recall], registry, TODAY) == Progress("正在查看之前的回答", "Megaspeed、兆速")
    assert progress.step([recent], registry, TODAY) == Progress("正在查看之前的回答")
    assert progress.step([recall, search], registry, TODAY) == Progress("正在查看之前的回答并检索", "Nvidia")
    assert progress.step([recent, read], registry, TODAY) == Progress(
        "正在查看之前的回答并阅读 1 篇报道", "U.S. jury convicts Huawei")
    assert progress.step([check, recall], registry, TODAY) == Progress("正在查看关注项、之前的回答", "Megaspeed、兆速")
    assert progress.step([check, recent], registry, TODAY) == Progress("正在查看关注项、之前的回答")


def _history(count: int) -> HistoryResult:
    answer = PastAnswer(TODAY, "q", "a", [{"url": "https://a.com/1"}])
    return HistoryResult([HistoryItem(answer, ["a"], [])] * count, {}, {"a.com/1": TODAY})


@pytest.mark.parametrize("args, result, expected", [
    (ReadHistoryArgs(keywords=["Megaspeed"]), _history(2), ProcessItem("回顾", "Megaspeed", "找到 2 次相关回答")),
    (ReadHistoryArgs(keywords=["Megaspeed", "兆速"]), _history(1), ProcessItem("回顾", "Megaspeed、兆速", "找到 1 次相关回答")),
    (ReadHistoryArgs(keywords=["Megaspeed"]), _history(0), ProcessItem("未找到", "Megaspeed", "相关内容")),
    (ReadHistoryArgs(keywords=["Megaspeed", "兆速"]), HistoryResult([], {}, {}),
     ProcessItem("未找到", "Megaspeed、兆速", "相关内容")),
    (ReadHistoryArgs(), _history(5), ProcessItem("查看", "最近 5 次回答", "")),
    (ReadHistoryArgs(limit=3), _history(2), ProcessItem("查看", "最近 2 次回答", "")),
    (ReadHistoryArgs(), HistoryResult([], {}, {}), ProcessItem("查看", "之前的回答", "没有记录")),
])
def test_record_of_read_history(args, result, expected):
    assert progress.record(_payload("read_history", args, result), TODAY, set()) == [expected]


def test_record_of_a_failed_read_history_lists_nothing_and_found_says_nothing():
    failed = _payload("read_history", ReadHistoryArgs(keywords=["Megaspeed"]), None, ok=False)
    assert progress.record(failed, TODAY, set()) == []
    assert progress.found([_payload("read_history", ReadHistoryArgs(), _history(2))]) == ""


def test_found_counts_distinct_stories_and_read_bodies():
    searches = [
        _payload("search_news", SearchNewsArgs(query="a"), SearchResult(hits=[_hit(1), _hit(2)], weak_match=False)),
        _payload("search_news", SearchNewsArgs(query="b"), SearchResult(hits=[_hit(2)], weak_match=False), index=1),
    ]
    read = _payload("read_articles", ReadArticlesArgs(ids=["E1", "E2"]),
                    ReadResult([ReadItem("E1", ARTICLE, "正文", 2, None), UNREAD]), index=2)

    assert progress.found(searches) == "找到 2 条新闻"
    assert progress.found([*searches, read]) == "找到 2 条新闻，读到 1 篇全文"
    assert progress.found([_payload("search_news", SearchNewsArgs(query="a"), None, ok=False)]) == "没有找到新闻"
    browse = _payload("browse_news", BrowseNewsArgs(), SearchResult(hits=[_hit(2), _hit(3)], weak_match=False))
    assert progress.found([*searches, browse]) == "找到 3 条新闻"
    assert progress.found([_payload("browse_news", BrowseNewsArgs(), None, ok=False)]) == "没有找到新闻"
    assert progress.found([_payload("read_articles", ReadArticlesArgs(ids=["E2"]), ReadResult([UNREAD]))]) == "没有读到全文"
    assert progress.found([_payload("read_articles", ReadArticlesArgs(ids=["E1"]), None, ok=False)]) == "没有读到全文"
    assert progress.found([]) == ""


@pytest.mark.parametrize("fields, expected", [
    ({"days": 7}, "最近 7 天内找到 1 条新闻"),
    ({"start_date": "2026-11-27"}, "今天找到 1 条新闻"),
    ({"start_date": "2026-11-01"}, "2026-11-01 至今找到 1 条新闻"),
    ({"start_date": "2026-11-20", "end_date": "2026-11-20"}, "2026-11-20 找到 1 条新闻"),
])
def test_record_of_a_search_says_what_it_found_in_its_window(fields, expected):
    payload = _payload("search_news", SearchNewsArgs(query="Nvidia", **fields),
                       SearchResult(hits=[_hit(1)], weak_match=False))
    assert progress.record(payload, TODAY, set()) == [ProcessItem("检索", "Nvidia", expected)]


def test_record_lists_filters_failures_and_each_article_once():
    args = SearchNewsArgs(query="OpenAI", topic="ai", kind="research", publisher_kind="official", start_date="2026-11-01")
    nothing = SearchResult(hits=[], weak_match=False)

    assert progress.record(_payload("search_news", args, nothing), TODAY, set()) == [
        ProcessItem("检索", "OpenAI", "仅人工智能领域、研究类、官方渠道，2026-11-01 至今没有找到")]
    # A browse names its window and topic in the subject, and lists its other filters.
    browse = BrowseNewsArgs(topic="ai", days=7)
    assert progress.record(_payload("browse_news", browse, SearchResult(hits=[_hit(1)], weak_match=False)), TODAY,
                           set()) == [ProcessItem("检索", "最近 7 天的人工智能要闻", "找到 1 条新闻")]
    filtered = BrowseNewsArgs(topic="ai", kind="research", publisher_kind="media", days=7)
    assert progress.record(_payload("browse_news", filtered, SearchResult(hits=[], weak_match=False)), TODAY,
                           set()) == [ProcessItem("检索", "最近 7 天的人工智能要闻", "仅研究类、媒体报道，没有找到")]
    assert progress.record(_payload("browse_news", BrowseNewsArgs(), None, ok=False), TODAY, set()) == [
        ProcessItem("检索", "最近 14 天的要闻", "检索失败")]
    assert progress.record(_payload("search_news", SearchNewsArgs(query="OpenAI"), None, ok=False), TODAY,
                           set()) == [ProcessItem("检索", "OpenAI", "检索失败")]

    read_ids: set[int] = set()
    read = _payload("read_articles", ReadArticlesArgs(ids=["E1", "E2"]),
                    ReadResult([ReadItem("E1", ARTICLE, "正文", 2, None), UNREAD]))
    assert progress.record(read, TODAY, read_ids) == [ProcessItem("阅读", "U.S. jury convicts Huawei", "CNBC 2026-11-25")]
    assert progress.record(read, TODAY, read_ids) == []


def test_record_of_a_follow_check_names_the_follow_and_what_it_found_since_its_last_check():
    # 17:00 UTC on 11-24 is 11-25 in Beijing.
    checked = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
    found, nothing = SearchResult(hits=[_hit(1), _hit(2), _hit(3)], weak_match=False), SearchResult([], False)
    checks = FollowsResult([
        FollowCheck(Follow(1, "谷歌", checked, "Google"), "Google", 30, found, None),
        FollowCheck(Follow(2, "Anthropic", checked, None), "Anthropic", 30, nothing, None),
        FollowCheck(Follow(3, "具身智能", None, "embodied AI"), "embodied AI", 14, found, None),
        FollowCheck(Follow(4, "OpenAI", None, None), "OpenAI", 14, nothing, None),
        FollowCheck(Follow(5, "英伟达", checked, "NVIDIA"), "NVIDIA", 30, None, "TimeoutError"),
        FollowCheck(Follow(6, "Meta", None, None), "Meta", 14, None, "TimeoutError"),
    ], {})

    assert progress.record(_payload("check_follows", CheckFollowsArgs(), checks), TODAY, set()) == [
        ProcessItem("查看", "谷歌的新动态", "2026-11-25 以来找到 3 条新闻"),
        ProcessItem("查看", "Anthropic 的新动态", "2026-11-25 以来没有找到"),
        ProcessItem("查看", "具身智能的新动态", "最近 14 天内找到 3 条新闻"),
        ProcessItem("查看", "OpenAI 的新动态", "最近 14 天内没有找到"),
        ProcessItem("查看", "英伟达的新动态", "检索失败"),
        ProcessItem("查看", "Meta 的新动态", "检索失败"),
    ]


def test_process_summary_counts_each_tool():
    items = [("search_news", ProcessItem("检索", "a", "")), ("read_articles", ProcessItem("阅读", "b", "")),
             ("check_follows", ProcessItem("查看", "c", "")), ("browse_news", ProcessItem("检索", "d", ""))]

    assert progress.process(items) == Process("查看 1 个关注项，检索 2 次，阅读 1 篇报道",
                                              tuple(item for _, item in items))
    assert progress.process([]) is None


def test_process_summary_counts_past_answers_apart_from_follows_that_share_their_action():
    listed = ("read_history", ProcessItem("查看", "最近 5 次回答", ""))
    recalled = ("read_history", ProcessItem("回顾", "Megaspeed", "找到 2 次相关回答"))
    follow = ("check_follows", ProcessItem("查看", "谷歌的新动态", "最近 3 天内找到 1 条新闻"))
    search = ("search_news", ProcessItem("检索", "Megaspeed", "最近 14 天内找到 1 条新闻"))
    read = ("read_articles", ProcessItem("阅读", "t", "CNBC 2026-11-25"))

    assert progress.process([listed, recalled]).summary == "查看之前的回答 2 次"
    assert progress.process([recalled, search, read]).summary == "查看之前的回答 1 次，检索 1 次，阅读 1 篇报道"
    assert progress.process([follow, listed, follow, recalled, follow, search]).summary == (
        "查看 3 个关注项、之前的回答 2 次，检索 1 次")
    assert progress.process([follow, search]).summary == "查看 1 个关注项，检索 1 次"
