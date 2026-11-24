"""Jina Reader JSON responses: parsing, taking the article out of the page, and the checks that it is the article
that was asked for."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from ingest.dates import parse_datetime
from ingest.extract import extract_article, is_paywalled
from ingest.models import ReaderResult

# Shorter article text means Reader got no article: a broken page, or a video page with a one-line description.
_EMPTY_BODY_CHARS = 200
_TITLE_MATCH = 0.5
_TITLE_WORD = re.compile(r"[a-z0-9]{3,}")
# Warnings in which Reader suggests a request option, and the header that applies it.
_RETRY_HINTS = (
    ("specify a timeout", "X-Timeout", "30"),
    ("shadow dom", "X-With-Shadow-Dom", "true"),
)


def titles_match(expected: str, actual: str) -> bool:
    """Whether at least half of the expected title's words appear in the page title."""
    words = set(_TITLE_WORD.findall(expected.lower()))
    if not words:
        return True
    return len(words & set(_TITLE_WORD.findall(actual.lower()))) / len(words) >= _TITLE_MATCH


def same_page(requested: str, final: str | None) -> bool:
    """Whether Reader ended on the URL it was asked for: the same host, with or without www, and the same path."""
    if not final:
        return False
    first, last = urlsplit(requested), urlsplit(final)
    host = lambda parts: parts.netloc.lower().removeprefix("www.")  # noqa: E731
    return host(first) == host(last) and first.path.rstrip("/") == last.path.rstrip("/")


def _failure(error_kind: str, title=None, published_at=None, body: str = "", warning=None) -> ReaderResult:
    return ReaderResult(False, error_kind, title, published_at, body, warning, {})


def parse_reader_response(status: int, text: str, *, url: str, expected_title: str | None) -> ReaderResult:
    """url is the article URL Reader was asked for; expected_title is the title the feed gave, or None when there is
    none to compare, as for a sitemap entry."""
    if status != 200:
        return _failure("reader_http", body=text)
    try:
        data = json.loads(text).get("data")
    except (ValueError, AttributeError):
        data = None
    if not isinstance(data, dict):
        return _failure("reader_invalid", body=text)

    content = (data.get("content") or "").strip()
    title = (data.get("title") or "").strip() or None
    published_at = parse_datetime(data.get("publishedTime"))
    warning = (data.get("warning") or "").strip() or None
    site_status = data.get("httpStatus")
    lowered = (warning or "").lower()
    retry_headers = {header: value for hint, header, value in _RETRY_HINTS if hint in lowered}
    body = extract_article(content, title or expected_title)
    # Reader can end on another page, such as a redirect target or an interstitial. On the page it was asked for, a
    # title unlike the feed's is a headline changed after publication.
    elsewhere = not same_page(url, data.get("finalUrl"))

    if isinstance(site_status, int) and site_status >= 400:
        error_kind = "reader_site_status"
    elif "captcha" in lowered:
        error_kind = "reader_blocked"
    elif len(body) < _EMPTY_BODY_CHARS:
        error_kind = "reader_empty"
    elif expected_title and title and elsewhere and not titles_match(expected_title, title):
        error_kind = "reader_title_mismatch"
    else:
        error_kind = None
    if error_kind is not None:
        return ReaderResult(False, error_kind, title, published_at, content, warning, retry_headers)
    return ReaderResult(True, None, title, published_at, body, warning, retry_headers, is_paywalled(content))
