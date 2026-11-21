"""Tracking-task acceptance (plan 28.3): three hand-built sets, one run_turn per case, automatic metrics.

Input, in eval/tracking/ (built by hand from the library after the switch-over), one JSON object per line:

    inventory.jsonl  {"case_id": "i01", "question": "...", "expected_story_ids": [101, 102]}
    tracking.jsonl   {"case_id": "t01", "question": "...",
                      "progress": [{"article_id": 11, "label": "..."}, ...],
                      "follow_query": "...", "checked_at": "2026-11-10", "followup_question": "..."}
                     follow_query, checked_at and followup_question are optional but come together.
    contrast.jsonl   {"case_id": "c01", "question": "...", "story_id": 301}

    python eval/tracking/run_tracking.py run [--set inventory|tracking|contrast|all]
    python eval/tracking/run_tracking.py score          # after filling in contrast_sheet.md

Output: runs/<set>.jsonl (resumable; cases with status `error` are rerun), report.md, and for the contrast set
contrast_sheet.md, the Markdown table for the two manual judgments. Articles are only read; the one write is the
follow created for the "since last time" check of the tracking set, which is deleted after the check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import BEIJING  # noqa: E402
from agent.retrieval.stories import fetch_discussions_by_url_keys  # noqa: E402
from eval.acceptance.run_acceptance import _append_jsonl, _cell, _read_jsonl  # noqa: E402
from eval.common import (  # noqa: E402
    add_db_args, answer_line, configure_stdout, parse_answers, pct, read_choices, readonly_cursor,
)
from services.script_env import load_env  # noqa: E402
from services.urls import url_key  # noqa: E402

MAX_CASE_ID_CHARS = 40  # request_id (eval_<set>_<case_id>_<8 hex>) must fit agent_runs.request_id VARCHAR(64)
SETS = ("inventory", "tracking", "contrast")
REQUEST_PREFIX = {"inventory": "eval_inv", "tracking": "eval_trk", "contrast": "eval_con", "followup": "eval_fup"}
EVAL_USER_KEY = "eval:tracking"
DISCUSSION_SOURCE = "HackerNews 讨论"
NON_EVENT_KINDS = ("tutorial", "opinion", "promo")

# Pass conditions of plan 28.3 (initial values).
MIN_INVENTORY_RECALL = 0.7
MIN_COMPLETENESS = 0.8
MIN_ROLE_COVERAGE = 0.9

SHEET_FIELDS = ("归属错误", "评论表述为事实")

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*]\s|\d+[.)、](?!\d))")


# ---------------------------------------------------------------- input

_EXAMPLES = {
    "inventory": '{"case_id": "i01", "question": "过去两周 AI 领域发生了什么？", "expected_story_ids": [101, 102]}',
    "tracking": '{"case_id": "t01", "question": "...", "progress": [{"article_id": 11, "label": "..."}], '
                '"follow_query": "...", "checked_at": "2026-11-10", "followup_question": "..."}',
    "contrast": '{"case_id": "c01", "question": "...", "story_id": 301}',
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_inventory(item: dict, where: str) -> dict:
    ids = item.get("expected_story_ids")
    if not isinstance(ids, list) or not ids or not all(_is_int(i) for i in ids):
        raise SystemExit(f'{where}: "expected_story_ids" must be a non-empty list of integers.')
    return {"expected_story_ids": list(dict.fromkeys(ids))}


def _check_tracking(item: dict, where: str) -> dict:
    progress = item.get("progress")
    if not isinstance(progress, list) or not progress:
        raise SystemExit(f'{where}: "progress" must be a non-empty list.')
    for step in progress:
        if not isinstance(step, dict) or not _is_int(step.get("article_id")) or not isinstance(step.get("label"), str):
            raise SystemExit(f'{where}: each progress item must be {{"article_id": <int>, "label": "<text>"}}.')
    extra: dict[str, Any] = {"progress": [{"article_id": s["article_id"], "label": s["label"]} for s in progress]}
    follow = {k: item.get(k) for k in ("follow_query", "checked_at", "followup_question")}
    if any(v is not None for v in follow.values()):
        if not all(isinstance(v, str) and v.strip() for v in follow.values()):
            raise SystemExit(f"{where}: follow_query, checked_at and followup_question must be given together.")
        try:
            date.fromisoformat(follow["checked_at"])
        except ValueError:
            raise SystemExit(f'{where}: "checked_at" must be a date such as 2026-11-10.') from None
        extra.update({k: v.strip() for k, v in follow.items()})
    return extra


def _check_contrast(item: dict, where: str) -> dict:
    if not _is_int(item.get("story_id")):
        raise SystemExit(f'{where}: "story_id" must be an integer.')
    return {"story_id": item["story_id"]}


_CHECKS = {"inventory": _check_inventory, "tracking": _check_tracking, "contrast": _check_contrast}


def load_cases(path: Path, set_name: str) -> list[dict]:
    if not path.exists():
        raise SystemExit(
            f"Acceptance set not found: {path}\n"
            "Build it by hand from the library, one JSON object per line, e.g.\n"
            f"  {_EXAMPLES[set_name]}\n"
            f"case_id is required (<= {MAX_CASE_ID_CHARS} chars, unique)."
        )
    cases: list[dict] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            where = f"{path}:{lineno}"
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{where}: invalid JSON: {exc}") from exc
            if not isinstance(item, dict):
                raise SystemExit(f"{where}: each line must be a JSON object.")
            case_id = str(item.get("case_id") or "").strip()
            question = str(item.get("question") or "").strip()
            if not case_id or not question:
                raise SystemExit(f'{where}: "case_id" and "question" are required.')
            if len(case_id) > MAX_CASE_ID_CHARS:
                raise SystemExit(f"{where}: case_id longer than {MAX_CASE_ID_CHARS} characters.")
            if case_id in seen:
                raise SystemExit(f"{where}: duplicate case_id {case_id!r}.")
            seen.add(case_id)
            cases.append({"case_id": case_id, "question": question, **_CHECKS[set_name](item, where)})
    if not cases:
        raise SystemExit(f"{path} has no cases.")
    return cases


# ---------------------------------------------------------------- sources and metrics

def _is_discussion(source: dict) -> bool:
    return source["source"] == DISCUSSION_SOURCE


def resolve_sources(
    sources, lookup: Callable[[list[str]], dict[str, dict]], lookup_discussions: Callable[[list[str]], dict[str, int | None]],
) -> list[dict]:
    """Match each cited source to its library article by url_key.

    A discussion has no article of its own; its story, the one of the article it discusses, goes to
    `discussion_story_id` and is used by the contrast set only.
    """
    keys = [url_key(s.url) for s in sources if s.source != DISCUSSION_SOURCE]
    discussion_keys = [url_key(s.url) for s in sources if s.source == DISCUSSION_SOURCE]
    found = lookup(keys) if keys else {}
    discussion_stories = lookup_discussions(discussion_keys) if discussion_keys else {}
    out = []
    for s in sources:
        item = {
            "n": s.n, "title": s.title, "source": s.source, "published_at": s.published_at, "url": s.url,
            "article_id": None, "story_id": None, "kind": None, "publisher_kind": None, "event_at": None,
            "discussion_story_id": None,
        }
        if s.source != DISCUSSION_SOURCE:
            item.update(found.get(url_key(s.url), {}))
        else:
            item["discussion_story_id"] = discussion_stories.get(url_key(s.url))
        out.append(item)
    return out


def cited_story_ids(sources: list[dict]) -> set[int]:
    return {s["story_id"] for s in sources if s["story_id"] is not None}


def first_positions(text: str) -> dict[int, int]:
    """Position of the first `[n]` marker for each citation number."""
    positions: dict[int, int] = {}
    for match in _CITATION_RE.finditer(text):
        positions.setdefault(int(match.group(1)), match.start())
    return positions


def inventory_metrics(expected: list[int], sources: list[dict], text: str) -> dict:
    cited = cited_story_ids(sources)
    non_event = {s["article_id"] for s in sources if s["kind"] in NON_EVENT_KINDS}
    list_lines = [line for line in text.splitlines() if _LIST_LINE_RE.match(line)]
    uncited = [line for line in list_lines if not _CITATION_RE.search(line)]
    return {
        "expected": len(expected),
        "hit": sum(1 for story_id in expected if story_id in cited),
        "non_event": len(non_event),
        "list_lines": len(list_lines),
        "uncited_lines": len(uncited),
        "all_cited": not uncited,
    }


def tracking_metrics(progress: list[dict], sources: list[dict], text: str) -> dict:
    """`progress` items carry the story resolved for their article, in chronological order."""
    positions = first_positions(text)
    covered_steps: list[bool] = []
    covered_positions: list[int] = []
    for step in progress:
        ns = [s["n"] for s in sources if s["story_id"] is not None and s["story_id"] == step["story_id"]]
        covered_steps.append(bool(ns))
        placed = [positions[n] for n in ns if n in positions]
        if placed:
            covered_positions.append(min(placed))
    return {
        "total": len(progress),
        "covered": sum(covered_steps),
        "covered_steps": covered_steps,
        "order_ok": all(a <= b for a, b in zip(covered_positions, covered_positions[1:])),
    }


def old_progress_citations(sources: list[dict], progress: list[dict], checked_at: datetime) -> list[int]:
    """Numbers of cited sources that belong to a progress story but were published before `checked_at`."""
    stories = {step["story_id"] for step in progress if step["story_id"] is not None}
    return [
        s["n"] for s in sources
        if s["story_id"] in stories and s["event_at"] and datetime.fromisoformat(s["event_at"]) < checked_at
    ]


def _story_of(source: dict) -> int | None:
    return source["discussion_story_id"] if _is_discussion(source) else source["story_id"]


def contrast_roles(sources: list[dict], story_id: int) -> list[str]:
    """Roles among the cited sources that belong to the case's story."""
    roles = set()
    for s in sources:
        if _story_of(s) != story_id:
            continue
        if _is_discussion(s):
            roles.add("discussion")
        elif s["publisher_kind"] in ("official", "media"):
            roles.add(s["publisher_kind"])
    return sorted(roles)


