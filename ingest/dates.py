"""Publication-time parsing for feed entries and Reader headers."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from email.utils import parsedate_to_datetime

# 36Kr pubDate: "2026-11-17 12:31:40  +0800"
_EXTRA_FORMATS = ("%Y-%m-%d %H:%M:%S  %z",)

# What sites emit for an empty date field: the Go and .NET zero time, the Unix epoch, the DOS and ZIP epoch.
_PLACEHOLDERS = frozenset(datetime(year, 1, 1, tzinfo=UTC) for year in (1, 1970, 1980))


def _parse_raw(value: str) -> datetime | None:
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in _EXTRA_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def parse_datetime(value: str | None, *, now: datetime | None = None, zone: tzinfo | None = None) -> datetime | None:
    """Return an aware UTC datetime, or None if unparseable or a placeholder. Naive input is taken as UTC; future values
    are clamped to now. zone is for a source whose clock times are in another zone than the one it states: the clock
    time is read in zone, whatever offset the value carries."""
    text = (value or "").strip()
    if not text:
        return None
    parsed = _parse_raw(text)
    if parsed is None:
        return None
    as_written = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    if as_written in _PLACEHOLDERS:
        return None
    result = as_written if zone is None else parsed.replace(tzinfo=zone).astimezone(UTC)
    current = now or datetime.now(UTC)
    return min(result, current)
