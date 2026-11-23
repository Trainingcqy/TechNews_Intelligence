"""Writes request traces to agent_runs, agent_trace_spans and agent_model_io."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from psycopg2.extras import Json

from common.db import db_transaction

if TYPE_CHECKING:
    from agent.trace import RequestTrace, Span

_INSERT_RUNNING = """
    INSERT INTO public.agent_runs (request_id, thread_id, channel, user_message, status, started_at)
    VALUES (%s, %s, %s, %s, 'running', %s)
    ON CONFLICT (request_id) DO NOTHING
"""

_UPSERT_RUN = """
    INSERT INTO public.agent_runs (
        request_id, thread_id, channel, user_message, status, error_code, error_message, exception,
        started_at, latency_ms, token_usage, warnings, output
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (request_id) DO UPDATE SET
        status = EXCLUDED.status,
        error_code = EXCLUDED.error_code,
        error_message = EXCLUDED.error_message,
        exception = EXCLUDED.exception,
        latency_ms = EXCLUDED.latency_ms,
        token_usage = EXCLUDED.token_usage,
        warnings = EXCLUDED.warnings,
        output = EXCLUDED.output
"""

_INSERT_SPAN = """
    INSERT INTO public.agent_trace_spans (
        request_id, span_id, parent_span_id, kind, name, status, started_at_ms, finished_at_ms,
        input, output, attributes, warnings, error_code, error_message, exception
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_MODEL_IO = """
    INSERT INTO public.agent_model_io (request_id, span_id, input_messages, output)
    VALUES (%s, %s, %s, %s)
"""


def insert_running(trace: RequestTrace) -> None:
    with db_transaction() as (_, cur):
        cur.execute(_INSERT_RUNNING, (trace.request_id, trace.thread_id, trace.channel, trace.question,
                                      _timestamp(trace.started_at_ms)))


def persist(trace: RequestTrace) -> None:
    """Write the finished request, its spans and model I/O in one transaction."""
    with db_transaction() as (_, cur):
        cur.execute(_UPSERT_RUN, (
            trace.request_id, trace.thread_id, trace.channel, trace.question, trace.status, trace.error_code,
            trace.error_message, trace.exception, _timestamp(trace.started_at_ms), trace.latency_ms,
            _json(trace.token_usage) if trace.token_usage else None, trace.all_warnings(), _json(trace.output),
        ))
        if trace.spans:
            cur.executemany(_INSERT_SPAN, [_span_row(trace.request_id, span) for span in trace.spans])
        if trace.model_io:
            cur.executemany(_INSERT_MODEL_IO, [
                (trace.request_id, span_id, _json(io["input_messages"]), _json(io["output"]))
                for span_id, io in trace.model_io.items()
            ])


def _span_row(request_id: str, span: Span) -> tuple:
    return (
        request_id, span.span_id, span.parent_id, span.kind, span.name, span.status, span.started_at_ms,
        span.finished_at_ms, _json(span.input), _json(span.output), _json(span.attributes), span.warnings,
        span.error_code, span.error_message, span.exception,
    )


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda item: json.dumps(item, ensure_ascii=False, default=str))


def _timestamp(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
