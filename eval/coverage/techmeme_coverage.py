"""Source coverage against the Techmeme reference channel (plan 28.1, "来源覆盖").

Read-only. Run once the reference channel has accumulated 7 days of headlines.

    python eval/coverage/techmeme_coverage.py report      --since ... --until ...   # URL hit rate, outlets, ranking
    python eval/coverage/techmeme_coverage.py event-sheet --since ... --until ...   # needs JINA_API_KEY; run by the user
    python eval/coverage/techmeme_coverage.py event-score --sheet runs/event_sheet.md --key runs/event_key.json

Story importance signals follow plan 10.3 and are computed here, together with the candidate ranking rules.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import add_db_args, answer_line, choice, configure_stdout, parse_answers, parse_time, pct, readonly_cursor  # noqa: E402

REFERENCE_CHANNEL = "techmeme"
EVENT_WINDOW_DAYS = 2
EVENT_CANDIDATES = 3
TOP_K = 20
PUBLISHER_WEIGHTS = (0.3, 0.5, 0.7)
JINA_EMBED_URL = "https://api.jina.ai/v1/embeddings"
JINA_MODEL = "jina-embeddings-v3"


@dataclass(frozen=True)
class Headline:
    headline_id: int
    title: str
    outlet: str | None
    target_url_key: str | None
    at: datetime
    article_id: int | None  # the article with url_key = target_url_key, if any
    status: str | None
    story_id: int | None


@dataclass(frozen=True)
class Story:
    story_id: int
    publisher_count: int
    has_official: bool
    hn_score: int | None
    hn_comments: int | None
    first_at: datetime
    last_at: datetime
    days: frozenset[date]


def load_headlines(cur, since: datetime, until: datetime) -> list[Headline]:
    cur.execute(
        """
        SELECT h.headline_id, h.title, h.outlet, h.target_url_key, COALESCE(h.published_at, h.first_seen_at),
               a.article_id, a.status, m.story_id
        FROM news.reference_headline h
        JOIN news.channel c ON c.channel_id = h.channel_id AND c.key = %(channel)s
        LEFT JOIN news.article a ON a.url_key = h.target_url_key
        LEFT JOIN news.story_member m ON m.article_id = a.article_id
        WHERE COALESCE(h.published_at, h.first_seen_at) >= %(since)s AND COALESCE(h.published_at, h.first_seen_at) < %(until)s
        ORDER BY 5, h.headline_id
        """,
        {"channel": REFERENCE_CHANNEL, "since": since, "until": until},
    )
    return [Headline(*row) for row in cur.fetchall()]


def load_stories(cur, since: datetime, until: datetime) -> list[Story]:
    """Signals of every story with a retrievable member in the window, aggregated over all its retrievable members."""
    cur.execute(
        """
        WITH win AS (
            SELECT DISTINCT m.story_id FROM news.story_member m
            JOIN news.article a ON a.article_id = m.article_id
            WHERE a.status = 'ready' AND a.event_at >= %(since)s AND a.event_at < %(until)s
        )
        SELECT m.story_id, count(DISTINCT a.publisher_id), bool_or(p.kind = 'official'),
               max(s.score), max(s.comment_count), min(a.event_at), max(a.event_at),
               array_agg(DISTINCT (a.event_at AT TIME ZONE 'UTC')::date)
        FROM win JOIN news.story_member m ON m.story_id = win.story_id
        JOIN news.article a ON a.article_id = m.article_id AND a.status = 'ready'
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        LEFT JOIN news.sighting s ON s.article_id = a.article_id
             AND s.channel_id IN (SELECT channel_id FROM news.channel WHERE fetch_kind = 'hn')
        GROUP BY m.story_id
        """,
        {"since": since, "until": until},
    )
    return [Story(sid, pubs, official, score, comments, first, last, frozenset(days))
            for sid, pubs, official, score, comments, first, last, days in cur.fetchall()]


# Ranking rules. Each maps the stories of one day to a sort key; the smaller key ranks first.
def _recency(story: Story) -> float:
    return -story.last_at.timestamp()


def three_level_key(stories: list[Story]) -> Callable[[Story], tuple]:
    """Plan 10.3: publisher_count desc, hn_score desc with nulls last, last_at desc."""
    return lambda s: (-s.publisher_count, s.hn_score is None, -(s.hn_score or 0), _recency(s), s.story_id)


def hn_first_key(stories: list[Story]) -> Callable[[Story], tuple]:
    return lambda s: (s.hn_score is None, -(s.hn_score or 0), -s.publisher_count, _recency(s), s.story_id)


def weighted_key(weight: float) -> Callable[[list[Story]], Callable[[Story], tuple]]:
    """weight * publisher_count + (1 - weight) * hn_score, each divided by the day's maximum."""

    def make(stories: list[Story]) -> Callable[[Story], tuple]:
        max_pub = max(s.publisher_count for s in stories)
        max_hn = max((s.hn_score or 0) for s in stories) or 1
        return lambda s: (-(weight * s.publisher_count / max_pub + (1 - weight) * (s.hn_score or 0) / max_hn), _recency(s), s.story_id)

    return make


