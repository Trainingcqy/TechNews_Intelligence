"""Tests for the topic and kind vocabularies."""

from __future__ import annotations

import pytest

from common.taxonomy import validate_kind, validate_topics


@pytest.mark.parametrize("topics", [["general"], ["policy", "ai", "business"]])
def test_validate_topics_returns_valid_topics_in_order(topics: list[str]) -> None:
    assert validate_topics(list(topics)) == topics


@pytest.mark.parametrize(
    "topics",
    [
        [],
        ["ai", "software", "chips", "devices"],
        ["ai", "crypto"],
        ["ai", "ai"],
        ["ai", "general"],
        ["general", "ai"],
    ],
)
def test_validate_topics_rejects(topics: list[str]) -> None:
    with pytest.raises(ValueError):
        validate_topics(topics)


def test_validate_kind() -> None:
    assert validate_kind("other") == "other"
    with pytest.raises(ValueError):
        validate_kind("blog")
