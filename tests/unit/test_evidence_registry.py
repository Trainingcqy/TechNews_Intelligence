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
    "[E2] Title: Google’s Genie 4 world model adds audio, more realistic worlds\n"
    "Chinese title: 谷歌 Genie 4 世界模型新增音频与更真实的场景\n"
    "Publisher: TechCrunch, media | Date: 2026-11-12 | Topics: ai | Type: release\n"
    "Coverage: 5 reports, 3 publishers, including an official publisher, first 2026-11-11, latest 2026-11-13\n"
    "Summary: 谷歌发布 Genie 4。"
)
ARTICLE_BODY = "\nFull text, first 40 of 12034 characters:\n" + "正" * 40
OTHER_BLOCK = (
    "[E3] Title: Genie 4 arrives\n"
    "Publisher: The Verge, media | Date: 2026-11-12 | Topics: ai | Type: news\n"
    "Summary: 谷歌发布 Genie 4。"
)
OTHER_BODY = "\nFull text, first 40 of 21840 characters:\n" + "文" * 40


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
        "[E2] Title: Google’s Genie 4 world model adds audio, more realistic worlds\n"
        "Publisher: TechCrunch, media | Date: 2026-11-12 | Topics: ai, software\n"
        "Coverage: 5 reports, 3 publishers, first 2026-11-11, latest 2026-11-13\n"
        "Summary: 谷歌发布 Genie 4。\n"
        "No full text stored: the summary is based only on the title and an excerpt.\n"
        "\n"
        "[E3] Title: Genie 4 arrives\n"
        "Publisher: The Verge, media | Date: 2026-11-12 | Topics: ai | Type: news\n"
        "Summary: 谷歌发布 Genie 4。\n"
        "Full text, 4 characters:\n"
        "正文全文"
    )


def test_brief_puts_this_turn_first_and_leaves_out_history_before_bodies() -> None:
    registry = EvidenceRegistry()
    registry.add_article(_article(1, "https://h.com/1"), "summary", history=True)
    registry.add_article(_article(2, "https://h.com/2"), "summary", history=True)
    registry.add_article(OTHER, "summary")
    registry.set_fulltext("E3", "文" * 40, 40, False)

    full, left_out = render_for_synthesis(registry, 100_000)
    assert left_out == [] and full.index("[E3]") < full.index("[E1]") < full.index("[E2]")
    without_e2 = full[:full.index("\n\n[E2]")]
    assert render_for_synthesis(registry, len(without_e2)) == (without_e2, ["E2"])
    assert render_for_synthesis(registry, 10)[1] == ["E1", "E2", "E3"]


def test_finding_or_reading_a_history_item_in_this_turn_makes_it_current() -> None:
    registry = EvidenceRegistry()
    registry.add_article(_article(1, "https://h.com/1"), "summary", history=True)
    registry.add_article(_article(2, "https://h.com/2"), "summary", history=True)
    registry.add_article(_article(3, "https://h.com/3"), "summary")
    registry.add_article(_article(1, "https://h.com/1"), "listed")
    registry.set_fulltext("E2", "body", 4, False)
    registry.add_article(_article(3, "https://h.com/3"), "summary", history=True)
    assert [evidence.history for evidence in registry.items()] == [False, False, False]


def test_render_drops_bodies_from_the_last_registered_until_within_limit() -> None:
    registry = _two_fulltext_registry()
    without_other_body = ARTICLE_BLOCK + ARTICLE_BODY + "\n\n" + OTHER_BLOCK
    without_bodies = ARTICLE_BLOCK + "\n\n" + OTHER_BLOCK
    assert render_for_synthesis(registry, len(without_other_body)) == (without_other_body, ["E3"])
    assert render_for_synthesis(registry, len(without_other_body) - 1) == (without_bodies, ["E2", "E3"])
    assert render_for_synthesis(registry, 10) == (without_bodies, ["E2", "E3"])
