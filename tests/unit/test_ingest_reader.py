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


_URL = "https://www.example.com/news/kagi-email/"


def _parse(text: str, expected_title: str | None = None, status: int = 200, url: str = _URL):
    return parse_reader_response(status, text, url=url, expected_title=expected_title)


def test_success_with_title_and_published_time():
    result = _parse(_load("reader_ok.json"), "How genome mapping can help researchers better understand disease")
    assert (result.ok, result.error_kind, result.retry_headers) == (True, None, {})
    assert result.title == "How genome mapping can help researchers better understand disease"
    assert result.published_at == datetime(2026, 11, 17, 22, 0, tzinfo=UTC)
    # The date and reading time above the standfirst are not article text.
    assert result.body.startswith("In our latest installment of the Ask a Scientist series")
    assert result.paywalled is False


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


@pytest.mark.parametrize(("length", "expected"), [(199, "reader_empty"), (200, None)])
def test_body_length_at_threshold(length, expected):
    assert _parse(_response("x" * length)).error_kind == expected


def test_body_length_ignores_surrounding_whitespace():
    assert _parse(_response("\n\n" + "x" * 150 + "\n\n\n" + " " * 300)).error_kind == "reader_empty"


_MENU = "\n".join(f"* {item}" for item in ("Products", "Research", "Safety", "Company", "Careers", "Pricing", "Docs",
                                         "Developers", "Business", "Enterprise", "Education", "Stories", "Podcast",
                                         "Livestreams", "Help center", "Status", "Terms of use", "Privacy policy"))
_ARTICLE = ("Example Labs released Model X on Tuesday, a language model that reads documents of up to two million "
            "words and answers questions about them in a few seconds, the company said.\n\n"
            "Early customers include three law firms and a hospital network in Ohio that tested the model for six "
            "months, and pricing starts at four dollars per million words read.")


_TEASERS = (
    "A chip designer in Austin said its newest accelerator uses half the power of the previous generation, and that "
    "two large hosting companies have agreed to test it in their data centers next spring.",
    "A startup in Berlin raised forty million dollars to build robots that sort recycling at municipal plants, "
    "according to people familiar with the deal and filings seen by reporters this week.",
    "Regulators in Brussels opened an inquiry into how three cloud providers price storage for public agencies, the "
    "commission said, adding that the review would take at least nine months to complete.",
    "Shares of a satellite operator rose after it won a contract to provide internet service to rural schools, a deal "
    "that analysts said could be worth several hundred million dollars over the next decade.",
)


@pytest.mark.parametrize(
    ("page_title", "expected_title"),
    [("Example Labs ships Model X | Example Blog", None), ("", "Example Labs ships Model X")],
)
def test_body_is_the_article_text_of_the_page(page_title, expected_title):
    # The heading that repeats the title, from the page or else from the feed, marks where the article starts; after
    # it, "Related" ends the article although more prose follows.
    teasers = "\n\n".join(_TEASERS)
    content = f"{_MENU}\n\n# Example Labs ships Model X\n\nNovember 20, 2026\n\n{_ARTICLE}\n\n## Related\n\n{teasers}"
    result = _parse(_response(content, title=page_title), expected_title)
    assert (result.ok, result.body) == (True, _ARTICLE)


def test_length_check_applies_to_the_article_text():
    content = f"{_MENU}\n\nWatch a walkthrough of the new feature in this short video.\n\n{_MENU}"
    result = _parse(_response(content))
    assert (result.ok, result.error_kind) == (False, "reader_empty")
    # The failure keeps Reader's whole content for the failure detail.
    assert result.body == content


def test_free_part_of_a_paywalled_article_is_flagged():
    content = f"{_ARTICLE}\n\n## This post is for paid members only\n\nBecome a paid member for unlimited access."
    result = _parse(_response(content))
    assert (result.ok, result.paywalled, result.body) == (True, True, _ARTICLE)


def test_page_title_that_does_not_match_the_feed_title_is_a_mismatch():
    page = _response(title="Best gifts under $50 for 2026")
    assert _parse(page, "Google alternative Kagi launches an email service").error_kind == "reader_title_mismatch"
    # No title to compare on either side: the check does not apply.
    assert _parse(page, None).ok is True
    assert _parse(_response(title=""), "Google alternative Kagi launches an email service").ok is True


@pytest.mark.parametrize(
    ("final_url", "error_kind"),
    [
        # Reader stayed on the requested page: a headline changed after publication is no mismatch.
        (_URL, None),
        ("https://EXAMPLE.com/news/kagi-email", None),
        ("https://www.example.com/news/kagi-email/?utm_source=rss", None),
        # Reader ended elsewhere, or did not say where: the titles must match.
        ("https://www.example.com/plus/learnmore", "reader_title_mismatch"),
        ("https://www.example.org/news/kagi-email/", "reader_title_mismatch"),
        ("https://cm.adform.net/cookie?redirect_url=https%3A%2F%2Fexample.com", "reader_title_mismatch"),
        (None, "reader_title_mismatch"),
    ],
)
def test_title_check_applies_when_reader_ended_on_another_page(final_url, error_kind):
    data = {"title": "Could a new inbox change how people pay for search?"}
    if final_url is not None:
        data["finalUrl"] = final_url
    result = _parse(_response(**data), "Google alternative Kagi launches an email service")
    assert result.error_kind == error_kind


def test_redirect_to_a_page_with_the_same_title_is_fine():
    page = _response(title="Kagi launches an email service | Example Blog", finalUrl="https://blog.example.org/kagi")
    assert _parse(page, "Kagi launches an email service").ok is True


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
