"""Tests for shared URL normalization."""

from __future__ import annotations

import pytest

from common.urls import extract_urls, host_of, url_key


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://example.com/a", "example.com/a"),
        ("https://example.com:443/a", "example.com/a"),
        ("http://example.com:80/a", "example.com/a"),
        ("https://example.com:8443/a", "example.com:8443/a"),
        ("https://WWW.Example.COM/Path", "example.com/Path"),
        ("https://example.com/a/", "example.com/a"),
        ("https://example.com/", "example.com"),
        ("https://example.com/a#intro", "example.com/a"),
        (
            "https://example.com/a?id=7&utm_source=x&utm_id=y&fbclid=1&gclid=2&mc_cid=3&mc_eid=4&ref_src=5&f=rss",
            "example.com/a?id=7",
        ),
        ("https://example.com/a?b=2&a=1", "example.com/a?a=1&b=2"),
        ("  Not A URL.  ", "not a url"),
        ("HTTP://[Bad", "http://[bad"),
    ],
)
def test_url_key(url: str, expected: str) -> None:
    assert url_key(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://WWW.Example.COM:8080/a?b=1#c", "example.com"),
        ("https://developers.openai.com/x", "developers.openai.com"),
        ("not a url", ""),
    ],
)
def test_host_of(url: str, expected: str) -> None:
    assert host_of(url) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("看这个https://example.com/a，还有别的", ["https://example.com/a"]),
        ("链接https://example.com/a在这里", ["https://example.com/a"]),
        ("「https://example.com/a」和 https://example.com/a.", ["https://example.com/a"]),
    ],
)
def test_extract_urls_ends_link_at_non_ascii(text: str, expected: list[str]) -> None:
    assert extract_urls(text) == expected
