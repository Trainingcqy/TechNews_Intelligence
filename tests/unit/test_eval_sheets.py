"""Tests for the answer-sheet helpers and the story-evaluation computations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eval.common import answer_line, choice, parse_answers
from eval.stories import compute_threshold, cross_source, story_spans
from eval.stories.build_label_sheet import sample_pairs

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_parse_answers_reads_filled_blank_and_crlf_blocks() -> None:
    text = "\r\n".join([
        "# sheet", "## 1. `P001`", "", "**A** x", answer_line("同一事件").replace("[ ]", "[ Y ]"), "",
        "---", "## 2. `P002`", answer_line("同一事件"), answer_line("候选2").replace("[ ]", "[n]"),
    ])
    assert parse_answers(text) == {"P001": {"同一事件": "y"}, "P002": {"同一事件": "", "候选2": "n"}}


def test_choice_blank_and_unsure_are_excluded_and_other_values_rejected() -> None:
    assert choice("") is None and choice("?") is None and choice("y") == "y"
    with pytest.raises(ValueError):
        choice("yes")


def _threshold_input():
    # same group: bin 0.75 holds 100 pairs (labeled y and n), bin 0.85 holds 10 (both y), 20 pairs are >= 0.95.
    pairs = [
        {"pair_id": "a", "group": "same", "similarity": 0.76, "bin_lo": 0.75},
        {"pair_id": "b", "group": "same", "similarity": 0.77, "bin_lo": 0.75},
        {"pair_id": "c", "group": "same", "similarity": 0.86, "bin_lo": 0.85},
        {"pair_id": "d", "group": "same", "similarity": 0.87, "bin_lo": 0.85},
    ]
    labels = {"a": "y", "b": "n", "c": "y", "d": "y"}
    bins = [
        {"group": "same", "lo": 0.75, "hi": 0.77, "population": 100},
        {"group": "same", "lo": 0.85, "hi": 0.87, "population": 10},
        {"group": "same", "lo": 0.95, "hi": None, "population": 20},
    ]
    return pairs, labels, bins


def test_threshold_precision_recall_are_weighted_by_bin_population() -> None:
    items = compute_threshold.weighted_pairs(*_threshold_input(), "same")
    # at 0.75: predicted 50+50+5+5+20 = 130, true positives 50+5+5+20 = 80, positives 80
    assert compute_threshold.precision_recall(items, 0.75) == pytest.approx((80 / 130, 1.0))
    # at 0.85: predicted 5+5+20 = 30, all true; positives 80
    assert compute_threshold.precision_recall(items, 0.85) == pytest.approx((1.0, 30 / 80))


def test_sample_pairs_keeps_languages_apart_and_records_populations() -> None:
    pairs = [{"a": i, "b": i + 1000, "similarity": 0.751 + 0.001 * (i % 10), "group": "same" if i < 30 else "cross"} for i in range(40)]
    pairs.append({"a": 99, "b": 1099, "similarity": 0.96, "group": "same"})
    sampled, bins = sample_pairs(pairs, width=0.1, per_bin=5, cross_n=2, seed=1)
    by_group = {g: [p for p in sampled if p["group"] == g] for g in ("same", "cross")}
    assert len(by_group["same"]) == 5 and len(by_group["cross"]) == 1  # quota 1 per bin, only bin 0.75 has cross pairs
    assert all(p["bin_lo"] == 0.75 for p in sampled)
    assert {(b["group"], b["lo"]): b["population"] for b in bins if b["lo"] in (0.75, 0.95)} == {
        ("same", 0.75): 30, ("cross", 0.75): 10, ("same", 0.95): 1, ("cross", 0.95): 0}


def test_pick_stories_takes_largest_and_longest_once_each() -> None:
    def story(sid, size, days):
        return story_spans.Story(sid, size, 1, T0, T0 + timedelta(days=days))

    stories = [story(1, 9, 0), story(2, 3, 6), story(3, 9, 1), story(4, 2, 5), story(5, 1, 0)]
    assert [s.story_id for s in story_spans.pick_stories(stories, 2)] == [1, 2, 3, 4]
    assert story_spans.percentile([1, 2, 3, 4, 5], 0.5) == 3


def test_translation_score_majority_rule() -> None:
    assert "改为按语言分别计数" in cross_source.score({"T1": "y", "T2": "y", "T3": "n", "T4": None})
    assert "保持按发布方计数" in cross_source.score({"T1": "y", "T2": "n"})


def test_review_channels_need_twenty_articles_and_zero_overlap() -> None:
    rows = [("a", 25, 0), ("b", 19, 0), ("c", 30, 1)]
    assert cross_source.review_channels(rows) == ["a"]
