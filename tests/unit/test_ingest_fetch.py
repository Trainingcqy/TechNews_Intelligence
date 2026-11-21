"""Tests for body cleaning and the placeholder title used by the fetch stage."""

from __future__ import annotations

from ingest.discover.sitemap import placeholder_title
from ingest.fetch import clean_body


def test_clean_body_removes_images_and_keeps_link_text():
    text = "x ![alt](https://i.example/p.png) y\nsee [one](https://a.example/1) and [two](http://b.example/2).\x00"
    assert clean_body(text) == "x  y\nsee one and two."


def test_placeholder_title_is_last_path_segment():
    assert placeholder_title("https://www.anthropic.com/news/claude-5/") == "claude-5"
    assert placeholder_title("https://www.anthropic.com/news/claude-5?ref=x") == "claude-5"
