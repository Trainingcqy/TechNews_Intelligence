"""Sampling for the taxonomy trial (plan 28.1, "分类试标").

Read-only. Reads the old tables (the old category exists only as the `[标签]` prefix of `title_cn`) and writes

- sample.json: the full sample with the old categories, the strata, the pairs and the stratum populations;
- sample_keys.txt: one url_key per line, the list `python -m ingest relabel` is to relabel.

Strata (an article may belong to several; `strata` lists them):

- `category`: `--per-category` random articles per old category (plan: 50 each, 300 in total);
- `lab`: `--lab-n` random articles whose title names an AI lab or model, drawn from all categories, so the
  old share of `ai` in this stratum is an unbiased estimate of the plan's baseline (53.8%);
- `pair`: both articles of every pair with similarity >= `--pair-sim` and at most `--pair-gap-days` apart,
  within the last `--pair-days` days (plan: 109 pairs over 75 days).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.helpers import add_db_args, configure_stdout, parse_time, pct, readonly_cursor  # noqa: E402
from common.taxonomy import LEGACY_CATEGORY_MAP  # noqa: E402
from common.urls import url_key  # noqa: E402

# AI labs and model families; matched case-insensitively on word boundaries in the original title.
LAB_NAMES = (
    "OpenAI", "Anthropic", "DeepMind", "Mistral", "DeepSeek", "xAI", "Moonshot", "Cohere", "Perplexity",
    "MiniMax", "Zhipu", "Hugging Face",
    "ChatGPT", "GPT", "Claude", "Gemini", "Llama", "Qwen", "Grok", "Kimi", "Gemma", "GLM", "Sora", "Codex", "Copilot",
)
_LAB_RE = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in LAB_NAMES) + r")\b", re.I)
_CATEGORY_RE = re.compile(r"^\[([^\]]*)\]")
OLD_CATEGORIES = tuple(LEGACY_CATEGORY_MAP)


def old_category(title_cn: str | None) -> str | None:
    match = _CATEGORY_RE.match(title_cn or "")
    return match.group(1) if match and match.group(1) in OLD_CATEGORIES else None


def mentions_lab(title: str | None) -> bool:
    return bool(_LAB_RE.search(title or ""))


def load_articles(cur, asof: datetime) -> list[dict]:
    cur.execute(
        "SELECT id, url, title, title_cn, summary, created_at FROM public.tech_news "
        "WHERE created_at AT TIME ZONE 'UTC' < %s ORDER BY id",
        (asof,),
    )
    rows = []
    for article_id, url, title, title_cn, summary, created_at in cur.fetchall():
        category = old_category(title_cn)
        if category is None:
            continue
        rows.append({
            "id": article_id, "url": url, "url_key": url_key(url), "title": title, "title_cn": title_cn,
            "has_summary": bool((summary or "").strip()), "old_category": category,
            "lab": mentions_lab(title), "created_at": created_at.isoformat(),
        })
    return rows


def find_pairs(cur, asof: datetime, *, days: int, similarity: float, gap_days: int) -> list[tuple[int, int, float]]:
    """Pairs of old articles (both with a vector) whose summaries are near-duplicates and which are close in time."""
    cur.execute(
        """
        WITH n AS (
            SELECT t.id, t.created_at, e.embedding
            FROM public.tech_news t JOIN public.news_embeddings e ON e.url = t.url
            WHERE t.created_at AT TIME ZONE 'UTC' >= %(asof)s - make_interval(days => %(days)s)
              AND t.created_at AT TIME ZONE 'UTC' < %(asof)s
        )
        SELECT a.id, b.id, 1 - (a.embedding <=> b.embedding)
        FROM n a JOIN n b ON a.id < b.id AND abs(extract(epoch FROM a.created_at - b.created_at)) <= %(gap)s * 86400
        WHERE 1 - (a.embedding <=> b.embedding) >= %(sim)s
        ORDER BY a.id, b.id
        """,
        {"asof": asof, "days": days, "gap": gap_days, "sim": similarity},
    )
    return [(a, b, float(sim)) for a, b, sim in cur.fetchall()]


def draw_sample(articles: list[dict], pairs: list[tuple[int, int, float]], *, per_category: int, lab_n: int, seed: int) -> dict:
    rng = random.Random(seed)
    by_id = {a["id"]: a for a in articles}
    pairs = [p for p in pairs if p[0] in by_id and p[1] in by_id]  # articles without a recognised prefix are not in by_id
    strata: dict[int, set[str]] = {}

    category_population = Counter(a["old_category"] for a in articles)
    for category in OLD_CATEGORIES:
        pool = [a["id"] for a in articles if a["old_category"] == category and a["has_summary"]]
        for article_id in rng.sample(pool, min(per_category, len(pool))):
            strata.setdefault(article_id, set()).add("category")
    lab_pool = [a["id"] for a in articles if a["lab"] and a["has_summary"]]
    for article_id in rng.sample(lab_pool, min(lab_n, len(lab_pool))):
        strata.setdefault(article_id, set()).add("lab")
    for a, b, _ in pairs:
        for article_id in (a, b):
            strata.setdefault(article_id, set()).add("pair")

    sample_articles = []
    for article_id in sorted(strata):
        row = by_id[article_id]
        sample_articles.append({
            "id": article_id, "url": row["url"], "url_key": row["url_key"], "title": row["title"],
            "old_category": row["old_category"], "strata": sorted(strata[article_id]),
        })
    lab_all = [a for a in articles if a["lab"]]
    return {
        "category_population": {c: category_population.get(c, 0) for c in OLD_CATEGORIES},
        "lab_population": len(lab_all),
        "lab_population_old_ai": sum(1 for a in lab_all if a["old_category"] == "AI"),
        "articles": sample_articles,
        "pairs": [{"a": a, "b": b, "similarity": round(sim, 4)} for a, b, sim in pairs],
    }


def old_pair_agreement(sample: dict) -> tuple[int, int]:
    category = {a["id"]: a["old_category"] for a in sample["articles"]}
    same = sum(1 for p in sample["pairs"] if category[p["a"]] == category[p["b"]])
    return same, len(sample["pairs"])


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description="Sample articles for the taxonomy trial.")
    parser.add_argument("--asof", type=parse_time, help="only articles written before this time (default: now)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--per-category", type=int, default=50)
    parser.add_argument("--lab-n", type=int, default=200)
    parser.add_argument("--pair-days", type=int, default=75)
    parser.add_argument("--pair-sim", type=float, default=0.94)
    parser.add_argument("--pair-gap-days", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "runs" / "sample.json")
    add_db_args(parser)
    args = parser.parse_args(argv)
    asof = args.asof or datetime.now(timezone.utc)

    with readonly_cursor(args.env_file) as cur:
        articles = load_articles(cur, asof)
        pairs = find_pairs(cur, asof, days=args.pair_days, similarity=args.pair_sim, gap_days=args.pair_gap_days)
    sample = draw_sample(articles, pairs, per_category=args.per_category, lab_n=args.lab_n, seed=args.seed)
    sample["params"] = {k: v if not isinstance(v, (Path, datetime)) else str(v) for k, v in vars(args).items() if k != "env_file"}
    sample["params"]["asof"] = asof.isoformat()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    keys_path = args.out.with_name("sample_keys.txt")
    keys_path.write_text("".join(a["url_key"] + "\n" for a in sample["articles"]), encoding="utf-8")

    same, total = old_pair_agreement(sample)
    print(f"articles in sample: {len(sample['articles'])} -> {args.out}, {keys_path}")
    print("category population:", sample["category_population"])
    print(f"lab-name articles: {sample['lab_population']}, old category AI: "
          f"{sample['lab_population_old_ai']} ({pct(sample['lab_population_old_ai'], sample['lab_population'])})")
    print(f"pairs: {total}, same old category: {same} ({pct(same, total)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
