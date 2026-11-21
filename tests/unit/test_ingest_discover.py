"""Tests for the candidate filter of the discover stage."""

from __future__ import annotations

from datetime import UTC, datetime

from ingest.discover import keep_candidate
from ingest.models import Candidate

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def _candidate(url: str) -> Candidate:
    return Candidate(url, "t", None, None, None, None, None, None)


def test_max_path_segments():
    config = {"max_path_segments": 2}
    assert keep_candidate(_candidate("https://huggingface.co/blog/smolvlm"), config, now=NOW)
    assert keep_candidate(_candidate("https://huggingface.co/blog/smolvlm/"), config, now=NOW)
    assert not keep_candidate(_candidate("https://huggingface.co/blog/someuser/my-post"), config, now=NOW)


def test_no_limit_without_config():
    assert keep_candidate(_candidate("https://huggingface.co/blog/someuser/my-post"), {}, now=NOW)
