"""Tests for publisher resolution."""

from __future__ import annotations

import pytest

from ingest.sources import Publisher, parse_sources, resolve_publisher

_SOURCES = parse_sources(
    """
[[publisher]]
name = "OpenAI"
kind = "official"
hosts = ["openai.com"]

[[publisher]]
name = "Amazon"
kind = "official"
hosts = ["amazon.com"]

[[publisher]]
name = "AWS"
kind = "official"
hosts = ["aws.amazon.com"]

[[publisher]]
name = "Hugging Face"
kind = "official"
hosts = ["huggingface.co/blog"]

[[publisher]]
name = "Hugging Face Hub"
kind = "code_host"
hosts = ["huggingface.co"]

[[publisher]]
name = "月之暗面"
kind = "official"
hosts = ["huggingface.co/moonshotai"]

[[publisher]]
name = "Example"
kind = "official"
hosts = ["example.org/a"]

[[publisher]]
name = "Example Models"
kind = "official"
hosts = ["example.org/a/models"]

[[publisher]]
name = "Top-level label"
kind = "unknown"
hosts = ["org"]
"""
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.openai.com/index/x", "OpenAI"),
        ("https://developers.openai.com/api/docs", "OpenAI"),
        ("https://a.b.developers.openai.com/x", "OpenAI"),
        ("https://aws.amazon.com/blogs/aws/x", "AWS"),
        ("https://smile.amazon.com/x", "Amazon"),
        ("https://huggingface.co/blog/some-post", "Hugging Face"),
        ("https://huggingface.co/Blog", "Hugging Face"),
        ("https://huggingface.co/moonshotai/Kimi-K2", "月之暗面"),
        ("https://huggingface.co/MoonshotAI/Kimi-K2", "月之暗面"),
        ("https://huggingface.co/blogger/model", "Hugging Face Hub"),
        ("https://huggingface.co/", "Hugging Face Hub"),
        ("https://example.org/a/models/x", "Example Models"),
        ("https://example.org/a/modelsx", "Example"),
    ],
)
def test_resolve_registered_publisher(url: str, expected: str) -> None:
    assert resolve_publisher(url, _SOURCES.hosts).name == expected


def test_unmatched_host_yields_auto_publisher() -> None:
    assert resolve_publisher("https://www.Blog.Example.com/post", _SOURCES.hosts) == Publisher(
        name="blog.example.com", kind="unknown", hosts=(("blog.example.com", ""),)
    )


def test_parent_domain_matches_path_prefix() -> None:
    assert resolve_publisher("https://sub.example.org/a/x", _SOURCES.hosts).name == "Example"
    assert resolve_publisher("https://sub.example.org/b", _SOURCES.hosts).name == "sub.example.org"
    assert resolve_publisher("https://example.org/b", _SOURCES.hosts).name == "example.org"


def test_resolve_rejects_url_without_host() -> None:
    with pytest.raises(ValueError):
        resolve_publisher("not a url", _SOURCES.hosts)
