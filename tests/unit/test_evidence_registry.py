from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from agent.evidence import EvidenceRegistry, render_for_synthesis
from agent.types import Article, StoryStats

ARTICLE = Article(
    article_id=30,
    url="https://techcrunch.com/genie-4",
    title="Google’s Genie 4 world model adds audio, more realistic worlds",
    title_zh="谷歌 Genie 4 世界模型新增音频与更真实的场景",
    summary="谷歌发布 Genie 4。",
    topics=("ai",),
    kind="release",
    publisher="TechCrunch",
    publisher_kind="media",
    lang="en",
    published_at=datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc),  # 2026-11-12 00:30 in Beijing
    body_level="full",
    story_id=5,
)
STATS = StoryStats(
    story_id=5,
    report_count=5,
    publisher_count=3,
    publisher_kinds=("media", "official"),
    first_at=datetime(2026, 11, 10, 16, tzinfo=timezone.utc),
    last_at=datetime(2026, 11, 12, 16, tzinfo=timezone.utc),
)
OTHER = replace(ARTICLE, article_id=31, url="https://theverge.com/genie-4", title="Genie 4 arrives", title_zh="",
                kind="news", publisher="The Verge")


def _article(article_id: int, url: str, **fields) -> Article:
    return replace(ARTICLE, article_id=article_id, url=url, **fields)


def test_ids_follow_registration_order_and_reuse_same_url() -> None:
    registry = EvidenceRegistry()
    first = registry.add_article(_article(1, "https://a.com/x"), "summary")
    second = registry.add_article(_article(2, "https://b.com/y"), "listed")
    again = registry.add_article(_article(1, "https://www.a.com/x/?utm_source=feed"), "listed")
    assert [first.eid, second.eid, again.eid] == ["E1", "E2", "E1"]
    assert registry.by_url("http://a.com/x").eid == "E1"
    assert registry.by_url("https://c.com/z") is None
    assert [e.eid for e in registry.items()] == ["E1", "E2"]


def test_level_only_rises_and_new_stats_replace_old() -> None:
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "listed")
    assert registry.add_article(ARTICLE, "summary", STATS).level == "summary"
    evidence = registry.add_article(ARTICLE, "listed")
    assert (evidence.level, evidence.stats) == ("summary", STATS)
    newer = replace(STATS, report_count=6)
    assert registry.add_article(ARTICLE, "listed", newer).stats == newer
    registry.set_fulltext("E1", "正文", 2, False)
    assert registry.add_article(ARTICLE, "summary").level == "fulltext"


def test_citable_ids_are_summary_and_fulltext() -> None:
    registry = EvidenceRegistry()
    registry.add_article(_article(1, "https://a.com/1"), "listed")
    registry.add_article(_article(2, "https://a.com/2"), "summary")
    registry.add_article(_article(3, "https://a.com/3"), "listed")
    registry.add_article(_article(4, "https://a.com/4"), "listed")
    registry.set_fulltext("E4", "正文", 2, False)
    assert registry.citable_ids() == {"E2", "E4"}


def _two_fulltext_registry() -> EvidenceRegistry:
    registry = EvidenceRegistry()
    registry.add_article(_article(9, "https://a.com/listed"), "listed")
    registry.add_article(ARTICLE, "summary", STATS)
    registry.add_article(OTHER, "listed")
    registry.set_fulltext("E2", "正" * 40, 12034, True)
    registry.set_fulltext("E3", "文" * 40, 21840, True)
    return registry


ARTICLE_BLOCK = (
    "[E2] 标题：Google’s Genie 4 world model adds audio, more realistic worlds\n"
    "中文标题：谷歌 Genie 4 世界模型新增音频与更真实的场景\n"
    "发布方：TechCrunch，媒体｜时间：2026-11-12｜领域：ai｜类型：release\n"
    "同一故事：5 篇报道，3 个发布方，含官方发布，首次 2026-11-11，最近 2026-11-13\n"
    "摘要：谷歌发布 Genie 4。"
)
ARTICLE_BODY = "\n正文，前 40 字符，全文 12034 字符：\n" + "正" * 40
OTHER_BLOCK = (
    "[E3] 标题：Genie 4 arrives\n"
    "发布方：The Verge，媒体｜时间：2026-11-12｜领域：ai｜类型：news\n"
    "摘要：谷歌发布 Genie 4。"
)
OTHER_BODY = "\n正文，前 40 字符，全文 21840 字符：\n" + "文" * 40


def test_render_formats_articles_with_and_without_story_stats() -> None:
    expected = ARTICLE_BLOCK + ARTICLE_BODY + "\n\n" + OTHER_BLOCK + OTHER_BODY
    assert render_for_synthesis(_two_fulltext_registry(), 10_000) == (expected, [])


def test_render_omits_optional_lines_and_marks_missing_body() -> None:
    registry = EvidenceRegistry()
    registry.add_article(_article(9, "https://a.com/listed"), "listed")
    registry.add_article(
        replace(ARTICLE, title_zh="", body_level="excerpt", topics=("ai", "software"), kind=None),
        "summary",
        replace(STATS, publisher_kinds=("media",)),
    )
    registry.add_article(OTHER, "listed")
    registry.set_fulltext("E3", "正文全文", 4, False)
    text, dropped = render_for_synthesis(registry, 10_000)
    assert dropped == []
    assert text == (
        "[E2] 标题：Google’s Genie 4 world model adds audio, more realistic worlds\n"
        "发布方：TechCrunch，媒体｜时间：2026-11-12｜领域：ai、software\n"
        "同一故事：5 篇报道，3 个发布方，首次 2026-11-11，最近 2026-11-13\n"
        "摘要：谷歌发布 Genie 4。\n"
        "本文没有入库正文，摘要只依据标题和摘录生成\n"
        "\n"
        "[E3] 标题：Genie 4 arrives\n"
        "发布方：The Verge，媒体｜时间：2026-11-12｜领域：ai｜类型：news\n"
        "摘要：谷歌发布 Genie 4。\n"
        "正文，全文 4 字符：\n"
        "正文全文"
    )


def test_render_drops_bodies_from_the_last_registered_until_within_limit() -> None:
    registry = _two_fulltext_registry()
    without_other_body = ARTICLE_BLOCK + ARTICLE_BODY + "\n\n" + OTHER_BLOCK
    without_bodies = ARTICLE_BLOCK + "\n\n" + OTHER_BLOCK
    assert render_for_synthesis(registry, len(without_other_body)) == (without_other_body, ["E3"])
    assert render_for_synthesis(registry, len(without_other_body) - 1) == (without_bodies, ["E2", "E3"])
    assert render_for_synthesis(registry, 10) == (without_bodies, ["E2", "E3"])
