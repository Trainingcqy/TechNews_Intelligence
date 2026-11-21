"""Tests for ingest publication-time parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ingest.dates import parse_datetime

NOW = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Tue, 17 Nov 2026 18:00:16 GMT", datetime(2026, 11, 17, 18, 0, 16, tzinfo=UTC)),
        ("Tue, 17 Nov 2026 18:00:16 +0800", datetime(2026, 11, 17, 10, 0, 16, tzinfo=UTC)),
        ("2026-11-17T22:00:00+00:00", datetime(2026, 11, 17, 22, 0, tzinfo=UTC)),
        ("2026-11-17T22:00:00Z", datetime(2026, 11, 17, 22, 0, tzinfo=UTC)),
        ("2026-11-17T12:00:00.000-05:00", datetime(2026, 11, 17, 17, 0, tzinfo=UTC)),
        # 36Kr pubDate: two spaces before the offset
        ("2026-11-17 12:31:40  +0800", datetime(2026, 11, 17, 4, 31, 40, tzinfo=UTC)),
        # no offset: taken as UTC
        ("2026-11-17T08:00:00", datetime(2026, 11, 17, 8, 0, tzinfo=UTC)),
        ("  2026-11-17T08:00:00Z  ", datetime(2026, 11, 17, 8, 0, tzinfo=UTC)),
    ],
)
def test_parses_formats_to_utc(value, expected):
    assert parse_datetime(value, now=NOW) == expected


def test_result_is_aware_utc():
    parsed = parse_datetime("2026-11-17 12:31:40  +0800", now=NOW)
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("value", ["Wed, 18 Nov 2026 06:00:00 GMT", "2026-12-01T00:00:00Z", "2026-11-18 12:00:00  +0800"])
def test_future_values_are_clamped_to_now(value):
    assert parse_datetime(value, now=NOW) == NOW


def test_value_equal_to_now_is_kept():
    assert parse_datetime("2026-11-18T00:00:00Z", now=NOW) == NOW


@pytest.mark.parametrize("value", [None, "", "   ", "not a date", "17 Nov", "2026-13-45T00:00:00Z"])
def test_unparseable_returns_none(value):
    assert parse_datetime(value, now=NOW) is None


def test_now_defaults_to_current_time():
    assert parse_datetime("2999-01-01T00:00:00Z") <= datetime.now(UTC)