RULES: dict[str, Callable[[list[Story]], Callable[[Story], tuple]]] = {
    "three_level": three_level_key,
    "hn_first": hn_first_key,
    **{f"weighted_{w}": weighted_key(w) for w in PUBLISHER_WEIGHTS},
}


def day_rank(stories: list[Story], story_id: int, day: date, rule: str) -> int | None:
    """1-based rank of the story among the stories that have a member on `day`; None if it has none that day."""
    that_day = [s for s in stories if day in s.days]
    if not any(s.story_id == story_id for s in that_day):
        return None
    ordered = sorted(that_day, key=RULES[rule](that_day))
    return next(i for i, s in enumerate(ordered, start=1) if s.story_id == story_id)


def headline_day(headline: Headline) -> date:
    return headline.at.astimezone(timezone.utc).date()


def rank_table(headlines: list[Headline], stories: list[Story]) -> tuple[dict[str, dict], list[tuple[Headline, dict[str, int | None]]]]:
    """Per rule: headlines in the top K. Only URL hits that belong to a story with retrievable members are ranked;
    a story with no member on the headline's day counts as outside the top K."""
    ranked = [h for h in headlines if h.story_id is not None and any(s.story_id == h.story_id for s in stories)]
    rows = [(h, {rule: day_rank(stories, h.story_id, headline_day(h), rule) for rule in RULES}) for h in ranked]
    summary = {rule: {"top": sum(1 for _, r in rows if r[rule] is not None and r[rule] <= TOP_K), "n": len(rows)} for rule in RULES}
    return summary, rows


def missing_outlets(headlines: list[Headline]) -> list[tuple[str, int]]:
    return Counter(h.outlet or "(unknown)" for h in headlines if h.article_id is None).most_common()


def build_report(headlines: list[Headline], stories: list[Story], since: datetime, until: datetime) -> str:
    hits = [h for h in headlines if h.article_id is not None]
    out = ["# 来源覆盖", "", f"窗口：{since.isoformat()} 至 {until.isoformat()}；头条 {len(headlines)} 条，"
           f"其中没有原文地址的 {sum(h.target_url_key is None for h in headlines)} 条（计为未命中）。", "",
           f"地址命中率：{len(hits)} / {len(headlines)} = {pct(len(hits), len(headlines))}", "",
           "## 缺失媒体（地址未命中的头条，按 Techmeme 标注的媒体计数）", "", "| 媒体 | 条数 |", "| --- | --- |"]
    out += [f"| {outlet} | {count} |" for outlet, count in missing_outlets(headlines)]
    summary, rows = rank_table(hits, stories)
    unranked = len(hits) - len(rows)
    out += ["", f"## 头条所属故事的名次（日期按 UTC）", "",
            f"地址命中的 {len(hits)} 条中 {len(rows)} 条属于有可检索成员的故事，参与排名；其余 {unranked} 条的文章尚未成为 `ready` 或没有故事。", "",
            f"| 规则 | 进入前 {TOP_K} 名 | 比例 |", "| --- | --- | --- |"]
    out += [f"| {rule} | {v['top']} / {v['n']} | {pct(v['top'], v['n'])} |" for rule, v in summary.items()]
    if summary:
        best = max(summary, key=lambda rule: summary[rule]["top"])
        out += ["", f"比例最高的规则：{best}"]
    out += ["", "| 日期 | 头条 | 媒体 | " + " | ".join(RULES) + " |", "| --- | --- | --- | " + " | ".join("---" for _ in RULES) + " |"]
    for h, ranks in rows:
        out.append(f"| {headline_day(h)} | {h.title.replace('|', '/')[:80]} | {h.outlet or ''} | "
                   + " | ".join("-" if ranks[r] is None else str(ranks[r]) for r in RULES) + " |")
    return "\n".join(out) + "\n"


def jina_query_embeddings(texts: list[str]) -> list[list[float]]:
    import requests

    key = os.getenv("JINA_API_KEY", "")
    if not key:
        raise RuntimeError("JINA_API_KEY is not set")
    vectors: list[list[float]] = []
    for start in range(0, len(texts), 32):
        response = requests.post(
            JINA_EMBED_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": JINA_MODEL, "task": "retrieval.query", "input": texts[start:start + 32]},
            timeout=60,
        )
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors += [item["embedding"] for item in data]
    return vectors