# ---------------------------------------------------------------- running

@dataclass
class Deps:
    ask: Callable[..., Any]  # (question, *, request_id, user_key=None) -> Answer
    lookup_urls: Callable[[list[str]], dict[str, dict]]
    lookup_discussions: Callable[[list[str]], dict[str, int | None]]  # url_key of a discussion page -> story_id
    lookup_stories: Callable[[list[int]], dict[int, int | None]]
    follows: Any  # add(user_key, query) -> follow_id; set_checked(follow_id, when); delete(user_key)


def _request_id(kind: str, case_id: str) -> str:
    return f"{REQUEST_PREFIX[kind]}_{case_id}_{uuid.uuid4().hex[:8]}"


def _answer_fields(answer, sources: list[dict], seconds: float) -> dict:
    return {
        "status": answer.status,
        "error_code": answer.error_code,
        "tool_calls": list(answer.tool_calls),
        "seconds": round(seconds, 1),
        "answer": answer.text,
        "sources": sources,
    }


def check_since_last(case: dict, deps: Deps) -> dict:
    """Follow created, viewed-at set, question asked with the user key, follow deleted, in that order.

    The user key belongs to this check alone, so every row under it is removed before and after, which also
    clears what a killed earlier run left behind. An error in the middle is recorded as status `error`.
    """
    checked_at = datetime.combine(date.fromisoformat(case["checked_at"]), dtime.min, tzinfo=BEIJING)
    request_id = _request_id("followup", case["case_id"])
    result: dict[str, Any]
    deps.follows.delete(EVAL_USER_KEY)
    try:
        follow_id = deps.follows.add(EVAL_USER_KEY, case["follow_query"])
        deps.follows.set_checked(follow_id, checked_at)
        started = time.perf_counter()
        answer = deps.ask(case["followup_question"], request_id=request_id, user_key=EVAL_USER_KEY)
        seconds = time.perf_counter() - started
        sources = resolve_sources(answer.sources, deps.lookup_urls, deps.lookup_discussions)
        old = old_progress_citations(sources, case["progress"], checked_at)
        result = _answer_fields(answer, sources, seconds)
        result.update({"request_id": request_id, "old_cited": old, "passed": None if answer.status == "error" else not old})
    except Exception as exc:  # noqa: BLE001
        result = {"request_id": request_id, "status": "error", "error_code": f"runtime_{type(exc).__name__.lower()}",
                  "detail": str(exc), "passed": None}
    finally:
        deps.follows.delete(EVAL_USER_KEY)
    return result


