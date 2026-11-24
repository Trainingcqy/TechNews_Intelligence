"""Tests for the fetch stage: body cleaning, the placeholder title, retries and what is recorded."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from ingest import fetch, http, store
from ingest.config import IngestConfig
from ingest.discover.sitemap import placeholder_title
from ingest.fetch import clean_body


def test_clean_body_removes_images_and_keeps_link_text():
    text = "x ![alt](https://i.example/p.png) y\nsee [one](https://a.example/1) and [two](http://b.example/2).\x00"
    assert clean_body(text) == "x  y\nsee one and two."


def test_placeholder_title_is_last_path_segment():
    assert placeholder_title("https://www.anthropic.com/news/claude-5/") == "claude-5"
    assert placeholder_title("https://www.anthropic.com/news/claude-5?ref=x") == "claude-5"


def _reader(content: str = "x" * 500, **data) -> http.HttpResponse:
    payload = {"code": 200, "data": {"title": "Kagi launches an email service", "content": content, "httpStatus": 200, **data}}
    return http.HttpResponse(status=200, etag=None, last_modified=None, content=json.dumps(payload).encode())


@pytest.fixture
def recorded(monkeypatch):
    calls = {"requests": [], "success": [], "failure": []}
    responses: list[http.HttpResponse] = []

    def get(url, *, user_agent, timeout, headers):
        calls["requests"].append(headers)
        return responses.pop(0)

    monkeypatch.setattr(fetch.http, "get", get)
    monkeypatch.setattr(store, "record_fetch_success", lambda cur, article_id, **kw: calls["success"].append(kw))
    monkeypatch.setattr(store, "record_failure", lambda cur, *args: calls["failure"].append(args))
    calls["responses"] = responses
    return calls


@contextmanager
def _transaction():
    yield None


def _run(item: store.FetchItem) -> None:
    fetch.fetch_article(item, transaction=_transaction, config=IngestConfig())


def test_reader_is_asked_for_json_and_the_suggested_option_is_tried_once(recorded):
    recorded["responses"] += [
        _reader("partial page " * 40, warning="This page maybe not yet fully loaded, consider explicitly specify a timeout."),
        _reader("full article " * 80),
    ]
    _run(store.FetchItem(7, "https://www.theverge.com/kagi", "Kagi launches an email service"))
    first, second = recorded["requests"]
    assert first["Accept"] == "application/json" and "X-Timeout" not in first
    assert second["X-Timeout"] == "30"
    (success,) = recorded["success"]
    assert success["body"].startswith("full article") and success["title"] is None


def test_sitemap_entry_skips_the_title_check_and_takes_the_page_title(recorded):
    url = "https://api-docs.deepseek.com/news/news1120/"
    recorded["responses"].append(_reader(title="DeepSeek-V4 Release"))
    _run(store.FetchItem(7, url, placeholder_title(url)))
    assert recorded["success"][0]["title"] == "DeepSeek-V4 Release"
    assert recorded["failure"] == []


def test_page_with_another_title_is_recorded_as_a_mismatch(recorded):
    recorded["responses"].append(_reader(title="Best gifts under $50 for 2026"))
    _run(store.FetchItem(7, "https://www.theverge.com/kagi", "Google alternative Kagi launches an email service"))
    assert recorded["success"] == []
    ((article_id, stage, error_kind, detail, _config),) = recorded["failure"]
    assert (article_id, stage, error_kind) == (7, "fetch", "reader_title_mismatch")
    assert "expected title: Google alternative Kagi launches an email service" in detail
    assert "page title: Best gifts under $50 for 2026" in detail
