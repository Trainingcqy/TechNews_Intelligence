"""SSE event formatting for chat streaming responses."""

from __future__ import annotations

import json

from agent import Delta, Event, EvidenceFound, Progress


def sse_event(event: str, data: dict) -> str:
    """Encode one SSE event payload."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def event_to_sse(event: Event) -> str | None:
    if isinstance(event, Progress):
        return sse_event("progress", {
            "phase": event.stage,
            "title": event.title,
            "detail": event.detail,
            "status": "running",
        })
    if isinstance(event, EvidenceFound):
        return sse_event("evidence", {
            "eid": event.eid,
            "title": event.title,
            "source": event.source,
            "url": event.url,
            "created_at": event.published_at,
        })
    if isinstance(event, Delta):
        return sse_event("delta", {"text": event.text})
    return None
