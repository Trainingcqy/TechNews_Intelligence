"""Tests for the taxonomy-trial statistics."""

from __future__ import annotations

import pytest

from eval.taxonomy import score


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
