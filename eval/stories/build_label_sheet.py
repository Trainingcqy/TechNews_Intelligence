"""Label sheet for the story-merge threshold (plan 28.1, "故事归并阈值校准").

Read-only. Samples article pairs from `news.story_member` (an article and its recorded nearest neighbour)
by `nearest_similarity` bin and writes

- a blind Markdown sheet (similarity is not shown): fill `- 同一事件：[ ]` with `y` or `n` per pair;
- a key file with each pair's similarity, group and the bin populations, which compute_threshold.py needs.

Same-language pairs are sampled `--per-bin` per bin; cross-language pairs are sampled separately,
`--cross-n` in total spread evenly over the same bins.

    python eval/stories/build_label_sheet.py --sheet runs/pair_sheet.md --key runs/pair_key.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.helpers import add_db_args, answer_line, configure_stdout, readonly_cursor  # noqa: E402

SAME, CROSS = "same", "cross"
LO, HI = 0.75, 0.95


def load_pairs(cur, min_similarity: float = LO) -> list[dict]:
    """Unordered pairs (article, recorded nearest neighbour) with the language group.

    Pairs where either article has no language are left out: they belong to neither group.
    """
    cur.execute(
        """
        SELECT DISTINCT ON (LEAST(m.article_id, m.nearest_article_id), GREATEST(m.article_id, m.nearest_article_id))
               m.article_id, m.nearest_article_id, m.nearest_similarity, a.lang <> b.lang
        FROM news.story_member m
        JOIN news.article a ON a.article_id = m.article_id
        JOIN news.article b ON b.article_id = m.nearest_article_id
        WHERE m.nearest_similarity >= %s AND a.lang IS NOT NULL AND b.lang IS NOT NULL
        ORDER BY LEAST(m.article_id, m.nearest_article_id), GREATEST(m.article_id, m.nearest_article_id), m.article_id
        """,
        (min_similarity,),
    )
    return [{"a": a, "b": b, "similarity": float(sim), "group": CROSS if cross else SAME} for a, b, sim, cross in cur.fetchall()]


def bin_edges(width: float, lo: float = LO, hi: float = HI) -> list[float]:
    count = round((hi - lo) / width)
    return [round(lo + i * width, 4) for i in range(count)]


def sample_pairs(pairs: list[dict], *, width: float, per_bin: int, cross_n: int, seed: int) -> tuple[list[dict], list[dict]]:
    """Returns (sampled pairs with their bin, bin populations incl. the open top bin of each group)."""
    rng = random.Random(seed)
    edges = bin_edges(width)
    top = round(edges[-1] + width, 4)
    cross_per_bin = math.ceil(cross_n / len(edges))
    sampled, bins = [], []
    for group, quota in ((SAME, per_bin), (CROSS, cross_per_bin)):
        group_pairs = sorted((p for p in pairs if p["group"] == group), key=lambda p: (p["a"], p["b"]))
        for lo in edges:
            hi = round(lo + width, 4)
            members = [p for p in group_pairs if lo <= p["similarity"] < hi]
            bins.append({"group": group, "lo": lo, "hi": hi, "population": len(members)})
            sampled += [{**p, "bin_lo": lo} for p in rng.sample(members, min(quota, len(members)))]
        bins.append({"group": group, "lo": top, "hi": None, "population": sum(1 for p in group_pairs if p["similarity"] >= top)})
    rng.shuffle(sampled)
    return sampled, bins


def load_articles(cur, ids: list[int]) -> dict[int, dict]:
    cur.execute(
        "SELECT a.article_id, a.title, a.title_zh, a.summary, a.lang, a.event_at, a.url, p.name "
        "FROM news.article a JOIN news.publisher p ON p.publisher_id = a.publisher_id WHERE a.article_id = ANY(%s)",
        (ids,),
    )
    return {row[0]: {"title": row[1], "title_zh": row[2], "summary": row[3], "lang": row[4], "event_at": row[5],
                     "url": row[6], "publisher": row[7]} for row in cur.fetchall()}


def _article_lines(label: str, article: dict) -> list[str]:
    event_at: datetime = article["event_at"]
    return [f"**{label}** {article['publisher']} · {article['lang']} · {event_at:%Y-%m-%d %H:%M} · {article['url']}", "",
            f"{article['title_zh'] or article['title']}（原题：{article['title']}）", "", f"{article['summary'] or ''}", ""]


def render_sheet(sampled: list[dict], articles: dict[int, dict]) -> str:
    out = ["# 故事归并标注表", "", f"共 {len(sampled)} 对。判断每对文章是否在报道同一个事件：同一事件的报道填 `y`；"
           "同一条故事线上的不同进展、或只是话题相近的，填 `n`；拿不准留空或填 `?`。", "", "---", ""]
    for number, pair in enumerate(sampled, start=1):
        out += [f"## {number}. `{pair['pair_id']}`", ""]
        out += _article_lines("A", articles[pair["a"]]) + _article_lines("B", articles[pair["b"]])
        out += [answer_line("同一事件"), "", "---", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    here = Path(__file__).parent / "runs"
    parser = argparse.ArgumentParser(description="Sample article pairs for the story threshold calibration.")
    parser.add_argument("--bin-width", type=float, default=0.02)
    parser.add_argument("--per-bin", type=int, default=20)
    parser.add_argument("--cross-n", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sheet", type=Path, default=here / "pair_sheet.md")
    parser.add_argument("--key", type=Path, default=here / "pair_key.json")
    add_db_args(parser)
    args = parser.parse_args(argv)

    with readonly_cursor(args.env_file) as cur:
        pairs = load_pairs(cur)
        sampled, bins = sample_pairs(pairs, width=args.bin_width, per_bin=args.per_bin, cross_n=args.cross_n, seed=args.seed)
        for number, pair in enumerate(sampled, start=1):
            pair["pair_id"] = f"P{number:03d}"
        articles = load_articles(cur, sorted({i for p in sampled for i in (p["a"], p["b"])}))
    args.sheet.parent.mkdir(parents=True, exist_ok=True)
    args.sheet.write_text(render_sheet(sampled, articles), encoding="utf-8")
    key = {"params": {"bin_width": args.bin_width, "per_bin": args.per_bin, "cross_n": args.cross_n, "seed": args.seed},
           "bins": bins, "pairs": sampled}
    args.key.write_text(json.dumps(key, indent=1), encoding="utf-8")
    print(f"{len(pairs)} pairs with similarity >= {LO}; sampled {len(sampled)} -> {args.sheet}, {args.key}")
    for group in (SAME, CROSS):
        print(group, "sampled", sum(1 for p in sampled if p["group"] == group),
              "population", sum(b["population"] for b in bins if b["group"] == group))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
