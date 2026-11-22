"""Story-level checks (plan 28.1, "故事级检查"): span and size distributions, and a sheet for judging whether
the largest and the longest stories are single events.

Read-only.

    python eval/stories/story_spans.py report --sheet runs/story_sheet.md [--since 2026-11-01T00:00:00+00:00]
    python eval/stories/story_spans.py score  --sheet runs/story_sheet.md
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.helpers import add_db_args, answer_line, configure_stdout, parse_time, pct, read_choices, readonly_cursor  # noqa: E402

MULTI_EVENT_LIMIT = 0.10
SIZE_BUCKETS = ((1, 1), (2, 2), (3, 3), (4, 5), (6, 10), (11, None))
SPAN_BUCKETS_DAYS = (0, 1, 2, 3, 4, 5, 6, 7)  # a span goes into the bucket of its floor in days; > 7 days into the last one


@dataclass(frozen=True)
class Story:
    story_id: int
    size: int
    publishers: int
    first_at: datetime
    last_at: datetime

    @property
    def span(self) -> timedelta:
        return self.last_at - self.first_at


def load_stories(cur, since: datetime | None) -> list[Story]:
    cur.execute(
        """
        SELECT m.story_id, count(*), count(DISTINCT a.publisher_id), min(a.event_at), max(a.event_at)
        FROM news.story_member m JOIN news.article a ON a.article_id = m.article_id
        GROUP BY m.story_id
        HAVING %(since)s::timestamptz IS NULL OR min(a.event_at) >= %(since)s
        """,
        {"since": since},
    )
    return [Story(*row) for row in cur.fetchall()]


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    return sorted_values[max(0, math.ceil(len(sorted_values) * q) - 1)]


def size_histogram(stories: list[Story]) -> list[tuple[str, int]]:
    rows = []
    for lo, hi in SIZE_BUCKETS:
        label = f"{lo}" if lo == hi else f"{lo}+" if hi is None else f"{lo}-{hi}"
        rows.append((label, sum(1 for s in stories if s.size >= lo and (hi is None or s.size <= hi))))
    return rows


def span_histogram(stories: list[Story]) -> list[tuple[str, int]]:
    counts = {d: 0 for d in SPAN_BUCKETS_DAYS}
    for story in stories:
        counts[min(int(story.span.total_seconds() // 86400), SPAN_BUCKETS_DAYS[-1])] += 1
    return [(f"{d} 至 {d + 1} 天" if d < SPAN_BUCKETS_DAYS[-1] else f"{d} 天及以上", counts[d]) for d in SPAN_BUCKETS_DAYS]


def pick_stories(stories: list[Story], k: int) -> list[Story]:
    """The k largest by member count and the k longest by span, each story once; ties by story_id."""
    largest = sorted(stories, key=lambda s: (-s.size, s.story_id))[:k]
    longest = sorted(stories, key=lambda s: (-s.span, s.story_id))[:k]
    picked = {s.story_id: s for s in largest + longest}
    return sorted(picked.values(), key=lambda s: s.story_id)


def load_members(cur, story_ids: list[int]) -> dict[int, list[tuple]]:
    cur.execute(
        """
        SELECT m.story_id, a.event_at, p.name, a.lang, COALESCE(a.title_zh, a.title), a.url
        FROM news.story_member m JOIN news.article a ON a.article_id = m.article_id
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        WHERE m.story_id = ANY(%s) ORDER BY m.story_id, a.event_at, a.article_id
        """,
        (story_ids,),
    )
    members: dict[int, list[tuple]] = {}
    for story_id, *rest in cur.fetchall():
        members.setdefault(story_id, []).append(tuple(rest))
    return members


def render_sheet(picked: list[Story], members: dict[int, list[tuple]]) -> str:
    out = ["# 故事级检查表", "", f"共 {len(picked)} 个故事（成员最多与跨度最长的各取一批，重复的只列一次）。"
           "判断每个故事的成员是否都在报道同一个事件：是填 `y`，包含多个事件填 `n`。", "", "---", ""]
    for number, story in enumerate(picked, start=1):
        out += [f"## {number}. `S{story.story_id}`", "",
                f"成员 {story.size} 篇，发布方 {story.publishers} 个，跨度 {story.span.total_seconds() / 86400:.1f} 天", ""]
        out += [f"- {event_at:%m-%d %H:%M} · {publisher} · {lang} · {title}" for event_at, publisher, lang, title, _ in members[story.story_id]]
        out += ["", answer_line("单一事件"), "", "---", ""]
    return "\n".join(out)


def build_distribution_report(stories: list[Story]) -> str:
    if not stories:
        return "没有故事。\n"
    sizes = sorted(s.size for s in stories)
    spans = sorted(s.span.total_seconds() / 86400 for s in stories)
    multi = [s for s in stories if s.size > 1]
    out = ["# 故事分布", "", f"故事 {len(stories)} 个，其中不止一个成员的 {len(multi)} 个（{pct(len(multi), len(stories))}）。", "",
           "| | 中位数 | p90 | p99 | 最大 |", "| --- | --- | --- | --- | --- |",
           f"| 成员数 | {percentile(sizes, .5):.0f} | {percentile(sizes, .9):.0f} | {percentile(sizes, .99):.0f} | {sizes[-1]} |",
           f"| 跨度（天） | {percentile(spans, .5):.2f} | {percentile(spans, .9):.2f} | {percentile(spans, .99):.2f} | {spans[-1]:.2f} |",
           "", "| 成员数 | 故事数 |", "| --- | --- |"]
    out += [f"| {label} | {count} |" for label, count in size_histogram(stories)]
    out += ["", "| 跨度 | 故事数 |", "| --- | --- |"]
    out += [f"| {label} | {count} |" for label, count in span_histogram(stories)]
    return "\n".join(out) + "\n"


def score(answers: dict[str, str | None]) -> str:
    judged = [v for v in answers.values() if v is not None]
    multi = sum(v == "n" for v in judged)
    ratio = multi / len(judged) if judged else 0.0
    verdict = "超过 10%，启用第 30 节的故事锚点校验" if ratio > MULTI_EVENT_LIMIT else "未超过 10%，不启用故事锚点校验"
    return f"已判断 {len(judged)} / {len(answers)} 个故事，包含多个事件的 {multi} 个（{pct(multi, len(judged))}），{verdict}。\n"


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    here = Path(__file__).parent / "runs"
    parser = argparse.ArgumentParser(description="Story size/span distributions and single-event check.")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("--since", type=parse_time, help="only stories whose first member is at or after this time")
    report.add_argument("--k", type=int, default=20)
    report.add_argument("--sheet", type=Path, default=here / "story_sheet.md")
    add_db_args(report)
    score_cmd = commands.add_parser("score")
    score_cmd.add_argument("--sheet", type=Path, default=here / "story_sheet.md")
    args = parser.parse_args(argv)

    if args.command == "report":
        with readonly_cursor(args.env_file) as cur:
            stories = load_stories(cur, args.since)
            picked = pick_stories(stories, args.k)
            members = load_members(cur, [s.story_id for s in picked])
        print(build_distribution_report(stories))
        args.sheet.parent.mkdir(parents=True, exist_ok=True)
        args.sheet.write_text(render_sheet(picked, members), encoding="utf-8")
        print(f"wrote {len(picked)} stories -> {args.sheet}")
    else:
        print(score(read_choices(args.sheet, "单一事件")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
