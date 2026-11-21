"""Publication-time parsing for feed entries and Reader headers."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

# 36Kr pubDate: "2026-11-17 12:31:40  +0800"
_EXTRA_FORMATS = ("%Y-%m-%d %H:%M:%S  %z",)


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
    """Return an aware UTC datetime, or None if unparseable. Naive input is taken as UTC; future values are clamped to now."""
    text = (value or "").strip()
    if not text:
        return None
    parsed = _parse_raw(text)
    if parsed is None:
        return None
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    current = now or datetime.now(UTC)
    return min(parsed, current)
