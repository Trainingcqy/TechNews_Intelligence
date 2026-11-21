"""Tests for the topic and kind vocabularies."""

from __future__ import annotations

import pytest

from services.taxonomy import LEGACY_CATEGORY_MAP, validate_kind, validate_topics


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


def test_legacy_category_map_covers_all_old_labels() -> None:
    assert LEGACY_CATEGORY_MAP == {
        "AI": ("ai",),
        "安全": ("security",),
        "开发": ("software",),
        "商业": ("business",),
        "硬件": (),
        "生态": (),
    }