def nearest_stories(cur, vector: list[float], at: datetime) -> list[dict]:
    """The EVENT_CANDIDATES stories nearest to the query vector among members with event_at within the window around `at`;
    a story's similarity is that of its closest member."""
    cur.execute(
        """
        WITH hit AS (
            SELECT m.story_id, a.article_id, a.event_at, COALESCE(a.title_zh, a.title) AS title, a.publisher_id,
                   1 - (e.embedding <=> %(q)s::vector) AS sim
            FROM news.article a
            JOIN news.article_embedding e ON e.article_id = a.article_id
            JOIN news.story_member m ON m.article_id = a.article_id
            WHERE a.status = 'ready'
              AND a.event_at BETWEEN %(at)s - make_interval(days => %(w)s) AND %(at)s + make_interval(days => %(w)s)
        ), best AS (
            SELECT story_id, max(sim) AS sim FROM hit GROUP BY story_id ORDER BY max(sim) DESC, story_id LIMIT %(k)s
        )
        SELECT b.story_id, b.sim, h.event_at, p.name, h.title
        FROM best b JOIN hit h ON h.story_id = b.story_id JOIN news.publisher p ON p.publisher_id = h.publisher_id
        ORDER BY b.sim DESC, b.story_id, h.sim DESC, h.article_id
        """,
        {"q": "[" + ",".join(repr(float(x)) for x in vector) + "]", "at": at, "w": EVENT_WINDOW_DAYS, "k": EVENT_CANDIDATES},
    )
    stories: dict[int, dict] = {}
    for story_id, sim, event_at, publisher, title in cur.fetchall():
        story = stories.setdefault(story_id, {"story_id": story_id, "similarity": float(sim), "members": []})
        story["members"].append({"event_at": event_at, "publisher": publisher, "title": title})
    return list(stories.values())


def render_event_sheet(items: list[tuple[Headline, list[dict]]]) -> str:
    out = ["# 事件命中标注表", "", f"共 {len(items)} 条地址未命中的头条。每条头条给出库内与它最接近的 {EVENT_CANDIDATES} 个故事，"
           "判断该故事是否报道了与头条同一个事件：是填 `y`，不是填 `n`，拿不准留空。", "", "---", ""]
    for number, (headline, candidates) in enumerate(items, start=1):
        out += [f"## {number}. `H{headline.headline_id}`", "", f"**头条：** {headline.title}（{headline.outlet or '-'}，{headline.at:%Y-%m-%d %H:%M}）", ""]
        for index in range(1, EVENT_CANDIDATES + 1):
            if index <= len(candidates):
                out += [f"**候选{index}：**"] + [f"- {m['event_at']:%m-%d %H:%M} · {m['publisher']} · {m['title']}" for m in candidates[index - 1]["members"][:3]] + [""]
            out.append(answer_line(f"候选{index}"))
            out.append("")
        out += ["---", ""]
    return "\n".join(out)


def event_hit_rate(answers: dict[str, dict[str, str | None]]) -> tuple[int, int]:
    """(headlines with a candidate marked y, headlines with at least one answered candidate)."""
    judged = hit = 0
    for fields in answers.values():
        values = [v for v in fields.values() if v is not None]
        judged += bool(values)
        hit += "y" in values
    return hit, judged


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    here = Path(__file__).parent / "runs"
    parser = argparse.ArgumentParser(description="Source coverage against the Techmeme reference headlines.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("report", "event-sheet"):
        command = commands.add_parser(name)
        command.add_argument("--since", type=parse_time)
        command.add_argument("--until", type=parse_time)
        command.add_argument("--days", type=int, default=7)
        add_db_args(command)
    commands.choices["report"].add_argument("--out", type=Path)
    commands.choices["event-sheet"].add_argument("--sheet", type=Path, default=here / "event_sheet.md")
    commands.choices["event-sheet"].add_argument("--key", type=Path, default=here / "event_key.json")
    score = commands.add_parser("event-score")
    score.add_argument("--sheet", type=Path, default=here / "event_sheet.md")
    score.add_argument("--key", type=Path, default=here / "event_key.json")
    args = parser.parse_args(argv)

    if args.command == "event-score":
        key = json.loads(args.key.read_text(encoding="utf-8"))
        answers = {item: {name: choice(value) for name, value in fields.items()}
                   for item, fields in parse_answers(args.sheet.read_text(encoding="utf-8")).items()}
        hit, judged = event_hit_rate(answers)
        total = key["total"]
        print(f"地址命中 {key['url_hits']} / {total} = {pct(key['url_hits'], total)}")
        print(f"事件命中率：{hit} / {judged} = {pct(hit, judged)}（地址未命中 {total - key['url_hits']} 条，已判断 {judged} 条）")
        if judged == total - key["url_hits"]:
            print(f"两者合计覆盖：{key['url_hits'] + hit} / {total} = {pct(key['url_hits'] + hit, total)}")
        return 0

    until = args.until or datetime.now(timezone.utc)
    since = args.since or until - timedelta(days=args.days)
    with readonly_cursor(args.env_file) as cur:
        headlines = load_headlines(cur, since, until)
        if args.command == "report":
            report = build_report(headlines, load_stories(cur, since, until), since, until)
            print(report)
            if args.out:
                args.out.write_text(report, encoding="utf-8")
            return 0
        missed = [h for h in headlines if h.article_id is None]
        vectors = jina_query_embeddings([h.title for h in missed])
        items = [(h, nearest_stories(cur, vector, h.at)) for h, vector in zip(missed, vectors)]
    args.sheet.parent.mkdir(parents=True, exist_ok=True)
    args.sheet.write_text(render_event_sheet(items), encoding="utf-8")
    args.key.write_text(json.dumps({"total": len(headlines), "url_hits": len(headlines) - len(missed)}), encoding="utf-8")
    print(f"wrote {len(items)} headlines -> {args.sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
