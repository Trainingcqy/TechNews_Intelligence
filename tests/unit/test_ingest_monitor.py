"""Tests for the monitor's threshold rules."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ingest import monitor
from ingest.store import WindowCounts

BASE = WindowCounts(articles=100, ready=100, too_long=1, analyze_failed=2, fetched=100, degraded=4)


def _names(breaches):
    return [breach.split("：")[0] for breach in breaches]


@pytest.mark.parametrize(
    ("current", "failing", "lagging", "expected"),
    [
        (WindowCounts(unlabeled=1), [], [], [monitor.UNLABELED]),
        (WindowCounts(), ["techcrunch"], [], [monitor.FAILING_CHANNELS]),
        (WindowCounts(), [], [("openai_news", 1156.2, 6)], [monitor.DISCOVERY_LAG]),
    ],
)
def test_fixed_rules(current, failing, lagging, expected):
    assert _names(monitor.evaluate(current, [], failing, lagging)) == expected


def _day(**counts):
    # Distinct denominators: articles for the analyze rate, ready for the too-long rate, fetched for degradation.
    return WindowCounts(articles=400, ready=50, fetched=200, **counts)


@pytest.mark.parametrize(
    ("current", "baseline_day", "expected"),
    [
        # Floors 5%, 12% and 15% apply while twice the median is lower.
        (BASE, BASE, []),
        (_day(too_long=2), BASE, []),
        (_day(too_long=3), BASE, [monitor.TOO_LONG]),
        (_day(analyze_failed=48), BASE, []),
        (_day(analyze_failed=49), BASE, [monitor.ANALYZE_FAILED]),
        (_day(degraded=30), BASE, []),
        (_day(degraded=31), BASE, [monitor.DEGRADED]),
        # Twice the median applies once it exceeds the floor.
        (_day(too_long=4, analyze_failed=80, degraded=40), _day(too_long=2, analyze_failed=40, degraded=20), []),
        (
            _day(too_long=5, analyze_failed=81, degraded=41),
            _day(too_long=2, analyze_failed=40, degraded=20),
            [monitor.TOO_LONG, monitor.ANALYZE_FAILED, monitor.DEGRADED],
        ),
        (WindowCounts(articles=50, ready=50, fetched=50), BASE, []),
        (WindowCounts(articles=49, ready=49, fetched=49), BASE, [monitor.VOLUME]),
    ],
)
def test_rolling_rules(current, baseline_day, expected):
    assert _names(monitor.evaluate(current, [baseline_day] * 14, [], [])) == expected


def test_rolling_rules_use_the_median():
    low = WindowCounts(articles=100, ready=10, fetched=100)
    high = WindowCounts(articles=100, ready=100, too_long=50, fetched=100)
    baseline = [low] * 7 + [high] * 6
    assert _names(monitor.evaluate(WindowCounts(articles=6, ready=6, fetched=6), baseline, [], [])) == []
    assert _names(monitor.evaluate(WindowCounts(articles=4, ready=4, fetched=4), baseline, [], [])) == [monitor.VOLUME]
    assert _names(monitor.evaluate(WindowCounts(articles=10, ready=10, too_long=1, fetched=10), baseline, [], [])) == [
        monitor.TOO_LONG
    ]


@pytest.mark.parametrize(("days", "expected"), [(6, []), (7, [monitor.TOO_LONG, monitor.VOLUME])])
def test_rolling_rules_need_seven_days(days, expected):
    current = WindowCounts(articles=10, ready=10, too_long=10, fetched=10)
    assert _names(monitor.evaluate(current, [BASE] * days, [], [])) == expected


@pytest.mark.parametrize(
    ("now", "done_on", "expected"),
    [
        (datetime(2026, 11, 20, 0, 59, tzinfo=UTC), None, None),
        (datetime(2026, 11, 20, 1, 0, tzinfo=UTC), None, date(2026, 11, 20)),
        (datetime(2026, 11, 20, 15, 0, tzinfo=UTC), date(2026, 11, 20), None),
        (datetime(2026, 11, 20, 16, 0, tzinfo=UTC), date(2026, 11, 20), None),
        (datetime(2026, 11, 21, 1, 0, tzinfo=UTC), date(2026, 11, 20), date(2026, 11, 21)),
    ],
)
def test_due_day_is_the_first_round_after_nine_beijing(now, done_on, expected):
    assert monitor.due_day(now, done_on) == expected
