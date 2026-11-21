"""Tests for the embed stage's response parsing."""

from __future__ import annotations

import json

import pytest

from ingest.embed import ShapeMismatch, parse_embeddings


def test_parse_embeddings_orders_by_index():
    content = json.dumps({"data": [{"index": 1, "embedding": [1.0] * 1024}, {"index": 0, "embedding": [0.0] * 1024}]})
    assert parse_embeddings(content.encode(), 2) == [[0.0] * 1024, [1.0] * 1024]


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [0.0] * 1024}],
        [{"index": 0, "embedding": [0.0] * 1024}, {"index": 0, "embedding": [0.0] * 1024}],
        [{"index": 0, "embedding": [0.0] * 1024}, {"index": 1, "embedding": [0.0] * 512}],
    ],
)
def test_parse_embeddings_rejects_shape_mismatch(data):
    with pytest.raises(ShapeMismatch):
        parse_embeddings(json.dumps({"data": data}).encode(), 2)
