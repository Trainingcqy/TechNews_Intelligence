"""Inspect self-hosted agent traces stored in PostgreSQL."""

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

# Median latency in seconds before the redesign, over requests since 2026-06-01.
# The intent node (10.0 s) has no counterpart in the new chain.
_BASELINE_NODE_SECONDS = {
    ("model_call", "gather"): 5.0,  # was tool planning
    ("model_call", "final_synthesizer"): 19.0,
    ("tool_call", "search_news"): 2.9,
}
_BASELINE_REQUEST_SECONDS = 43.7


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query agent trace spans and full model I/O.")
    parser.add_argument("--request-id", help="Request id to inspect.")
    parser.add_argument("--case-id", help="Eval case id. The newest non-success run is preferred.")
    parser.add_argument("--model-span-id", help="Print full prompt/output for one model_call span.")
    parser.add_argument("--show-model-io", action="store_true", help="Print all full model I/O rows for the request.")
    parser.add_argument("--limit", type=int, default=20, help="Max rows for case lookup.")
    parser.add_argument("--node-stats", action="store_true", help="Median latency and tokens per node, and the parallel gain of run_tool.")
    parser.add_argument("--since", help="With --node-stats: only requests created on or after this date (YYYY-MM-DD, UTC+8).")
    return parser.parse_args()


