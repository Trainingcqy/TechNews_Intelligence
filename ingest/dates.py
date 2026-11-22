"""Publication-time parsing for feed entries and Reader headers."""

from __future__ import annotations

from datetime import UTC, datetime
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


def parse_datetime(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Return an aware UTC datetime, or None if unparseable or a placeholder. Naive input is taken as UTC; future values
    are clamped to now."""
    text = (value or "").strip()
    if not text:
        return None
    parsed = _parse_raw(text)
    if parsed is None:
        return None
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    if parsed in _PLACEHOLDERS:
        return None
    current = now or datetime.now(UTC)
    return min(parsed, current)
