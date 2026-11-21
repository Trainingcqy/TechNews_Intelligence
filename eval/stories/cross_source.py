"""Cross-source overlap and translation share (plan 28.1, "跨来源重叠率" and "转译占比").

Read-only.

    python eval/stories/cross_source.py overlap [--days 14]                  # after the new channels ran 14 days
    python eval/stories/cross_source.py sheet --sheet runs/translation_sheet.md [--n 50] [--days 14]
    python eval/stories/cross_source.py score --sheet runs/translation_sheet.md
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import add_db_args, answer_line, configure_stdout, parse_time, pct, read_choices, readonly_cursor  # noqa: E402

REVIEW_MIN_ARTICLES = 20
TRANSLATION_MAJORITY = 0.5

# Retrievable (ready) articles of the window that belong to a story, with the story's number of publishers
# and whether another publisher reported it.
_ELIGIBLE = """
WITH eligible AS (
    SELECT a.article_id, a.publisher_id, m.story_id,
           EXISTS (SELECT 1 FROM news.story_member m2
                   JOIN news.article a2 ON a2.article_id = m2.article_id AND a2.status = 'ready'
                   WHERE m2.story_id = m.story_id AND a2.publisher_id <> a.publisher_id) AS overlapped
    FROM news.article a JOIN news.story_member m ON m.article_id = a.article_id
    WHERE a.status = 'ready' AND a.event_at >= %(since)s AND a.event_at < %(until)s
)
"""


def overall_overlap(cur, since: datetime, until: datetime) -> tuple[int, int]:
    cur.execute(_ELIGIBLE + "SELECT count(*), count(*) FILTER (WHERE overlapped) FROM eligible", {"since": since, "until": until})
    total, overlapped = cur.fetchone()
    return total, overlapped


def channel_overlap(cur, since: datetime, until: datetime) -> list[tuple[str, int, int]]:
    """(channel key, articles sighted in the channel, of those reported by another publisher) per corpus channel."""
    cur.execute(
        _ELIGIBLE
        + """
        SELECT c.key, count(DISTINCT e.article_id), count(DISTINCT e.article_id) FILTER (WHERE e.overlapped)
        FROM news.channel c
        LEFT JOIN news.sighting s ON s.channel_id = c.channel_id
        LEFT JOIN eligible e ON e.article_id = s.article_id
        WHERE c.purpose = 'corpus' AND c.enabled
        GROUP BY c.key ORDER BY c.key
        """,
        {"since": since, "until": until},
    )
    return cur.fetchall()


def review_channels(rows: list[tuple[str, int, int]]) -> list[str]:
    return [key for key, articles, overlapped in rows if articles >= REVIEW_MIN_ARTICLES and overlapped == 0]


def render_overlap(total: tuple[int, int], channels: list[tuple[str, int, int]], since: datetime, until: datetime) -> str:
    out = ["# 跨来源重叠率", "", f"窗口：{since.isoformat()} 至 {until.isoformat()}", "",
           f"整体：{total[1]} / {total[0]} = {pct(total[1], total[0])} 的可检索文章属于多发布方故事。", "",
           "| 渠道 | 文章数 | 有其他发布方同故事报道 | 重叠率 |", "| --- | --- | --- | --- |"]
    out += [f"| {key} | {n} | {k} | {pct(k, n)} |" for key, n, k in channels]
    flagged = review_channels(channels)
    out += ["", f"复核对象（不少于 {REVIEW_MIN_ARTICLES} 篇而重叠率为 0）：" + (", ".join(flagged) if flagged else "无"), "",
            "一篇文章被两个渠道发现时在两个渠道行中各计一次。"]
    return "\n".join(out) + "\n"


def cross_language_members(cur, since: datetime | None) -> dict[int, dict[str, list[dict]]]:
    """Members of stories that have both a zh and an en article, grouped by story and language."""
    cur.execute(
        """
        SELECT m.story_id, a.article_id, a.lang, a.title, a.title_zh, a.summary, a.url, p.name, a.event_at
        FROM news.story_member m
        JOIN news.article a ON a.article_id = m.article_id AND a.status = 'ready' AND a.lang IN ('zh', 'en')
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        WHERE m.story_id IN (
            SELECT m2.story_id FROM news.story_member m2
            JOIN news.article a2 ON a2.article_id = m2.article_id AND a2.status = 'ready'
            GROUP BY m2.story_id HAVING count(DISTINCT a2.lang) FILTER (WHERE a2.lang IN ('zh', 'en')) = 2
        )
          AND (%(since)s::timestamptz IS NULL OR a.event_at >= %(since)s)
        ORDER BY m.story_id, a.article_id
        """,
        {"since": since},
    )
    stories: dict[int, dict[str, list[dict]]] = {}
    for story_id, article_id, lang, title, title_zh, summary, url, publisher, event_at in cur.fetchall():
        item = {"article_id": article_id, "title": title, "title_zh": title_zh, "summary": summary, "url": url,
                "publisher": publisher, "event_at": event_at}
        stories.setdefault(story_id, {}).setdefault(lang, []).append(item)
    return {sid: langs for sid, langs in stories.items() if "zh" in langs and "en" in langs}


def draw_pairs(stories: dict[int, dict[str, list[dict]]], n: int, seed: int) -> list[tuple[int, dict, dict]]:
    """One (zh, en) pair per story, for n random stories, so a large story does not dominate the sample."""
    rng = random.Random(seed)
    chosen = rng.sample(sorted(stories), min(n, len(stories)))
    return [(sid, rng.choice(stories[sid]["zh"]), rng.choice(stories[sid]["en"])) for sid in sorted(chosen)]


def render_sheet(pairs: list[tuple[int, dict, dict]]) -> str:
    out = ["# 转译占比标注表", "", f"共 {len(pairs)} 对：每对是同一故事里的一篇中文报道（ZH）和一篇英文报道（EN）。"
           "判断中文一篇是否只是英文报道的翻译或改写，没有自己的采访、数据或补充：是填 `y`，不是填 `n`。", "", "---", ""]
    for number, (story_id, zh, en) in enumerate(pairs, start=1):
        out += [f"## {number}. `T{story_id}`", ""]
        for label, article in (("ZH", zh), ("EN", en)):
            out += [f"**{label}** {article['publisher']} · {article['event_at']:%Y-%m-%d %H:%M} · {article['url']}", "",
                    f"{article['title_zh'] or article['title']}（原题：{article['title']}）", "", f"{article['summary'] or ''}", ""]
        out += [answer_line("中文为转译或改写"), "", "---", ""]
    return "\n".join(out)


def score(answers: dict[str, str | None]) -> str:
    judged = [v for v in answers.values() if v is not None]
    translated = sum(v == "y" for v in judged)
    ratio = translated / len(judged) if judged else 0.0
    verdict = ("超过一半，`publisher_count` 改为按语言分别计数后取较大值" if ratio > TRANSLATION_MAJORITY
               else "未超过一半，`publisher_count` 保持按发布方计数")
    return f"已判断 {len(judged)} / {len(answers)} 对，中文为转译或改写的 {translated} 对（{pct(translated, len(judged))}），{verdict}。\n"


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    here = Path(__file__).parent / "runs"
    parser = argparse.ArgumentParser(description="Cross-source overlap and translation share.")
    commands = parser.add_subparsers(dest="command", required=True)
    overlap = commands.add_parser("overlap")
    sheet = commands.add_parser("sheet")
    for command in (overlap, sheet):
        command.add_argument("--since", type=parse_time)
        command.add_argument("--until", type=parse_time)
        command.add_argument("--days", type=int, default=14)
        add_db_args(command)
    sheet.add_argument("--n", type=int, default=50)
    sheet.add_argument("--seed", type=int, default=42)
    sheet.add_argument("--sheet", type=Path, default=here / "translation_sheet.md")
    score_cmd = commands.add_parser("score")
    score_cmd.add_argument("--sheet", type=Path, default=here / "translation_sheet.md")
    args = parser.parse_args(argv)

    if args.command == "score":
        print(score(read_choices(args.sheet, "中文为转译或改写")))
        return 0
    until = args.until or datetime.now(timezone.utc)
    since = args.since or until - timedelta(days=args.days)
    with readonly_cursor(args.env_file) as cur:
        if args.command == "overlap":
            print(render_overlap(overall_overlap(cur, since, until), channel_overlap(cur, since, until), since, until))
        else:
            pairs = draw_pairs(cross_language_members(cur, since), args.n, args.seed)
            args.sheet.parent.mkdir(parents=True, exist_ok=True)
            args.sheet.write_text(render_sheet(pairs), encoding="utf-8")
            print(f"wrote {len(pairs)} pairs -> {args.sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