def run_case(set_name: str, case: dict, deps: Deps) -> dict:
    request_id = _request_id(set_name, case["case_id"])
    started = time.perf_counter()
    answer = deps.ask(case["question"], request_id=request_id)
    seconds = time.perf_counter() - started
    sources = resolve_sources(answer.sources, deps.lookup_urls, deps.lookup_discussions)
    record: dict[str, Any] = {"case_id": case["case_id"], "question": case["question"], "request_id": request_id}
    record.update(_answer_fields(answer, sources, seconds))
    if set_name == "inventory":
        record["expected_story_ids"] = case["expected_story_ids"]
        record["metrics"] = inventory_metrics(case["expected_story_ids"], sources, answer.text)
    elif set_name == "tracking":
        stories = deps.lookup_stories([step["article_id"] for step in case["progress"]])
        progress = [{**step, "story_id": stories.get(step["article_id"])} for step in case["progress"]]
        record["metrics"] = tracking_metrics(progress, sources, answer.text)
        record["progress"] = progress
        if "follow_query" in case and answer.status != "error":
            for key in ("follow_query", "checked_at", "followup_question"):
                record[key] = case[key]
            record["followup"] = check_since_last({**case, "progress": progress}, deps)
    else:
        record["story_id"] = case["story_id"]
        record["metrics"] = {"roles": contrast_roles(sources, case["story_id"])}
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    return record


