from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent.citations import (
    BLOCKED_TEXT,
    ERROR_TEXT,
    CitationCheck,
    finalize_answer,
    resolve_citations,
    rewrite_history_citations,
    split_status,
)
from agent.evidence import EvidenceRegistry
from agent.types import Article, Discussion, Source

ARTICLE = Article(
    article_id=1,
    url="https://a.com/1",
    title="Title 1",
    title_zh="标题 1",
    summary="",
    topics=("ai",),
    kind="news",
    publisher="TechCrunch",
    publisher_kind="media",
    lang="en",
    published_at=datetime(2026, 11, 11, 16, tzinfo=timezone.utc),  # 2026-11-12 00:00 in Beijing
    body_level="full",
    hn_score=None,
    hn_comments=None,
    hn_sighting_id=None,
    story_id=10,
)


def _registry() -> EvidenceRegistry:
    """E1 summary (story 10), E2 listed, E3 summary (story 30), E4 discussion of E2 read in full."""
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")
    registry.add_article(replace(ARTICLE, article_id=2, url="https://a.com/2", story_id=20), "listed")
    registry.add_article(
        replace(ARTICLE, article_id=3, url="https://b.com/3", title="Title 3", title_zh="", publisher="Google",
                published_at=datetime(2026, 11, 10, 16, tzinfo=timezone.utc), story_id=30),
        "summary",
    )
    registry.add_discussion(
        Discussion(sighting_id=7, article_id=2, url="https://news.ycombinator.com/item?id=7", title="Title 1",
                   score=10, comment_total=3)
    )
    registry.set_fulltext("E4", "comments", 8, False)
    return registry


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("STATUS: answered\n正文", ("answered", "正文")),
        ("\n  status: Insufficient  \n正文", ("insufficient", "\n正文")),
        ("正文\nSTATUS: answered", (None, "正文\nSTATUS: answered")),
        ("STATUS: unknown\n正文", (None, "STATUS: unknown\n正文")),
        ("STATUS: answered now\n正文", (None, "STATUS: answered now\n正文")),
    ],
)
def test_split_status(raw: str, expected: tuple[str | None, str]) -> None:
    assert split_status(raw) == expected


def test_resolve_renumbers_in_first_appearance_order() -> None:
    text, sources, story_ids, check = resolve_citations("甲 [E3]，乙 [E1]，丙 [E3][E4]。", _registry())
    assert text == "甲 [1]，乙 [2]，丙 [1][3]。"
    assert check == CitationCheck(cited=(("E3", 1), ("E1", 2), ("E4", 3)))
    assert sources == (
        Source(n=1, url="https://b.com/3", title="Title 3", title_zh="", source="Google", published_at="2026-11-11"),
        Source(n=2, url="https://a.com/1", title="Title 1", title_zh="标题 1", source="TechCrunch",
               published_at="2026-11-12"),
        Source(n=3, url="https://news.ycombinator.com/item?id=7", title="Title 1", title_zh="",
               source="HackerNews 讨论", published_at=""),
    )
    assert story_ids == (30, 10, 20)


def test_resolve_expands_lists_and_merges_duplicates() -> None:
    text, sources, _, _ = resolve_citations("甲 [E1, E3]，乙 [E3、E1]，丙 [E1，E1]。", _registry())
    assert text == "甲 [1][2]，乙 [2][1]，丙 [1]。"
    assert len(sources) == 2


def test_resolve_drops_raw_urls_up_to_non_ascii() -> None:
    text, _, _, _ = resolve_citations("见（https://a.com/1）的说法 [E1]，原文 https://a.com/x，有更多。", _registry())
    assert text == "见的说法 [1]，原文，有更多。"


def test_resolve_drops_only_the_invalid_id() -> None:
    text, sources, _, check = resolve_citations("甲 [E2][E1] 乙 [E9]。丙 [E1, E99] 丁（[E9]）", _registry())
    assert text == "甲 [1] 乙。丙 [1] 丁"
    assert [source.url for source in sources] == ["https://a.com/1"]
    assert check.removed_eids == ("E2", "E9", "E99")


def test_resolve_removes_numeric_citations_and_raw_urls() -> None:
    text, sources, _, check = resolve_citations("甲 [2] 乙 https://x.com/a 丙 [E1]，见 https://y.com/b 。", _registry())
    assert text == "甲 乙 丙 [1]，见。"
    assert len(sources) == 1
    assert (check.removed_numeric, check.removed_urls, check.removed_eids) == (1, 2, ())


def test_resolve_keeps_brackets_and_spacing_the_model_wrote() -> None:
    text, _, _, check = resolve_citations("调用 run()  即可 [E1]\n  - 缩进", _registry())
    assert text == "调用 run()  即可 [1]\n  - 缩进"
    assert check.removed_any is False


def test_finalize_empty_output_is_error() -> None:
    answer, check = finalize_answer("  \n", _registry(), "问题")
    assert (answer.status, answer.text, answer.error_code) == ("error", ERROR_TEXT["zh"], "empty_synthesis")
    assert check == CitationCheck()


def test_finalize_insufficient_does_not_need_citations() -> None:
    answer, check = finalize_answer("STATUS: insufficient\n只知道一部分 [E9]。", _registry(), "问题")
    assert (answer.status, answer.text, answer.sources, answer.error_code) == (
        "insufficient", "只知道一部分。", (), None
    )
    assert (check.status_line, check.removed_eids) == ("insufficient", ("E9",))


@pytest.mark.parametrize("raw, status_line", [("STATUS: answered\n结论 [E3] 😀", "answered"), ("结论 [E3] 😀", None)])
def test_finalize_answered_with_valid_citation(raw: str, status_line: str | None) -> None:
    answer, check = finalize_answer(raw, _registry(), "问题")
    assert (answer.status, answer.text, answer.story_ids) == ("answered", "结论 [1]", (30,))
    assert [source.url for source in answer.sources] == ["https://b.com/3"]
    assert (check.status_line, check.cited) == (status_line, (("E3", 1),))


def test_finalize_without_valid_citation_is_blocked_in_question_language() -> None:
    answer, _ = finalize_answer("STATUS: answered\nConclusion [E2].", _registry(), "What happened?")
    assert (answer.status, answer.text, answer.sources, answer.error_code) == (
        "blocked", BLOCKED_TEXT["en"], (), "no_valid_citation"
    )


def test_rewrite_history_citations() -> None:
    sources = [
        {"n": 1, "url": "https://b.com/3/"},
        {"n": 2, "url": "https://gone.com/x"},
        {"n": 3, "url": "https://a.com/1"},
    ]
    text = rewrite_history_citations("甲[1]，乙[2]，丙[3]，丁[4]。", sources, _registry())
    assert text == "甲[E3]，乙，丙[E1]，丁。"
