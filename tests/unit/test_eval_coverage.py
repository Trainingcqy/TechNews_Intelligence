"""Tests for the coverage ranking rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from eval.coverage import techmeme_coverage as tc

DAY = date(2026, 11, 10)
T0 = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def _story(sid, pubs, hn, hours):
    at = T0 + timedelta(hours=hours)
    return tc.Story(sid, pubs, hn, None, at, at, frozenset({DAY}))


def _order(rule, stories):
    return [s.story_id for s in sorted(stories, key=tc.RULES[rule](stories))]


def test_ranking_rules_order_stories_differently() -> None:
    stories = [_story(1, 1, 100, 3), _story(2, 2, None, 2), _story(3, 2, 50, 1)]
    assert _order("three_level", stories) == [3, 2, 1]
    assert _order("hn_first", stories) == [1, 3, 2]
    # weighted 0.5: story 1 = 0.25 + 0.5 = 0.75, story 3 = 0.5 + 0.25 = 0.75 (tie, newer first), story 2 = 0.5
    assert _order("weighted_0.5", stories) == [1, 3, 2]


def test_day_rank_is_none_when_the_story_has_no_member_that_day() -> None:
    other_day = tc.Story(9, 5, 500, None, T0, T0, frozenset({date(2026, 11, 11)}))
    stories = [_story(1, 1, None, 0), other_day]
    assert tc.day_rank(stories, 1, DAY, "three_level") == 1
    assert tc.day_rank(stories, 9, DAY, "three_level") is None


def test_rank_table_counts_top_k_and_missing_outlets() -> None:
    stories = [_story(i, 1, None, i) for i in range(1, 31)]  # same signals, newest first: story 30 is rank 1
    headlines = [
        tc.Headline(1, "a", "X", "k1", T0, 10, "ready", 30),
        tc.Headline(2, "b", "Y", "k2", T0, 11, "ready", 1),   # rank 30
        tc.Headline(3, "c", "Y", "k3", T0, None, None, None),
        tc.Headline(4, "d", None, None, T0, None, None, None),
    ]
    summary, rows = tc.rank_table([h for h in headlines if h.article_id], stories)
    assert summary["three_level"] == {"top": 1, "n": 2} and len(rows) == 2
    assert tc.missing_outlets(headlines) == [("Y", 1), ("(unknown)", 1)]


def test_event_hit_rate_counts_a_headline_once_and_skips_unanswered() -> None:
    answers = {"H1": {"候选1": "n", "候选2": "y", "候选3": "y"}, "H2": {"候选1": "n", "候选2": None}, "H3": {"候选1": None}}
    assert tc.event_hit_rate(answers) == (1, 2)