def _fetch_all(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with db_cursor() as (_, cur):
        cur.execute(sql, params)
        columns = [item[0] for item in cur.description or []]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _fetch_one(sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    rows = _fetch_all(sql, params)
    return rows[0] if rows else None


def _resolve_case_request(case_id: str, *, limit: int) -> str | None:
    rows = _fetch_all(
        """
        SELECT request_id, final_status, latency_ms, evidence_count, created_at
        FROM public.agent_runs
        WHERE trace_payload #>> '{final_answer_metadata,case_id}' = %s
           OR trace_payload #>> '{summary,final_answer_metadata,case_id}' = %s
        ORDER BY (final_status = 'success') ASC, created_at DESC
        LIMIT %s
        """,
        (case_id, case_id, max(1, int(limit))),
    )
    if not rows:
        print(f"No trace rows found for case_id={case_id}")
        return None
    print(f"Runs for case_id={case_id}:")
    for row in rows:
        print(
            f"- {row['request_id']} status={row['final_status']} "
            f"latency_ms={row['latency_ms']} evidence={row['evidence_count']} created_at={row['created_at']}"
        )
    return str(rows[0]["request_id"])


def _print_request_summary(request_id: str) -> None:
    row = _fetch_one(
        """
        SELECT final_status, latency_ms, evidence_count, error_code, trace_payload, created_at
        FROM public.agent_runs
        WHERE request_id = %s
        """,
        (request_id,),
    )
    if not row:
        print(f"No run found for request_id={request_id}")
        return
    payload = row.get("trace_payload") if isinstance(row.get("trace_payload"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    langsmith = runtime.get("langsmith") if isinstance(runtime.get("langsmith"), dict) else {}
    run_id = str(langsmith.get("langsmith_run_id") or "").strip()
    run_suffix = f" run_id={run_id}" if run_id else ""
    print(
        f"Run request_id={request_id} status={row['final_status']} "
        f"latency_ms={row['latency_ms']} evidence={row['evidence_count']} "
        f"error={row['error_code']} created_at={row['created_at']}"
    )
    print(
        "LangSmith "
        f"enabled={bool(langsmith.get('enabled'))} "
        f"project={langsmith.get('project') or ''} "
        f"endpoint={langsmith.get('endpoint') or ''}{run_suffix}"
    )


def _print_span_tree(request_id: str) -> None:
    spans = _fetch_all(
        """
        SELECT span_id, parent_span_id, span_type, name, status, latency_ms,
               input_summary, output_summary, error_code, error_message, metadata
        FROM public.agent_trace_spans
        WHERE request_id = %s
        ORDER BY started_at_ms ASC
        """,
        (request_id,),
    )
    if not spans:
        print(f"No spans found for request_id={request_id}")
        return

    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        parent = span.get("parent_span_id")
        children.setdefault(parent, []).append(span)

    def _walk(parent: str | None, depth: int) -> None:
        for span in children.get(parent, []):
            indent = "  " * depth
            suffix = ""
            if span.get("error_code"):
                suffix = f" error={span['error_code']}"
            print(
                f"{indent}- {span['name']} [{span['span_type']}] "
                f"status={span['status']} latency_ms={span['latency_ms']} span_id={span['span_id']}{suffix}"
            )
            _walk(str(span["span_id"]), depth + 1)

    print(f"Span tree for request_id={request_id}:")
    _walk(None, 0)


def _print_model_io(request_id: str, *, span_id: str | None = None) -> None:
    if span_id:
        rows = _fetch_all(
            """
            SELECT request_id, span_id, node, provider, model, input_messages,
                   raw_output, parsed_output, token_usage, created_at
            FROM public.agent_model_io
            WHERE request_id = %s AND span_id = %s
            ORDER BY created_at ASC
            """,
            (request_id, span_id),
        )
    else:
        rows = _fetch_all(
            """
            SELECT request_id, span_id, node, provider, model, input_messages,
                   raw_output, parsed_output, token_usage, created_at
            FROM public.agent_model_io
            WHERE request_id = %s
            ORDER BY created_at ASC
            """,
            (request_id,),
        )
    if not rows:
        target = f"{request_id}/{span_id}" if span_id else request_id
        print(f"No model I/O rows found for {target}")
        return
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


_MEDIAN = "percentile_cont(0.5) WITHIN GROUP (ORDER BY {})"


def _node_stats(since: str) -> list[dict[str, Any]]:
    return _fetch_all(
        f"""
        SELECT s.span_type, s.name, count(*) AS n,
               {_MEDIAN.format("s.latency_ms")} AS latency_ms,
               {_MEDIAN.format("(m.token_usage->>'prompt_tokens')::numeric")} AS prompt_tokens,
               {_MEDIAN.format("(m.token_usage->>'completion_tokens')::numeric")} AS completion_tokens,
               {_MEDIAN.format("(m.token_usage->>'total_tokens')::numeric")} AS total_tokens
        FROM public.agent_trace_spans s
        LEFT JOIN public.agent_model_io m ON m.request_id = s.request_id AND m.span_id = s.span_id
        WHERE s.created_at >= %s::timestamptz
          AND s.span_type IN ('graph_node', 'model_call', 'tool_call')
          AND s.latency_ms IS NOT NULL
        GROUP BY s.span_type, s.name
        ORDER BY s.span_type, s.name
        """,
        (since,),
    )


def _request_stats(since: str) -> dict[str, Any]:
    return _fetch_all(
        f"""
        SELECT count(*) AS n,
               {_MEDIAN.format("latency_ms")} AS latency_ms,
               {_MEDIAN.format("(token_usage->>'prompt_tokens')::numeric")} AS prompt_tokens,
               {_MEDIAN.format("(token_usage->>'completion_tokens')::numeric")} AS completion_tokens,
               {_MEDIAN.format("(token_usage->>'total_tokens')::numeric")} AS total_tokens
        FROM public.agent_runs
        WHERE created_at >= %s::timestamptz
        """,
        (since,),
    )[0]


def _parallel_steps(since: str) -> list[dict[str, Any]]:
    # A step is the set of run_tool spans started after the same gather span.
    return _fetch_all(
        """
        WITH rt AS (
            SELECT r.request_id, r.started_at_ms, r.finished_at_ms, r.latency_ms,
                   (SELECT max(g.started_at_ms) FROM public.agent_trace_spans g
                     WHERE g.request_id = r.request_id AND g.span_type = 'graph_node'
                       AND g.name = 'gather' AND g.started_at_ms <= r.started_at_ms) AS step_key
            FROM public.agent_trace_spans r
            WHERE r.span_type = 'graph_node' AND r.name = 'run_tool'
              AND r.created_at >= %s::timestamptz AND r.latency_ms IS NOT NULL AND r.finished_at_ms IS NOT NULL
        )
        SELECT request_id, step_key, count(*) AS branches, sum(latency_ms) AS branch_ms_sum,
               max(finished_at_ms) - min(started_at_ms) AS step_ms
        FROM rt
        WHERE step_key IS NOT NULL
        GROUP BY request_id, step_key
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
    print(f"Per-node medians, requests created since {day} (UTC+8):")
    print(f"{'type':<11} {'node':<20} {'n':>5} {'sec':>7} {'base sec':>9} {'prompt':>8} {'completion':>11} {'total':>8}")
    for row in _node_stats(since):
        base = _BASELINE_NODE_SECONDS.get((row["span_type"], row["name"]))
        print(
            f"{row['span_type']:<11} {row['name']:<20} {row['n']:>5} {_seconds(row['latency_ms']):>7} "
            f"{'-' if base is None else f'{base:.1f}':>9} {_tokens(row['prompt_tokens']):>8} "
            f"{_tokens(row['completion_tokens']):>11} {_tokens(row['total_tokens']):>8}"
        )
    request = _request_stats(since)
    print(
        f"{'request':<11} {'(whole request)':<20} {request['n']:>5} {_seconds(request['latency_ms']):>7} "
        f"{_BASELINE_REQUEST_SECONDS:>9.1f} {_tokens(request['prompt_tokens']):>8} "
        f"{_tokens(request['completion_tokens']):>11} {_tokens(request['total_tokens']):>8}"
    )
    print("base sec: medians before the redesign (2026-06-01 onward); the intent node (10.0 s) no longer exists.")

    steps = [row for row in _parallel_steps(since) if row["step_ms"]]
    print()
    if not steps:
        print("Parallel gain: no step with several run_tool branches.")
        return
    ratios = [float(row["branch_ms_sum"]) / float(row["step_ms"]) for row in steps]
    aggregate = sum(float(row["branch_ms_sum"]) for row in steps) / sum(float(row["step_ms"]) for row in steps)
    print(
        f"Parallel gain over {len(steps)} steps with several run_tool branches: "
        f"sum of branch time / step time, median {statistics.median(ratios):.2f}, aggregate {aggregate:.2f} "
        "(1.00 = sequential)."
    )


def main() -> int:
    args = _parse_args()
    if args.node_stats:
        if not args.since:
            raise SystemExit("--node-stats requires --since YYYY-MM-DD.")
        _print_node_stats(date.fromisoformat(args.since).isoformat())
        return 0
    request_id = str(args.request_id or "").strip()
    if args.case_id and not request_id:
        resolved = _resolve_case_request(str(args.case_id).strip(), limit=int(args.limit))
        if not resolved:
            return 1
        request_id = resolved

    if not request_id:
        raise SystemExit("--request-id or --case-id is required.")

    _print_request_summary(request_id)
    _print_span_tree(request_id)
    if args.show_model_io or args.model_span_id:
        _print_model_io(request_id, span_id=str(args.model_span_id).strip() if args.model_span_id else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
