"""Tests for Jina Reader response parsing, using recorded JSON responses (trimmed)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ingest.reader import parse_reader_response, titles_match

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _response(content: str = "x" * 500, **data) -> str:
    return json.dumps({"code": 200, "status": 20000, "data": {"title": "T", "content": content, "httpStatus": 200, **data}})


def _parse(text: str, expected_title: str | None = None, status: int = 200):
    return parse_reader_response(status, text, expected_title=expected_title)


def test_success_with_title_and_published_time():
    result = _parse(_load("reader_ok.json"), "How genome mapping can help researchers better understand disease")
    assert (result.ok, result.error_kind, result.retry_headers) == (True, None, {})
    assert result.title == "How genome mapping can help researchers better understand disease"
    assert result.published_at == datetime(2026, 11, 17, 22, 0, tzinfo=UTC)
    assert result.body.startswith("Nov 17, 2026")


def test_published_time_in_rfc2822_form():
    result = _parse(_response(publishedTime="Tue, 17 Nov 2026 18:00:16 GMT"))
    assert result.published_at == datetime(2026, 11, 17, 18, 0, 16, tzinfo=UTC)


def test_missing_or_unparseable_published_time_is_none():
    assert _parse(_response()).published_at is None
    assert _parse(_response(publishedTime="soon")).published_at is None


def test_non_200_is_reader_http_even_with_a_body():
    result = _parse(_response(), status=422)
    assert (result.ok, result.error_kind) == (False, "reader_http")
    assert _parse("", status=429).error_kind == "reader_http"


@pytest.mark.parametrize("text", ["not json", "[]", '{"code": 200}', '{"data": "x"}'])
def test_response_without_a_data_object_is_reader_invalid(text):
    assert _parse(text).error_kind == "reader_invalid"


def test_captcha_warning_is_blocked():
    result = _parse(_load("reader_blocked.json"), "Interview question about system design")
    assert (result.ok, result.error_kind) == (False, "reader_blocked")
    assert result.title == "Reddit - Prove your humanity"
    assert result.body.startswith("# Prove your humanity")


def test_site_error_status_fails_even_with_a_long_body():
    result = _parse(_response("x" * 3000, httpStatus=404, warning="Target URL returned error 404: Not Found"))
    assert (result.ok, result.error_kind) == (False, "reader_site_status")
    assert _parse(_response(httpStatus=400)).error_kind == "reader_site_status"
    assert _parse(_response(httpStatus=399)).ok is True


def test_other_warnings_do_not_fail_the_result():
    assert _parse(_response(warning="This is a cached snapshot")).ok is True


@pytest.mark.parametrize(("length", "expected"), [(399, "reader_empty"), (400, None)])
def test_body_length_at_threshold(length, expected):
    assert _parse(_response("x" * length)).error_kind == expected


def test_body_length_ignores_surrounding_whitespace():
    assert _parse(_response("\n\n" + "x" * 300 + "\n\n\n" + " " * 300)).error_kind == "reader_empty"


def test_page_title_that_does_not_match_the_feed_title_is_a_mismatch():
    page = _response(title="Best gifts under $50 for 2026")
    assert _parse(page, "Google alternative Kagi launches an email service").error_kind == "reader_title_mismatch"
    # No title to compare on either side: the check does not apply.
    assert _parse(page, None).ok is True
    assert _parse(_response(title=""), "Google alternative Kagi launches an email service").ok is True


@pytest.mark.parametrize(
    ("expected", "actual", "match"),
    [
        ("Kagi launches an email service", "Kagi launches an email service | The Verge", True),
        ("Trump says FBI hack is ‘not good’", "Trump says FBI hack is 'not good'", True),
        # Two of the four words are half: enough.
        ("Alpha beta gamma delta", "alpha beta other words", True),
        ("Alpha beta gamma delta", "alpha other words here", False),
        ("4 cybersecurity steps every organization should take", "Security fundamentals to enable the frontier", False),
        ("A B", "anything", True),
    ],
)
def test_titles_match(expected, actual, match):
    assert titles_match(expected, actual) is match


@pytest.mark.parametrize(
    ("warning", "headers"),
    [
        ("This page maybe not yet fully loaded, consider explicitly specify a timeout.", {"X-Timeout": "30"}),
        ("This page contains shadow DOM that are currently hidden, consider enabling shadow DOM processing.",
         {"X-With-Shadow-Dom": "true"}),
        ("This is a cached snapshot", {}),
    ],
)
def test_warning_suggestions_become_retry_headers(warning, headers):
    assert _parse(_response(warning=warning)).retry_headers == headers


def test_cloudflare_in_body_is_not_a_failure():
    result = _parse(_load("reader_cloudflare.json"), "Announcing Cloudflare Wallets")
    assert result.ok is True
    assert "cloudflare" in result.body.lower()
    assert result.title.startswith("Announcing Cloudflare Wallets")
