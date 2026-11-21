"""Jina Reader response parsing and failure classification."""

from __future__ import annotations

from ingest.dates import parse_datetime
from ingest.models import ReaderResult

_BODY_MARKER = "Markdown Content:"
_BLOCKED_BODY_CHARS = 2000  # a Warning: header on a body shorter than this means the target site blocked the fetch
_EMPTY_BODY_CHARS = 400


def _header_value(header: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in header.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
    return None


def parse_reader_response(status: int, text: str) -> ReaderResult:
    header, marker, body = text.partition(_BODY_MARKER)
    if not marker:
        header, body = "", text
    body = body.strip()
    title = _header_value(header, "Title")
    published_at = parse_datetime(_header_value(header, "Published Time"))
    warning = _header_value(header, "Warning")

    if status != 200:
        error_kind = "reader_http"
    elif warning is not None and len(body) < _BLOCKED_BODY_CHARS:
        error_kind = "reader_blocked"
    elif len(body) < _EMPTY_BODY_CHARS:
        error_kind = "reader_empty"
    else:
        error_kind = None
    return ReaderResult(error_kind is None, error_kind, title, published_at, body, warning)
