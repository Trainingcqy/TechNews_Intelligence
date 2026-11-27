from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from agent import progress
from agent.evidence import EvidenceRegistry
from agent.memory import Follow
from agent.retrieval.search import SearchResult
from agent.tools.follows import FollowCheck, FollowsResult
from agent.tools.read import ReadItem, ReadResult
from agent.tools.registry import CheckFollowsArgs, PreparedCall, ReadArticlesArgs, SearchNewsArgs, ToolPayload
from agent.types import Article, Process, ProcessItem, Progress, StoryHit, StoryStats

TODAY = date(2026, 11, 27)
PUBLISHED = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
ARTICLE = Article(
    article_id=1, url="https://www.cnbc.com/huawei", title="U.S. jury convicts Huawei", title_zh="", summary="",
    topics=(), kind="news", publisher="CNBC", publisher_kind="media", lang="en", published_at=PUBLISHED,
    body_level="full", story_id=10,
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
    assert progress.window(SearchNewsArgs(**fields), TODAY) == expected


@pytest.mark.parametrize("fields, expected", [
    ({"query": "Huawei verdict"}, "Huawei verdict"),
    ({}, "要闻"),
    ({"topic": "ai"}, "人工智能要闻"),
    ({"topic": "ai", "sort": "time"}, "人工智能最新新闻"),
])
def test_subject(fields, expected):
    assert progress.subject(SearchNewsArgs(**fields)) == expected


def test_step_merges_the_calls_of_a_step():
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")
    searches = [PreparedCall(0, "c0", "search_news", SearchNewsArgs(query="Huawei verdict", days=7)),
                PreparedCall(1, "c1", "search_news", SearchNewsArgs(query="China reaction", days=7))]
    # E9 is not registered: the read is counted but has no title to show.
    read = PreparedCall(2, "c2", "read_articles", ReadArticlesArgs(ids=["E1", "E9"], urls=["https://www.cnbc.com/x"]))

    assert progress.step([*searches, read], registry, [], TODAY) == Progress(
        "正在检索并阅读 3 篇报道", "Huawei verdict、China reaction 最近 7 天")
    assert progress.step([read], registry, [], TODAY) == Progress("正在阅读 3 篇报道", "U.S. jury convicts Huawei、cnbc.com")


def test_step_names_the_follows_and_windows_that_differ():
    check = PreparedCall(0, "c0", "check_follows", CheckFollowsArgs())
    search = PreparedCall(1, "c1", "search_news", SearchNewsArgs(query="Nvidia"))
    browse = PreparedCall(2, "c2", "search_news", SearchNewsArgs(start_date="2026-11-27"))
    read = PreparedCall(3, "c3", "read_articles", ReadArticlesArgs(ids=["E1"]))

    assert progress.step([check], EvidenceRegistry(), ["谷歌", "OpenAI"], TODAY) == Progress(
        "正在检查 2 个关注项", "谷歌、OpenAI")
    assert progress.step([check, search, browse, read], EvidenceRegistry(), ["谷歌"], TODAY) == Progress(
        "正在检查 1 个关注项、检索并阅读 1 篇报道", "Nvidia 最近 14 天、要闻 今天")


def test_found_counts_distinct_stories_and_read_bodies():
    searches = [
        _payload("search_news", SearchNewsArgs(query="a"), SearchResult(hits=[_hit(1), _hit(2)], weak_match=False)),
        _payload("search_news", SearchNewsArgs(query="b"), SearchResult(hits=[_hit(2)], weak_match=False), index=1),
    ]
    read = _payload("read_articles", ReadArticlesArgs(ids=["E1", "E2"]),
                    ReadResult([ReadItem("E1", ARTICLE, "正文", 2, None), UNREAD]), index=2)

    assert progress.found(searches) == "找到 2 条新闻"
    assert progress.found([*searches, read]) == "找到 2 条新闻，读到 1 篇全文"
    assert progress.found([_payload("search_news", SearchNewsArgs(), None, ok=False)]) == "没有找到新闻"
    assert progress.found([_payload("read_articles", ReadArticlesArgs(ids=["E2"]), ReadResult([UNREAD]))]) == "没有读到全文"
    assert progress.found([_payload("read_articles", ReadArticlesArgs(ids=["E1"]), None, ok=False)]) == "没有读到全文"
    assert progress.found([]) == ""


def test_record_lists_filters_failures_and_each_article_once():
    args = SearchNewsArgs(query="OpenAI", topic="ai", kind="research", publisher_kind="official", start_date="2026-11-01")
    nothing = SearchResult(hits=[], weak_match=False)

    assert progress.record(_payload("search_news", args, nothing), TODAY, set()) == [
        ProcessItem("检索", "OpenAI", "仅人工智能领域 仅研究类 仅官方渠道 2026-11-01 至今 无结果")]
    # Without query the topic is part of the subject, not a filter.
    browse = SearchNewsArgs(topic="ai", days=7)
    assert progress.record(_payload("search_news", browse, SearchResult(hits=[_hit(1)], weak_match=False)), TODAY,
                           set()) == [ProcessItem("检索", "人工智能要闻", "最近 7 天 1 条新闻")]
    assert progress.record(_payload("search_news", SearchNewsArgs(), None, ok=False), TODAY, set()) == [
        ProcessItem("检索", "要闻", "最近 14 天 失败")]

    read_ids: set[int] = set()
    read = _payload("read_articles", ReadArticlesArgs(ids=["E1", "E2"]),
                    ReadResult([ReadItem("E1", ARTICLE, "正文", 2, None), UNREAD]))
    assert progress.record(read, TODAY, read_ids) == [ProcessItem("阅读", "U.S. jury convicts Huawei", "CNBC 2026-11-25")]
    assert progress.record(read, TODAY, read_ids) == []


def test_record_of_a_follow_check_names_the_search_terms_only_when_they_differ():
    checks = FollowsResult([
        FollowCheck(Follow(1, "谷歌", None), "Google", 3, SearchResult(hits=[_hit(1)], weak_match=False), None),
        FollowCheck(Follow(2, "OpenAI", None), "openai", 14, None, "TimeoutError"),
    ])

    assert progress.record(_payload("check_follows", CheckFollowsArgs(), checks), TODAY, set()) == [
        ProcessItem("检查", "谷歌", "Google 最近 3 天 1 条新闻"),
        ProcessItem("检查", "OpenAI", "最近 14 天 失败"),
    ]


def test_process_summary_counts_each_action():
    items = [ProcessItem("检索", "a", ""), ProcessItem("阅读", "b", ""), ProcessItem("检查", "c", ""),
             ProcessItem("检索", "d", "")]

    assert progress.process(items) == Process("检查 1 个关注项，检索 2 次，阅读 1 篇报道", tuple(items))
    assert progress.process([]) is None