def needs_rerun(record: dict | None) -> bool:
    if record is None or record["status"] == "error":
        return True
    return (record.get("followup") or {}).get("status") == "error"


def _describe(record: dict) -> str:
    m = record["metrics"]
    if "hit" in m:
        return f"recall={m['hit']}/{m['expected']} non_event={m['non_event']} all_cited={m['all_cited']}"
    if "covered" in m:
        followup = record.get("followup")
        tail = f" since_last={followup['passed']}" if followup else ""
        return f"covered={m['covered']}/{m['total']} order_ok={m['order_ok']}{tail}"
    return f"roles={','.join(m['roles']) or '-'}"


def run_set(set_name: str, cases: list[dict], output: Path, deps: Deps, sleep_seconds: float) -> list[str]:
    """Run the cases that are new or errored; returns the case ids that were run."""
    existing = {r["case_id"]: r for r in _read_jsonl(output)} if output.exists() else {}
    pending = [c for c in cases if needs_rerun(existing.get(c["case_id"]))]
    print(f"[{set_name}] total: {len(cases)}; pending: {len(pending)}")
    for idx, case in enumerate(pending, start=1):
        print(f"[{set_name} {idx}/{len(pending)}] {case['case_id']}: {case['question']} ...", flush=True)
        record = run_case(set_name, case, deps)
        _append_jsonl(output, record)
        print(f"  -> status={record['status']} seconds={record['seconds']} {_describe(record)}")
        if sleep_seconds > 0 and idx < len(pending):
            time.sleep(sleep_seconds)
    return [c["case_id"] for c in pending]


# ---------------------------------------------------------------- report

def _verdict(ok: bool) -> str:
    return "通过" if ok else "未通过"


def _ratio_row(name: str, num: int, den: int, minimum: float, condition: str) -> tuple[str, str, str, str]:
    ok = den > 0 and num / den >= minimum
    return name, f"{num} / {den} = {pct(num, den)}", condition, _verdict(ok)


def inventory_summary(records: list[dict]) -> list[tuple[str, str, str, str]]:
    ms = [r["metrics"] for r in records]
    non_event = sum(m["non_event"] for m in ms)
    all_cited = sum(m["all_cited"] for m in ms)
    return [
        _ratio_row("应提及事件的召回率", sum(m["hit"] for m in ms), sum(m["expected"] for m in ms), MIN_INVENTORY_RECALL, "不低于 70%"),
        ("非事件类条目数（tutorial、opinion、promo）", str(non_event), "0", _verdict(non_event == 0)),
        ("全部带引用的用例", f"{all_cited} / {len(ms)}", "全部", _verdict(bool(ms) and all_cited == len(ms))),
    ]


def tracking_summary(records: list[dict]) -> list[tuple[str, str, str, str]]:
    ms = [r["metrics"] for r in records]
    order_ok = sum(m["order_ok"] for m in ms)
    checked = [r["followup"] for r in records if r.get("followup") and r["followup"]["passed"] is not None]
    passed = sum(f["passed"] for f in checked)
    rows = [
        _ratio_row("进展的完整率", sum(m["covered"] for m in ms), sum(m["total"] for m in ms), MIN_COMPLETENESS, "不低于 80%"),
        ("顺序正确的用例", f"{order_ok} / {len(ms)}", "全部", _verdict(bool(ms) and order_ok == len(ms))),
    ]
    rows.append(("“自上次以来”不混入旧进展的用例", f"{passed} / {len(checked)}", "全部",
                 _verdict(bool(checked) and passed == len(checked)) if checked else "无用例"))
    return rows


