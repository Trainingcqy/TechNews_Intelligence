"""Phase-1 pipeline comparison (plan 28.1): old n8n pipeline vs the new ingest pipeline on the shared channels.

Read-only. Run after the new pipeline has shadowed the old one for 3 days:

    python eval/pipeline/compare_shadow.py --since 2026-11-20T00:00:00+00:00 --until 2026-11-23T00:00:00+00:00
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import add_db_args, configure_stdout, parse_time, pct, readonly_cursor  # noqa: E402
from services.urls import url_key  # noqa: E402

# Old source_key -> new channel key (plan 12.1).
OLD_TO_NEW = {
    "hackernews_frontpage": "hn_frontpage",
    "techcrunch_feed": "techcrunch",
    "microsoft_ai_blog": "microsoft_blog",
}
ANALYSIS_FAILED = "AI Analysis Failed"
FETCH_FAILED = "Content Fetch Failed"


@dataclass(frozen=True)
class OldRow:
    url_key: str
    source_key: str | None
    outcome: str  # ok | analysis_failed | fetch_failed | other_failed


@dataclass(frozen=True)
class NewRow:
    url_key: str
    status: str
    body_level: str
    failed_stage: str | None
    channels: tuple[str, ...]


def _outcome(error_reason: str | None) -> str:
    if error_reason is None:
        return "ok"
    return {ANALYSIS_FAILED: "analysis_failed", FETCH_FAILED: "fetch_failed"}.get(error_reason, "other_failed")


def old_rows(cur, since: datetime, until: datetime, old_keys: list[str] | None) -> list[OldRow]:
    """Candidates of the old pipeline: rows written to tech_news plus rows written to tech_news_failed.

    tech_news.created_at is a naive UTC timestamp; tech_news_failed.created_at is timestamptz.
    """
    cur.execute(
        """
        SELECT url, source_key, NULL::text AS error_reason
        FROM public.tech_news
        WHERE created_at AT TIME ZONE 'UTC' >= %(since)s AND created_at AT TIME ZONE 'UTC' < %(until)s
          AND (%(keys)s::text[] IS NULL OR source_key = ANY(%(keys)s))
        UNION ALL
        SELECT url, source_key, error_reason
        FROM public.tech_news_failed
        WHERE created_at >= %(since)s AND created_at < %(until)s
          AND (%(keys)s::text[] IS NULL OR source_key = ANY(%(keys)s))
        """,
        {"since": since, "until": until, "keys": old_keys},
    )
    return [OldRow(url_key(url), source_key, _outcome(reason)) for url, source_key, reason in cur.fetchall()]


def new_rows(cur, since: datetime, until: datetime, channel_keys: list[str]) -> list[NewRow]:
    """Candidates of the new pipeline: articles first written in the window that a given channel has sighted."""
    cur.execute(
        """
        SELECT a.url_key, a.status, a.body_level,
               (SELECT f.stage FROM news.stage_failure f WHERE f.article_id = a.article_id
                ORDER BY f.failure_id DESC LIMIT 1) AS failed_stage,
               ARRAY(SELECT DISTINCT c.key FROM news.sighting s JOIN news.channel c ON c.channel_id = s.channel_id
                     WHERE s.article_id = a.article_id AND c.key = ANY(%(keys)s) ORDER BY c.key) AS channels
        FROM news.article a
        WHERE a.first_seen_at >= %(since)s AND a.first_seen_at < %(until)s
          AND EXISTS (SELECT 1 FROM news.sighting s JOIN news.channel c ON c.channel_id = s.channel_id
                      WHERE s.article_id = a.article_id AND c.key = ANY(%(keys)s))
        """,
        {"since": since, "until": until, "keys": channel_keys},
    )
    return [NewRow(key, status, body, stage, tuple(channels)) for key, status, body, stage, channels in cur.fetchall()]


def old_stats(rows: list[OldRow]) -> dict[str, dict[str, int]]:
    """Row counts per source_key plus a 'total' entry; the old pipeline's candidate count is its row count."""
    stats: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        stats[row.source_key or "(none)"].update(candidates=1, **{row.outcome: 1})
        stats["total"].update(candidates=1, **{row.outcome: 1})
    return {key: dict(value) for key, value in stats.items()}


