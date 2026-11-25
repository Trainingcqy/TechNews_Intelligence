"""Second-stage reranking utilities for retrieval-oriented tools."""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

import requests

from agent.trace import trace_span
from common import model_settings

logger = logging.getLogger(__name__)

RERANK_MODE_NONE = "none"
RERANK_MODE_LLM = "llm_rerank"

DEFAULT_RERANK_MODE_ENV = "NEWS_RERANK_MODE"
JINA_RERANK_URL = "https://api.jina.ai/v1/rerank"
JINA_RERANK_MODEL = model_settings.get("rerank").model


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _candidate_text(candidate: dict[str, Any]) -> str:
    title = str(candidate.get("title") or "").strip()
    summary = str(candidate.get("summary") or "").strip()
    source_type = str(candidate.get("source_type") or "").strip()
    url = str(candidate.get("url") or "").strip()

    parts: list[str] = []
    if title:
        parts.append(f"Title: {title}")
    if summary:
        parts.append(f"Summary: {summary}")
    if source_type:
        parts.append(f"Source: {source_type}")
    if url:
        parts.append(f"URL: {url}")
    return "\n".join(parts) if parts else url


def resolve_rerank_mode(mode: str | None = None) -> str:
    """Resolve effective rerank mode from the explicit arg, then NEWS_RERANK_MODE; unset means llm_rerank."""
    raw = (mode or "").strip().lower()
    if not raw:
        raw = os.getenv(DEFAULT_RERANK_MODE_ENV, "").strip().lower()

    alias = {
        "": RERANK_MODE_LLM,
        "off": RERANK_MODE_NONE,
        "disabled": RERANK_MODE_NONE,
        "disable": RERANK_MODE_NONE,
        RERANK_MODE_NONE: RERANK_MODE_NONE,
        "llm": RERANK_MODE_LLM,
        "llm-rerank": RERANK_MODE_LLM,
        RERANK_MODE_LLM: RERANK_MODE_LLM,
    }
    resolved = alias.get(raw, RERANK_MODE_NONE)
    if raw and raw not in alias:
        print(f"[Warn] unknown rerank mode '{raw}', fallback to '{RERANK_MODE_NONE}'.")
    return resolved


def _call_jina_rerank(
    *,
    query: str,
    candidates: Sequence[dict[str, Any]],
    top_k: int,
    model: str,
) -> list[dict[str, Any]]:
    jina_key = os.getenv("JINA_API_KEY", "").strip()
    if not jina_key:
        raise RuntimeError("JINA_API_KEY not set.")

    if not model:
        raise RuntimeError("rerank model is empty.")

    docs = [_candidate_text(item) for item in candidates]
    timeout_sec = _safe_float(os.getenv("JINA_RERANK_TIMEOUT_SEC", "15"), 15.0)
    timeout_sec = max(3.0, min(timeout_sec, 60.0))

    resp = requests.post(
        JINA_RERANK_URL,
        headers={
            "Authorization": f"Bearer {jina_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "query": query,
            "documents": docs,
            "top_n": top_k,
        },
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError("invalid rerank response: missing results.")

    ordered: list[dict[str, Any]] = []
    used_indexes: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        if idx in used_indexes:
            continue
        used_indexes.add(idx)
        ranked = dict(candidates[idx])
        ranked["rerank_score"] = _safe_float(item.get("relevance_score"), 0.0)
        ordered.append(ranked)
        if len(ordered) >= top_k:
            break

    if not ordered:
        raise RuntimeError("rerank response has no valid result indices.")
    return ordered


def rerank_candidates(
    query: str,
    candidates: Sequence[dict[str, Any]],
    *,
    mode: str | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Rerank candidate dictionaries and return the top k; on failure keep the given order."""
    resolved_mode = resolve_rerank_mode(mode)
    ranked = list(candidates)
    if not ranked:
        return []

    if top_k is None:
        top_k = len(ranked)
    top_k = max(1, min(int(top_k), len(ranked)))

    if resolved_mode == RERANK_MODE_NONE or len(ranked) <= 1:
        return ranked[:top_k]

    with trace_span("rerank", input={"candidates": len(ranked)},
                    attributes={"model": JINA_RERANK_MODEL, "top_k": top_k}) as span:
        try:
            reranked = _call_jina_rerank(query=query, candidates=ranked, top_k=top_k, model=JINA_RERANK_MODEL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rerank failed, keeping the fused order: %s", exc)
            span.fail(exc)
            span.warn("rerank_fallback")
            return ranked[:top_k]
        span.output = {"ranked": [
            {"article_id": getattr(item.get("article"), "article_id", None), "score": item["rerank_score"]}
            for item in reranked
        ]}
        return reranked