def contrast_summary(records: list[dict], sheet_scores: dict[str, dict] | None) -> list[tuple[str, str, str, str]]:
    full = sum(len(r["metrics"]["roles"]) == 3 for r in records)
    rows = [_ratio_row("角色覆盖（官方、媒体、讨论齐全）的用例", full, len(records), MIN_ROLE_COVERAGE, "不低于 90%")]
    for field in SHEET_FIELDS:
        score = (sheet_scores or {}).get(field)
        if score is None:
            rows.append((field, "未判断", "0", "待判断"))
            continue
        judged, flagged, total = score["judged"], score["flagged"], score["total"]
        verdict = "待判断" if judged < total and flagged == 0 else _verdict(flagged == 0)
        rows.append((field, f"{flagged}（已判断 {judged} / {total}）", "0", verdict))
    return rows


def sheet_scores(sheet_path: Path) -> dict[str, dict]:
    scores = {}
    for field in SHEET_FIELDS:
        choices = read_choices(sheet_path, field)
        judged = [v for v in choices.values() if v is not None]
        scores[field] = {"judged": len(judged), "flagged": judged.count("y"), "total": len(choices)}
    return scores


def _source_line(s: dict) -> str:
    role = "discussion" if _is_discussion(s) else (s["publisher_kind"] or "未对回")
    story_id = _story_of(s)
    story = "" if story_id is None else f" story={story_id}"
    kind = "" if s["kind"] is None else f" kind={s['kind']}"
    return f"[{s['n']}] {s['source']} {s['published_at']} {s['title']} ({role}{story}{kind}) {s['url']}"


def _quote(text: str) -> list[str]:
    return ["> " + part if part else ">" for part in (text or "").splitlines()]


def _case_table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines


def _details(records: list[dict], extra: Callable[[dict], list[str]] | None = None) -> list[str]:
    lines = ["", "### 逐条明细"]
    for r in records:
        lines += ["", f"#### {r['case_id']} {r['question']}", "",
                  f"状态：{r['status']}" + (f"（{r['error_code']}）" if r.get("error_code") else "") + f"；request_id：`{r['request_id']}`", ""]
        lines += _quote(r["answer"])
        if r["sources"]:
            lines += ["", "来源："] + [f"- {_source_line(s)}" for s in r["sources"]]
        if extra:
            lines += extra(r)
    return lines


def _tracking_extra(r: dict) -> list[str]:
    lines = ["", "进展："]
    for step, covered in zip(r["progress"], r["metrics"]["covered_steps"]):
        mark = "已引用" if covered else "未引用"
        story = "未对回故事" if step["story_id"] is None else f"story={step['story_id']}"
        lines.append(f"- {mark} article={step['article_id']} {story} {step['label']}")
    followup = r.get("followup")
    if followup:
        lines += ["", f"自上次以来：关注项“{r['follow_query']}”，查看时间 {r['checked_at']}，提问“{r['followup_question']}”；"
                  f"状态 {followup['status']}，早于查看时间的进展来源 {followup.get('old_cited') or '无'}"]
        lines += _quote(followup.get("answer", ""))
        lines += [f"- {_source_line(s)}" for s in followup.get("sources", [])]
    return lines


