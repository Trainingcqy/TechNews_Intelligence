"""Tests for the taxonomy-trial sampling and statistics."""

from __future__ import annotations

import pytest

from eval.taxonomy import build_sample, score


@pytest.mark.parametrize("title,expected", [
    ("Anthropic prices IPO", True),
    ("GPT-5 is out", True),
    ("xAI raises $10B", True),
    ("Maxai launches", False),
    ("Gemini-like search", True),
    ("Lloyds bank outage", False),
])
def test_mentions_lab_matches_whole_words(title: str, expected: bool) -> None:
    assert build_sample.mentions_lab(title) is expected


def test_old_category_reads_only_known_prefixes() -> None:
    assert build_sample.old_category("[生态] 标题") == "生态"
    assert build_sample.old_category("[其他] 标题") is None
    assert build_sample.old_category("标题") is None


def _articles():
    rows = []
    for i in range(1, 9):
        rows.append({"id": i, "url": f"u{i}", "url_key": f"u{i}", "title": "Claude news" if i % 2 else "plain",
                     "title_cn": "", "has_summary": i != 8, "old_category": "AI" if i <= 4 else "安全",
                     "lab": i % 2 == 1, "created_at": ""})
    return rows


def test_draw_sample_strata_population_and_pair_filter() -> None:
    sample = build_sample.draw_sample(_articles(), [(1, 2, 0.95), (7, 99, 0.99)], per_category=2, lab_n=1, seed=3)
    assert sample["category_population"]["AI"] == 4 and sample["category_population"]["安全"] == 4
    assert (sample["lab_population"], sample["lab_population_old_ai"]) == (4, 2)
    assert len(sample["pairs"]) == 1  # the pair with an unknown article id is dropped
    by_id = {a["id"]: a["strata"] for a in sample["articles"]}
    assert "pair" in by_id[1] and "pair" in by_id[2]
    assert sum("category" in s for s in by_id.values()) == 4
    assert 8 not in {i for i, s in by_id.items() if "category" in s or "lab" in s}  # no summary, cannot be relabeled


def test_category_weights_and_shares_restore_the_old_distribution() -> None:
    weights = score.category_weights(["AI", "AI", "安全"], {"AI": 80, "安全": 20})
    assert weights == [40.0, 40.0, 20.0]
    assert score.weighted_shares(["ai", "software", "security"], weights) == pytest.approx({"ai": 0.4, "software": 0.4, "security": 0.2})


def test_pair_agreement_counts_only_fully_relabeled_pairs() -> None:
    sample = {"articles": [{"id": i, "old_category": "AI" if i < 3 else "商业"} for i in (1, 2, 3, 4)],
              "pairs": [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 1, "b": 3}]}
    labeled = {1: {"topics": ["ai", "business"], "kind": "news"}, 2: {"topics": ["business"], "kind": "news"},
               3: {"topics": ["ai"], "kind": "release"}, 4: {"topics": ["ai"], "kind": "release"}}
    result = score.pair_agreement(sample, labeled)
    assert (result["pairs"], result["primary"], result["kind"], result["old"]) == (3, 2, 2, 2)
    assert result["primary_diff"] == {("ai", "business"): 1}