def new_stats(rows: list[NewRow]) -> dict[str, dict[str, int]]:
    """Counts per channel (an article sighted in two channels counts in both) plus a 'total' of distinct articles."""
    stats: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        counts = Counter(candidates=1)
        if row.status == "failed" and row.failed_stage == "analyze":
            counts["analysis_failed"] += 1
        if row.status == "discovered":
            counts["pending"] += 1
        elif row.body_level != "full":
            counts["body_unavailable"] += 1
        for channel in row.channels:
            stats[channel].update(counts)
        stats["total"].update(counts)
    return {key: dict(value) for key, value in stats.items()}


def missing_reason(first_seen_at: datetime | None, since: datetime, until: datetime) -> str:
    """Why an address the old pipeline has is not a new-pipeline candidate of the window.

    `absent`: no row in news.article. `earlier`/`later`: the article was first written outside the window
    (migration or an earlier sighting by another channel). `other_channel`: written in the window, but none
    of the compared channels sighted it.
    """
    if first_seen_at is None:
        return "absent"
    if first_seen_at < since:
        return "earlier"
    if first_seen_at >= until:
        return "later"
    return "other_channel"


def article_lookup(cur, keys: list[str]) -> dict[str, datetime]:
    if not keys:
        return {}
    cur.execute("SELECT url_key, first_seen_at FROM news.article WHERE url_key = ANY(%s)", (keys,))
    return dict(cur.fetchall())


def poll_health(cur, since: datetime, until: datetime, channel_keys: list[str]) -> list[tuple]:
    cur.execute(
        """
        SELECT c.key, count(p.poll_id), count(p.poll_id) FILTER (WHERE NOT p.ok),
               max(p.polled_at) FILTER (WHERE p.ok)
        FROM news.channel c
        LEFT JOIN news.channel_poll p ON p.channel_id = c.channel_id AND p.polled_at >= %(since)s AND p.polled_at < %(until)s
        WHERE c.key = ANY(%(keys)s)
        GROUP BY c.key ORDER BY c.key
        """,
        {"since": since, "until": until, "keys": channel_keys},
    )
    return cur.fetchall()


def failure_distribution(cur, since: datetime, until: datetime) -> list[tuple]:
    cur.execute(
        """
        SELECT stage, error_kind, count(*), (array_agg(detail ORDER BY failure_id DESC))[1]
        FROM news.stage_failure
        WHERE created_at >= %(since)s AND created_at < %(until)s
        GROUP BY stage, error_kind ORDER BY count(*) DESC, stage, error_kind
        """,
        {"since": since, "until": until},
    )
    return cur.fetchall()


def _rate_cells(stats: dict[str, int], field: str) -> str:
    count, total = stats.get(field, 0), stats.get("candidates", 0)
    return f"{count} / {total} = {pct(count, total)}"