def build_report(runs: dict[str, list[dict]], sheet_stats: dict[str, dict] | None) -> str:
    lines = ["# 追踪型任务验收结果", "", f"生成时间：{datetime.now(timezone.utc).isoformat()}", "",
             "通过条件取自方案第 28.3 节，是初值；各项按全部用例汇总。状态为 `error` 的用例不计入统计。"]
    for set_name, title in (("inventory", "盘点"), ("tracking", "追踪"), ("contrast", "对照")):
        records = runs.get(set_name)
        lines += ["", f"## {title}", ""]
        if records is None:
            lines.append("未运行。")
            continue
        errored = [r["case_id"] for r in records if needs_rerun(r)]
        valid = [r for r in records if r["case_id"] not in errored]
        lines.append(f"用例 {len(records)} 条，状态分布：" + "，".join(f"{k} {v}" for k, v in sorted(Counter(r["status"] for r in records).items())))
        if errored:
            lines.append(f"待重跑（不计入统计）：{', '.join(errored)}")
        unmatched = sum(1 for r in valid for s in r["sources"] if s["article_id"] is None and not _is_discussion(s))
        if unmatched:
            lines.append(f"未对回库内文章的来源：{unmatched} 个。")
        lines.append("")
        if set_name == "inventory":
            summary, header, rows = inventory_summary(valid), ["编号", "状态", "应提及", "命中", "非事件类", "列表行", "缺引用行"], [
                [r["case_id"], r["status"], r["metrics"]["expected"], r["metrics"]["hit"], r["metrics"]["non_event"],
                 r["metrics"]["list_lines"], r["metrics"]["uncited_lines"]] for r in valid]
            extra = None
        elif set_name == "tracking":
            summary, header, rows = tracking_summary(valid), ["编号", "状态", "进展数", "已引用", "顺序正确", "自上次以来"], [
                [r["case_id"], r["status"], r["metrics"]["total"], r["metrics"]["covered"], r["metrics"]["order_ok"],
                 "" if not r.get("followup") else r["followup"]["passed"]] for r in valid]
            extra = _tracking_extra
        else:
            summary, header, rows = contrast_summary(valid, sheet_stats), ["编号", "状态", "故事", "覆盖的角色"], [
                [r["case_id"], r["status"], r["story_id"], ", ".join(r["metrics"]["roles"])] for r in valid]
            extra = None
        lines += _case_table(["指标", "结果", "通过条件", "结论"], [list(row) for row in summary]) + [""]
        lines += _case_table(header, rows)
        if set_name == "contrast":
            lines += ["", "归属是否正确、是否把评论表述为事实由人工判断，填写 `contrast_sheet.md` 后运行 `score` 子命令。"]
        lines += _details(records, extra)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- contrast sheet

def render_sheet(records: list[dict], prior: dict[str, dict[str, str]]) -> str:
    out = ["# 对照验收判断表", "",
           f"共 {len(records)} 条。逐条阅读回答与来源，两项都是 `y` 表示有问题、`n` 表示没有，拿不准填 `?`：", "",
           "- 归属错误：回答把官方说法、媒体报道或社区评论归给了错误的主体。",
           "- 评论表述为事实：回答把社区评论中的看法或猜测写成了已确认的事实。", "", "---", ""]
    for number, r in enumerate(records, start=1):
        out += [f"## {number}. `{r['case_id']}`", "", f"{r['question']}（故事 {r['story_id']}）", ""]
        out += _quote(r["answer"])
        out += [""] + [f"- {_source_line(s)}" for s in r["sources"]] + [""]
        for field in SHEET_FIELDS:
            kept = prior.get(r["case_id"], {}).get(field)
            out.append(f"- {field}：`[{kept}]`" if kept else answer_line(field))
        out += ["", "---", ""]
    return "\n".join(out)


def write_sheet(path: Path, records: list[dict], rerun: set[str]) -> None:
    """Judgments already filled in survive a rerun, except for the cases that were rerun."""
    prior = parse_answers(path.read_text(encoding="utf-8")) if path.exists() else {}
    prior = {cid: fields for cid, fields in prior.items() if cid not in rerun}
    valid = [r for r in records if not needs_rerun(r)]
    path.write_text(render_sheet(valid, prior), encoding="utf-8")


# ---------------------------------------------------------------- real dependencies

_ARTICLE_COLUMNS = """
    SELECT a.url_key, a.article_id, m.story_id, a.kind, p.kind, a.event_at
    FROM news.article a
    JOIN news.publisher p ON p.publisher_id = a.publisher_id
    LEFT JOIN news.story_member m ON m.article_id = a.article_id
"""


def lookup_by_url_keys(cur, keys: list[str]) -> dict[str, dict]:
    cur.execute(_ARTICLE_COLUMNS + "WHERE a.url_key = ANY(%s)", (list(keys),))
    return {
        key: {"article_id": article_id, "story_id": story_id, "kind": kind, "publisher_kind": publisher_kind,
              "event_at": event_at.isoformat() if event_at else None}
        for key, article_id, story_id, kind, publisher_kind, event_at in cur.fetchall()
    }


def lookup_story_ids(cur, article_ids: list[int]) -> dict[int, int | None]:
    cur.execute(_ARTICLE_COLUMNS + "WHERE a.article_id = ANY(%s)", (list(article_ids),))
    return {article_id: story_id for _, article_id, story_id, *_ in cur.fetchall()}


