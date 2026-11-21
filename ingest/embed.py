"""embed stage: summary vectors from the Jina embeddings API."""

from __future__ import annotations

import json
import logging
import os

from ingest import http, store
from ingest.config import IngestConfig

logger = logging.getLogger(__name__)

EMBEDDINGS_URL = "https://api.jina.ai/v1/embeddings"
MODEL = "jina-embeddings-v3"
DIMENSIONS = 1024
_DETAIL_BODY_CHARS = 500


class ShapeMismatch(Exception):
    pass


class _BatchFailure(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def parse_embeddings(content: bytes, count: int) -> list[list[float]]:
    """Vectors in input order, matched by the index field of each returned item."""
    data = json.loads(content)["data"]
    if len(data) != count or sorted(item["index"] for item in data) != list(range(count)):
        raise ShapeMismatch(f"expected indexes 0..{count - 1}, got {[item['index'] for item in data]}")
    vectors: list[list[float]] = [[] for _ in range(count)]
    for item in data:
        if len(item["embedding"]) != DIMENSIONS:
            raise ShapeMismatch(f"expected {DIMENSIONS} dimensions, got {len(item['embedding'])} at index {item['index']}")
        vectors[item["index"]] = item["embedding"]
    return vectors


def _request_vectors(items: list[store.EmbedItem], config: IngestConfig, api_key: str) -> list[list[float]]:
    body = {
        "model": MODEL,
        "task": "retrieval.passage",
        "dimensions": DIMENSIONS,
        "embedding_type": "float",
        "input": [item.summary for item in items],
    }
    try:
        response = http.post(
            EMBEDDINGS_URL,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=config.embed_timeout,
        )
    except http.TransportError as exc:
        raise _BatchFailure(exc.kind, str(exc)) from exc
    if response.status != 200:
        text = response.content.decode("utf-8", errors="replace")[:_DETAIL_BODY_CHARS]
        raise _BatchFailure("embed_http", f"status: {response.status}\n{text}")
    try:
        return parse_embeddings(response.content, len(items))
    except ShapeMismatch as exc:
        raise _BatchFailure("embed_shape", str(exc)) from exc


def embed_batch(
    items: list[store.EmbedItem], *, transaction: store.Transaction, config: IngestConfig, api_key: str
) -> None:
    try:
        vectors = _request_vectors(items, config, api_key)
    except _BatchFailure as exc:
        kind, detail = exc.kind, exc.detail
    except Exception as exc:
        logger.exception("embed batch starting at %s failed", items[0].article_id)
        kind, detail = "unexpected", f"{type(exc).__name__}: {exc}"
    else:
        for item, vector in zip(items, vectors):
            try:
                with transaction() as cur:
                    store.record_embed_success(cur, item.article_id, vector, MODEL)
            except Exception as exc:
                logger.exception("embed %s: storing the vector failed", item.article_id)
                with transaction() as cur:
                    store.record_failure(
                        cur, item.article_id, "embed", "unexpected", f"{type(exc).__name__}: {exc}", config
                    )
        return
    for item in items:
        with transaction() as cur:
            store.record_failure(cur, item.article_id, "embed", kind, detail, config)


def run_embed(config: IngestConfig, *, transaction: store.Transaction) -> None:
    api_key = os.getenv("JINA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("JINA_API_KEY is required for the embed stage.")
    with transaction() as cur:
        items = store.take_embed(cur)
    for start in range(0, len(items), config.embed_batch_size):
        embed_batch(items[start : start + config.embed_batch_size], transaction=transaction, config=config, api_key=api_key)
