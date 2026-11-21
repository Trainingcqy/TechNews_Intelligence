"""Tests for Jina Reader response parsing, using recorded responses (trimmed)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ingest.reader import parse_reader_response

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _response(body: str, *, title: str = "T", warning: str | None = None, published: str | None = None) -> str:
    lines = [f"Title: {title}", "", "URL Source: https://example.com/a", ""]
    if published:
        lines += [f"Published Time: {published}", ""]
    if warning:
        lines += [f"Warning: {warning}", ""]
    return "\n".join(lines) + f"\nMarkdown Content:\n{body}"


def test_success_with_title_and_published_time():
    result = parse_reader_response(200, _load("reader_ok.txt"))
    assert result.ok is True
    assert result.error_kind is None
    assert result.title == "How genome mapping can help researchers better understand disease"
    assert result.published_at == datetime(2026, 11, 17, 22, 0, tzinfo=UTC)
    assert result.body.startswith("Nov 17, 2026")
    assert "Title:" not in result.body and "URL Source:" not in result.body


def test_published_time_in_rfc2822_form():
    published = "Tue, 17 Nov 2026 18:00:16 GMT"
    result = parse_reader_response(200, _response("x" * 500, published=published))
    assert result.published_at == datetime(2026, 11, 17, 18, 0, 16, tzinfo=UTC)


def test_missing_or_unparseable_published_time_is_none():
    assert parse_reader_response(200, _response("x" * 500)).published_at is None
    assert parse_reader_response(200, _response("x" * 500, published="soon")).published_at is None


def test_body_text_is_not_mistaken_for_header_fields():
    body = "Title: not the title\nPublished Time: 2020-01-01T00:00:00Z\nWarning: not a warning\n" + "x" * 500
    result = parse_reader_response(200, _response(body, title="Real"))
    assert result.ok is True
    assert result.title == "Real"
    assert result.published_at is None
    assert result.warning is None
    assert result.body.startswith("Title: not the title")


def test_non_200_is_reader_http_even_with_long_body():
    result = parse_reader_response(403, _response("x" * 3000))
    assert (result.ok, result.error_kind) == (False, "reader_http")
    assert parse_reader_response(429, "").error_kind == "reader_http"


def test_warning_with_short_body_is_blocked():
    result = parse_reader_response(200, _load("reader_blocked.txt"))
    assert (result.ok, result.error_kind) == (False, "reader_blocked")
    assert result.title == "Reddit - Prove your humanity"
    assert result.warning == (
        "This page maybe requiring CAPTCHA, please make sure you are authorized to access this page."
    )
    assert result.body.startswith("# Prove your humanity")


def test_blocked_takes_precedence_over_empty():
    result = parse_reader_response(200, _response("x" * 100, warning="This page maybe requiring CAPTCHA"))
    assert result.error_kind == "reader_blocked"


@pytest.mark.parametrize(("length", "expected"), [(1999, "reader_blocked"), (2000, None)])
def test_warning_with_body_length_at_threshold(length, expected):
    result = parse_reader_response(200, _response("x" * length, warning="This is a cached snapshot"))
    assert result.error_kind == expected


@pytest.mark.parametrize(("length", "expected"), [(399, "reader_empty"), (400, None)])
def test_body_length_at_threshold(length, expected):
    assert parse_reader_response(200, _response("x" * length)).error_kind == expected


def test_empty_body_is_reader_empty():
    result = parse_reader_response(200, "Title: T\n\nURL Source: https://example.com/a\n\nMarkdown Content:\n")
    assert (result.ok, result.error_kind, result.body) == (False, "reader_empty", "")


def test_body_length_ignores_surrounding_whitespace():
    assert parse_reader_response(200, _response("\n\n" + "x" * 300 + "\n\n\n" + " " * 300)).error_kind == "reader_empty"


def test_response_without_marker_uses_whole_text_as_body():
    result = parse_reader_response(200, "y" * 500)
    assert result.ok is True
    assert result.title is None
    assert result.body == "y" * 500


def test_cloudflare_in_body_is_not_a_failure():
    result = parse_reader_response(200, _load("reader_cloudflare.txt"))
    assert result.ok is True
    assert result.error_kind is None
    assert "cloudflare" in result.body.lower()
    assert result.title.startswith("Announcing Cloudflare Wallets")