def lookup_discussion_stories(cur, keys: list[str]) -> dict[str, int | None]:
    discussions = fetch_discussions_by_url_keys(cur, keys)
    stories = lookup_story_ids(cur, [d.article_id for d in discussions])
    return {url_key(d.url): stories.get(d.article_id) for d in discussions}


class _FollowStore:
    def add(self, user_key: str, query: str) -> int:
        from agent.memory import add_follow

        return add_follow(user_key, query).follow_id

    def set_checked(self, follow_id: int, when: datetime) -> None:
        from services.db import db_transaction

        with db_transaction() as (_, cur):
            cur.execute("UPDATE public.user_follow SET last_checked_at = %s WHERE follow_id = %s", (when, follow_id))

    def delete(self, user_key: str) -> None:
        from services.db import db_transaction

        with db_transaction() as (_, cur):
            cur.execute("DELETE FROM public.user_follow WHERE user_key = %s", (user_key,))


def real_deps(env_file: str) -> Deps:
    from agent import run_turn

    def read(fn, arg):
        with readonly_cursor(env_file) as cur:
            return fn(cur, arg)

    def ask(question: str, *, request_id: str, user_key: str | None = None):
        return run_turn(question, [], request_id=request_id, thread_id=request_id, user_key=user_key)

    return Deps(
        ask=ask,
        lookup_urls=lambda keys: read(lookup_by_url_keys, keys),
        lookup_discussions=lambda keys: read(lookup_discussion_stories, keys),
        lookup_stories=lambda ids: read(lookup_story_ids, ids),
        follows=_FollowStore(),
    )


# ---------------------------------------------------------------- cli

def _load_runs(directory: Path) -> dict[str, list[dict]]:
    runs = {}
    for set_name in SETS:
        path = directory / "runs" / f"{set_name}.jsonl"
        if path.exists():
            by_id: dict[str, dict] = {}
            for record in _read_jsonl(path):
                by_id[record["case_id"]] = record
            runs[set_name] = list(by_id.values())
    return runs


def _write_report(directory: Path, sheet_path: Path) -> Path:
    runs = _load_runs(directory)
    stats = sheet_scores(sheet_path) if sheet_path.exists() else None
    report = directory / "report.md"
    report.write_text(build_report(runs, stats), encoding="utf-8")
    return report


def cmd_run(args: argparse.Namespace, deps: Deps | None = None) -> int:
    directory = args.dir.resolve()
    selected = SETS if args.set == "all" else (args.set,)
    loaded = {name: load_cases(directory / f"{name}.jsonl", name) for name in selected}
    if args.only_case_id:
        loaded = {n: [c for c in cs if c["case_id"] == args.only_case_id] for n, cs in loaded.items()}
        loaded = {n: cs for n, cs in loaded.items() if cs}
        if not loaded:
            print(f"No case matching case_id={args.only_case_id}")
            return 1
    deps = deps or real_deps(args.env_file)
    sheet_path = directory / "contrast_sheet.md"
    for name, cases in loaded.items():
        ran = run_set(name, cases, directory / "runs" / f"{name}.jsonl", deps, args.sleep_seconds)
        if name == "contrast":
            write_sheet(sheet_path, _load_runs(directory)["contrast"], set(ran))
    report = _write_report(directory, sheet_path)
    print("=" * 60)
    print(f"Done; report={report}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    directory = args.dir.resolve()
    sheet_path = directory / "contrast_sheet.md"
    if not sheet_path.exists():
        raise SystemExit(f"Judgment sheet not found: {sheet_path}\nRun the contrast set first.")
    for field, score in sheet_scores(sheet_path).items():
        print(f"{field}：{score['flagged']} 条（已判断 {score['judged']} / {score['total']}），通过条件为 0")
    print(f"report={_write_report(directory, sheet_path)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Tracking-task acceptance runner.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    score = commands.add_parser("score")
    for command in (run, score):
        command.add_argument("--dir", type=Path, default=here, help="Folder with the three sets, runs/ and the outputs.")
    run.add_argument("--set", choices=(*SETS, "all"), default="all")
    run.add_argument("--only-case-id", type=str, default=None)
    run.add_argument("--sleep-seconds", type=float, default=2.0, help="Delay between cases (RPM quota).")
    add_db_args(run)
    args = parser.parse_args(argv)
    if args.command == "score":
        return cmd_score(args)
    load_env(args.env_file)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