def build_report(cur, since: datetime, until: datetime, *, old_keys: list[str], new_keys: list[str]) -> str:
    old = old_rows(cur, since, until, old_keys)
    new = new_rows(cur, since, until, new_keys)
    old_s, new_s = old_stats(old), new_stats(new)
    out = [
        "# 管线对比",
        "",
        f"窗口：{since.isoformat()} 至 {until.isoformat()}；渠道：{', '.join(new_keys)}",
        "",
        "## 分析失败率与正文不可用率",
        "",
        "| 范围 | 旧：候选 | 旧：分析失败率 | 旧：正文失败率 | 新：候选 | 新：分析失败率 | 新：正文不可用率 | 新：未取正文 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for old_key, new_key in [*OLD_TO_NEW.items(), ("total", "total")]:
        if old_key not in old_keys and old_key != "total":
            continue
        o, n = old_s.get(old_key, {}), new_s.get(new_key, {})
        out.append(
            f"| {new_key} | {o.get('candidates', 0)} | {_rate_cells(o, 'analysis_failed')} | {_rate_cells(o, 'fetch_failed')} "
            f"| {n.get('candidates', 0)} | {_rate_cells(n, 'analysis_failed')} | {_rate_cells(n, 'body_unavailable')} "
            f"| {n.get('pending', 0)} |"
        )
    out += ["", "渠道行中同时出现在两个渠道的新管线文章按渠道各计一次，total 行按文章去重。"]
    o, n = old_s.get("total", {}), new_s.get("total", {})
    if o.get("candidates") and n.get("candidates"):
        old_a, new_a = o.get("analysis_failed", 0) / o["candidates"], n.get("analysis_failed", 0) / n["candidates"]
        old_b, new_b = o.get("fetch_failed", 0) / o["candidates"], n.get("body_unavailable", 0) / n["candidates"]
        out += [
            "",
            f"- 分析失败率：新 {100 * new_a:.1f}% {'低于' if new_a < old_a else '不低于'}旧 {100 * old_a:.1f}%，"
            f"{'通过' if new_a < old_a else '未通过'}",
            f"- 正文不可用率：新 {100 * new_b:.1f}% {'不高于' if new_b <= old_b else '高于'}旧 {100 * old_b:.1f}%，"
            f"{'通过' if new_b <= old_b else '未通过'}",
        ]

    old_keys_set = {row.url_key: row for row in old}
    new_keys_set = {row.url_key for row in new}
    missing = sorted(set(old_keys_set) - new_keys_set)
    lookup = article_lookup(cur, missing)
    out += ["", "## 候选覆盖", "", f"旧管线地址 {len(old_keys_set)} 个，新管线 {len(new_keys_set)} 个；"
            f"旧有而新无 {len(missing)} 个，新有而旧无 {len(new_keys_set - set(old_keys_set))} 个。"]
    if missing:
        reasons = Counter(missing_reason(lookup.get(key), since, until) for key in missing)
        out += ["", "原因汇总：" + "，".join(f"{reason} {count}" for reason, count in sorted(reasons.items())), "",
                "| 地址 | 旧渠道 | 旧结果 | 原因 |", "| --- | --- | --- | --- |"]
        for key in missing:
            row = old_keys_set[key]
            out.append(f"| {key} | {row.source_key} | {row.outcome} | {missing_reason(lookup.get(key), since, until)} |")
        out += ["", "渠道轮询情况（`absent` 的原因要从这里和渠道自身的行为里找）：", "",
                "| 渠道 | 轮询次数 | 失败次数 | 最近成功 |", "| --- | --- | --- | --- |"]
        for key, polls, failed, last_ok in poll_health(cur, since, until, new_keys):
            out.append(f"| {key} | {polls} | {failed} | {last_ok.isoformat() if last_ok else '-'} |")
    out += ["", "## 失败类型分布（窗口内全部渠道）", "", "| 阶段 | error_kind | 条数 | 最近一条 detail |", "| --- | --- | --- | --- |"]
    for stage, kind, count, detail in failure_distribution(cur, since, until):
        out.append(f"| {stage} | {kind} | {count} | {(detail or '').replace(chr(10), ' ')[:100].replace('|', '/')} |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description="Old vs new pipeline comparison on the shared channels.")
    parser.add_argument("--since", type=parse_time, help="window start, ISO 8601 (default: until minus --days)")
    parser.add_argument("--until", type=parse_time, help="window end, ISO 8601 (default: now)")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--out", type=Path)
    add_db_args(parser)
    args = parser.parse_args(argv)
    until = args.until or datetime.now(timezone.utc)
    since = args.since or until - timedelta(days=args.days)
    with readonly_cursor(args.env_file) as cur:
        report = build_report(cur, since, until, old_keys=list(OLD_TO_NEW), new_keys=list(OLD_TO_NEW.values()))
    print(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
