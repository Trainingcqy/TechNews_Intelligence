"""Acceptance run: one run_turn per question, results table for manual review.

Input: eval/acceptance/questions.jsonl, hand-written, one JSON object per line:

    {"case_id": "q01", "question": "OpenAI 最近发布了什么？", "task": "问答"}

  - question: required.
  - case_id: optional, at most 40 characters, unique; defaults to q<line number>.
  - task: optional free label (问答 / 盘点 / 追踪 / 对照), echoed into the table.

Output: runs/acceptance.jsonl (one record per question, resumable) and report.md (the table
with blank columns for the manual judgments, followed by each case's calls, answer and sources).

Status, sources and latency come from run_turn; the step count, token usage and tool arguments
come from the trace rows run_turn persists (agent_runs, agent_trace_spans).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.script_env import load_env  # noqa: E402

MAX_CASE_ID_CHARS = 40  # request_id (eval_acc_<case_id>_<8 hex>) must fit agent_runs.request_id VARCHAR(64)

JUDGMENT_COLUMNS = ("调用工具", "多对象各检索一次", "无对象走浏览", "引用支持", "角色归属")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if text:
                out.append(json.loads(text))
    return out


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


def _load_questions(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(
            f"Question set not found: {path}\n"
            "Write it by hand, one JSON object per line, e.g.\n"
            '  {"case_id": "q01", "question": "OpenAI 最近发布了什么？", "task": "问答"}\n'
            "question is required; case_id (<= 40 chars, unique) and task (问答/盘点/追踪/对照) are optional."
        )
    cases: list[dict[str, str]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            question = str(item.get("question") or "").strip() if isinstance(item, dict) else ""
            if not question:
                raise SystemExit(f'{path}:{lineno}: each line must be an object with a non-empty "question".')
            case_id = str(item.get("case_id") or f"q{lineno}").strip()
            if len(case_id) > MAX_CASE_ID_CHARS:
                raise SystemExit(f"{path}:{lineno}: case_id longer than {MAX_CASE_ID_CHARS} characters.")
            if case_id in seen:
                raise SystemExit(f"{path}:{lineno}: duplicate case_id {case_id!r}.")
            seen.add(case_id)
            cases.append({"case_id": case_id, "question": question, "task": str(item.get("task") or "").strip()})
    if not cases:
        raise SystemExit(f"{path} has no questions.")
    return cases


def _format_call(name: str, args: Any) -> str:
    items = args.items() if isinstance(args, dict) else ()
    shown = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in items if v is not None)
    return f"{name}({shown})"


def _trace_facts(request_id: str, fetch: Callable[[str, tuple], list[tuple]]) -> dict[str, Any]:
    """Step count, token usage and formatted tool calls of one request, from the persisted trace."""
    runs = fetch("SELECT token_usage FROM public.agent_runs WHERE request_id = %s", (request_id,))
    spans = fetch(
        """
        SELECT span_type, name, input_summary
        FROM public.agent_trace_spans
        WHERE request_id = %s
          AND ((span_type = 'graph_node' AND name = 'collect') OR span_type = 'tool_call')
        ORDER BY started_at_ms
        """,
        (request_id,),
    )
    usage = runs[0][0] if runs and isinstance(runs[0][0], dict) else None
    calls = [
        _format_call(name, (summary or {}).get("args") if isinstance(summary, dict) else None)
        for span_type, name, summary in spans
        if span_type == "tool_call"
    ]
    return {
        "steps": sum(1 for span_type, _, _ in spans if span_type == "graph_node"),
        "token_usage": usage,
        "calls": calls,
    }


def _db_fetch(sql: str, params: tuple) -> list[tuple]:
    from services.db import db_cursor

    with db_cursor() as (_, cur):
        cur.execute(sql, params)
        return list(cur.fetchall())


def _record(case: dict[str, str], request_id: str, answer: Any, seconds: float, facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "task": case["task"],
        "question": case["question"],
        "request_id": request_id,
        "status": answer.status,
        "error_code": answer.error_code,
        "tool_calls": list(answer.tool_calls),
        "calls": facts["calls"],
        "steps": facts["steps"],
        "source_count": len(answer.sources),
        "seconds": round(seconds, 1),
        "token_usage": facts["token_usage"],
        "answer": answer.text,
        "sources": [
            {"n": s.n, "title": s.title, "source": s.source, "published_at": s.published_at, "url": s.url}
            for s in answer.sources
        ],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def _cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _build_report(records: list[dict[str, Any]]) -> str:
    lines: list[str] = ["# 验收问题集运行结果", "", f"生成时间：{datetime.now(timezone.utc).isoformat()}", ""]
    status_counts = Counter(r["status"] for r in records)
    lines.append(f"问题数：**{len(records)}**；状态分布：" + "，".join(f"{k} {v}" for k, v in sorted(status_counts.items())))
    lines.append("")
    lines.append(
        f"`direct` 状态共 **{status_counts.get('direct', 0)}** 条，逐条判断其中是否有应调用工具而未调用的事实性问题，"
        "结果填入「调用工具」列。"
    )
    seconds = [r["seconds"] for r in records]
    tokens = [r["token_usage"]["total_tokens"] for r in records if (r.get("token_usage") or {}).get("total_tokens")]
    if seconds:
        lines.append("")
        lines.append(f"耗时中位数 {statistics.median(seconds):.1f} 秒" + (f"；token 中位数 {statistics.median(tokens):.0f}" if tokens else ""))
    lines.append("")
    header = ["编号", "任务", "问题", "状态", "工具调用序列", "步数", "来源数", "耗时(秒)", "token", *JUDGMENT_COLUMNS]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for r in records:
        usage = r.get("token_usage") or {}
        row = [
            r["case_id"], r["task"], r["question"], r["status"],
            " → ".join(r["tool_calls"]), r["steps"], r["source_count"], r["seconds"], usage.get("total_tokens"),
            *[""] * len(JUDGMENT_COLUMNS),
        ]
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    lines.append("")
    lines.append("## 逐条明细")
    for r in records:
        lines.append("")
        lines.append(f"### {r['case_id']} {r['question']}")
        lines.append("")
        lines.append(f"状态：{r['status']}" + (f"（{r['error_code']}）" if r.get("error_code") else "") + f"；request_id：`{r['request_id']}`")
        lines.append("")
        if r["calls"]:
            lines.append("调用：")
            lines.extend(f"- `{call}`" for call in r["calls"])
        else:
            lines.append("调用：无")
        lines.append("")
        lines.extend("> " + part if part else ">" for part in (r["answer"] or "").splitlines())
        if r["sources"]:
            lines.append("")
            lines.append("来源：")
            for s in r["sources"]:
                lines.append(f"- [{s['n']}] {s['source']} {s['published_at']} {s['title']} {s['url']}")
    lines.append("")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Acceptance question-set runner.")
    parser.add_argument("--questions", type=Path, default=here / "questions.jsonl")
    parser.add_argument("--output", type=Path, default=here / "runs" / "acceptance.jsonl")
    parser.add_argument("--report", type=Path, default=here / "report.md")
    parser.add_argument("--only-case-id", type=str, default=None)
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="Delay between questions (RPM quota).")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    load_env()

    cases = _load_questions(args.questions.resolve())
    if args.only_case_id:
        cases = [c for c in cases if c["case_id"] == args.only_case_id]
        if not cases:
            print(f"No case matching case_id={args.only_case_id}")
            return 1

    output_path = args.output.resolve()
    existing = {r["case_id"]: r for r in _read_jsonl(output_path)} if output_path.exists() else {}
    pending = [c for c in cases if existing.get(c["case_id"], {}).get("status", "error") == "error"]
    print(f"Total: {len(cases)}; pending: {len(pending)}")

    from agent import run_turn  # noqa: E402

    run_token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for idx, case in enumerate(pending, start=1):
        case_id = case["case_id"]
        request_id = f"eval_acc_{case_id}_{uuid.uuid4().hex[:8]}"
        print(f"[{idx}/{len(pending)}] {case_id}: {case['question']} ...", flush=True)
        started = time.perf_counter()
        answer = run_turn(
            case["question"], [], request_id=request_id, thread_id=f"eval_acc_{case_id}_{run_token}"
        )
        seconds = time.perf_counter() - started
        try:
            facts = _trace_facts(answer.request_id or request_id, _db_fetch)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] trace lookup failed: {type(exc).__name__}: {exc}")
            facts = {"steps": None, "token_usage": None, "calls": []}
        record = _record(case, request_id, answer, seconds, facts)
        _append_jsonl(output_path, record)
        existing[case_id] = record
        usage = record["token_usage"] or {}
        print(
            f"  -> status={record['status']} tools={'>'.join(record['tool_calls']) or '-'} steps={record['steps']} "
            f"sources={record['source_count']} seconds={record['seconds']} tokens={usage.get('total_tokens')}"
        )
        if args.sleep_seconds > 0 and idx < len(pending):
            time.sleep(args.sleep_seconds)

    records = [existing[c["case_id"]] for c in cases if c["case_id"] in existing]
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_build_report(records), encoding="utf-8")
    print("=" * 60)
    print(f"Done. status counts: {dict(Counter(r['status'] for r in records))}; report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
