"""SSE event formatting for chat streaming responses."""

from __future__ import annotations

import json

from agent import Progress


def sse_event(event: str, data: dict) -> str:
    """Encode one SSE event payload."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def event_to_sse(event: Progress) -> str:
    return sse_event("progress", {"title": event.title, "detail": event.detail})
