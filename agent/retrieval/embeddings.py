"""Embedding helpers shared by retrieval modules."""

from __future__ import annotations

import logging
import os

import requests

from agent.trace import trace_span
from common import model_settings

logger = logging.getLogger(__name__)

JINA_EMBED_URL = "https://api.jina.ai/v1/embeddings"
JINA_MODEL = model_settings.get("embedding").model


def get_query_embedding(query: str) -> list[float] | None:
    """Get the query embedding from Jina. Return None on failure; semantic recall is then skipped."""
    with trace_span("embedding", attributes={"model": JINA_MODEL}) as span:
        jina_key = os.getenv("JINA_API_KEY", "")
        if not jina_key:
            logger.warning("JINA_API_KEY not set, semantic recall skipped")
            span.fail(code="embedding_no_api_key", message="JINA_API_KEY not set")
            span.warn("semantic_skipped")
            return None
        try:
            resp = requests.post(
                JINA_EMBED_URL,
                headers={
                    "Authorization": f"Bearer {jina_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": JINA_MODEL,
                    "task": "retrieval.query",
                    "input": [query],
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding request failed, semantic recall skipped: %s", exc)
            span.fail(exc)
            span.warn("semantic_skipped")
            return None
