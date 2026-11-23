"""Inspect agent traces stored in PostgreSQL."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.db import db_cursor  # noqa: E402

USAGE_KEYS = ("input_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
_MEDIAN = "percentile_cont(0.5) WITHIN GROUP (ORDER BY {})"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query agent traces and full model I/O.")
    parser.add_argument("--request-id", help="Request id to inspect.")
    parser.add_argument("--model-span-id", help="Print the full input and output of one llm span.")
    parser.add_argument("--show-model-io", action="store_true", help="Print the full model I/O of every llm span.")
    parser.add_argument("--node-stats", action="store_true",
                        help="Median latency and tokens per span kind and name, and the parallel gain of tool calls.")
    parser.add_argument("--since", help="With --node-stats: only requests started on or after this date (YYYY-MM-DD, UTC+8).")
    return parser.parse_args()


def _fetch_all(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with db_cursor() as (_, cur):
        cur.execute(sql, params)
        columns = [item[0] for item in cur.description or []]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _print_request(request_id: str) -> bool:
    rows = _fetch_all(
        """
        SELECT status, channel, thread_id, started_at, latency_ms, token_usage, warnings, error_code, error_message
        FROM public.agent_runs
        WHERE request_id = %s
        """,
        (request_id,),
    )
    if not rows:
        print(f"No run found for request_id={request_id}")
        return False
    run = rows[0]
    print(f"Run {request_id} status={run['status']} channel={run['channel']} latency_ms={run['latency_ms']} "
          f"started_at={run['started_at']} thread_id={run['thread_id']}")
    usage = run["token_usage"] or {}
    if usage:
        print("Tokens " + " ".join(f"{key}={usage.get(key, 0)}" for key in USAGE_KEYS))
    if run["warnings"]:
        print("Warnings " + ", ".join(run["warnings"]))
    if run["error_code"]:
        print(f"Error {run['error_code']}: {run['error_message']}")
    return True


def _print_span_tree(request_id: str) -> None:
    spans = _fetch_all(
        """
        SELECT span_id, parent_span_id, kind, name, status, started_at_ms, finished_at_ms, warnings, error_code
        FROM public.agent_trace_spans
        WHERE request_id = %s
        ORDER BY started_at_ms, span_id
        """,
        (request_id,),
    )
    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        children.setdefault(span["parent_span_id"], []).append(span)

    def walk(parent: str | None, depth: int) -> None:
        for span in children.get(parent, []):
            latency = None if span["finished_at_ms"] is None else span["finished_at_ms"] - span["started_at_ms"]
            suffix = "".join([
                f" warnings={','.join(span['warnings'])}" if span["warnings"] else "",
                f" error={span['error_code']}" if span["error_code"] else "",
            ])
            print(f"{'  ' * depth}- {span['kind']} {span['name'] or ''} status={span['status']} "
                  f"latency_ms={latency} span_id={span['span_id']}{suffix}")
            walk(span["span_id"], depth + 1)

    print("Spans:")
    walk(None, 1)


def _print_model_io(request_id: str, span_id: str | None) -> None:
    rows = _fetch_all(
        f"""
        SELECT s.span_id, s.name, m.input_messages, m.output
        FROM public.agent_model_io m
        JOIN public.agent_trace_spans s ON s.request_id = m.request_id AND s.span_id = m.span_id
        WHERE m.request_id = %s {"AND m.span_id = %s" if span_id else ""}
        ORDER BY s.started_at_ms
        """,
        (request_id, span_id) if span_id else (request_id,),
    )
    if not rows:
        print("No model I/O rows found.")
        return
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


def _usage_medians(column: str) -> str:
    return ", ".join(f"{_MEDIAN.format(f'({column}->>{key!r})::numeric')} AS {key}" for key in USAGE_KEYS)


def _span_stats(since: str) -> list[dict[str, Any]]:
    return _fetch_all(
        f"""
        SELECT s.kind, COALESCE(s.name, '') AS name, count(*) AS n,
               {_MEDIAN.format("s.finished_at_ms - s.started_at_ms")} AS latency_ms,
               {_usage_medians("s.attributes->'usage'")}
        FROM public.agent_trace_spans s
        JOIN public.agent_runs r ON r.request_id = s.request_id
        WHERE r.started_at >= %s::timestamptz AND s.finished_at_ms IS NOT NULL
        GROUP BY s.kind, s.name
        ORDER BY s.kind, s.name
        """,
        (since,),
    )


def _request_stats(since: str) -> dict[str, Any]:
    return _fetch_all(
        f"""
        SELECT count(*) AS n, {_MEDIAN.format("latency_ms")} AS latency_ms, {_usage_medians("token_usage")}
        FROM public.agent_runs
        WHERE started_at >= %s::timestamptz AND latency_ms IS NOT NULL
        """,
        (since,),
    )[0]


def _parallel_steps(since: str) -> list[dict[str, Any]]:
    # The tool calls of one step share its step span as parent and run in parallel.
    return _fetch_all(
        """
        SELECT t.request_id, t.parent_span_id, count(*) AS calls,
               sum(t.finished_at_ms - t.started_at_ms) AS call_ms_sum,
               max(t.finished_at_ms) - min(t.started_at_ms) AS wall_ms
        FROM public.agent_trace_spans t
        JOIN public.agent_runs r ON r.request_id = t.request_id
        WHERE t.kind = 'tool' AND r.started_at >= %s::timestamptz AND t.finished_at_ms IS NOT NULL
        GROUP BY t.request_id, t.parent_span_id
        HAVING count(*) > 1
        """,
        (since,),
    )


def _seconds(ms: Any) -> str:
    return "-" if ms is None else f"{float(ms) / 1000:.1f}"


def _tokens(value: Any) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def _print_node_stats(day: str) -> None:
    since = f"{day}T00:00:00+08:00"
    print(f"Medians per span kind and name, requests started since {day} (UTC+8):")
    header = f"{'kind':<10} {'name':<14} {'n':>5} {'sec':>7} " + " ".join(f"{key.split('_')[0]:>9}" for key in USAGE_KEYS)
    print(header)
    rows = [*_span_stats(since), {"kind": "request", "name": "", **_request_stats(since)}]
    for row in rows:
        print(f"{row['kind']:<10} {row['name']:<14} {row['n']:>5} {_seconds(row['latency_ms']):>7} "
              + " ".join(f"{_tokens(row[key]):>9}" for key in USAGE_KEYS))
    print("Token columns: input includes cache hits (cache), output includes reasoning.")

    steps = [row for row in _parallel_steps(since) if row["wall_ms"]]
    print()
    if not steps:
        print("Parallel gain: no step with several tool calls.")
        return
    ratios = [float(row["call_ms_sum"]) / float(row["wall_ms"]) for row in steps]
    aggregate = sum(float(row["call_ms_sum"]) for row in steps) / sum(float(row["wall_ms"]) for row in steps)
    print(
        f"Parallel gain over {len(steps)} steps with several tool calls: sum of call time / wall time of the calls, "
        f"median {statistics.median(ratios):.2f}, aggregate {aggregate:.2f} (1.00 = sequential)."
    )


def main() -> int:
    args = _parse_args()
    if args.node_stats:
        if not args.since:
            raise SystemExit("--node-stats requires --since YYYY-MM-DD.")
        _print_node_stats(date.fromisoformat(args.since).isoformat())
        return 0
    request_id = str(args.request_id or "").strip()
    if not request_id:
        raise SystemExit("--request-id is required.")
    if not _print_request(request_id):
        return 1
    _print_span_tree(request_id)
    if args.show_model_io or args.model_span_id:
        _print_model_io(request_id, str(args.model_span_id).strip() if args.model_span_id else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
