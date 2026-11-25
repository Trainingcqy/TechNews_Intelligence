"""Tests for IngestConfig.from_env."""

from __future__ import annotations

from ingest.config import IngestConfig


def test_config_reads_ints_and_the_backoff_table():
    config = IngestConfig.from_env({"INGEST_FETCH_WORKERS": "5", "INGEST_BACKOFF_MINUTES": "1,2"})
    assert (config.fetch_workers, config.backoff_minutes) == (5, (1, 2))
