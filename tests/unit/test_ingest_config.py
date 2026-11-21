"""Tests for IngestConfig.from_env."""

from __future__ import annotations

import pytest

from ingest.config import IngestConfig


@pytest.mark.parametrize(("raw", "expected"), [("false", False), ("true", True), ("TRUE", True)])
def test_config_parses_bool(raw, expected):
    assert IngestConfig.from_env({"INGEST_ANALYZE_JSON_MODE": raw}).analyze_json_mode is expected


def test_config_rejects_unknown_bool():
    with pytest.raises(ValueError):
        IngestConfig.from_env({"INGEST_ANALYZE_JSON_MODE": "no"})
