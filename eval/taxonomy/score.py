"""Statistics for the taxonomy trial (plan 28.1, "分类试标").

Read-only. Reads the sample written by build_sample.py and, from `news.article`, the labels
`python -m ingest relabel` wrote for it (`label_source = 'model'`). Two subcommands:

    python eval/taxonomy/score.py sheet  --sample runs/sample.json --out runs/spot_check.md   # 100-article sheet
    python eval/taxonomy/score.py report --sample runs/sample.json [--sheet runs/spot_check.md]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import add_db_args, answer_line, configure_stdout, pct, read_choices, readonly_cursor  # noqa: E402
from services.taxonomy import KIND_SCOPES, KINDS, TOPIC_SCOPES, TOPICS  # noqa: E402

LAB_AI_MIN = 0.95
PRIMARY_PAIR_MIN = 0.90
KIND_PAIR_RECORD_MIN = 0.80
GENERAL_MAX = 0.15
KEY_SHARE_MIN = 0.02
SPOT_PRIMARY_MIN = 0.90
SPOT_KIND_MIN = 0.85


def load_labels(cur, url_keys: list[str]) -> dict[str, dict]:
    """Relabeled articles only: label_source = 'model' with a non-empty topics list."""
    cur.execute(
        "SELECT url_key, topics, kind, title_zh, summary FROM news.article "
        "WHERE url_key = ANY(%s) AND label_source = 'model' AND cardinality(topics) > 0",
        (url_keys,),
    )
    return {key: {"topics": topics, "kind": kind, "title_zh": title_zh, "summary": summary}
            for key, topics, kind, title_zh, summary in cur.fetchall()}


def labeled_articles(sample: dict, labels: dict[str, dict]) -> dict[int, dict]:
    return {a["id"]: {**a, **labels[a["url_key"]]} for a in sample["articles"] if a["url_key"] in labels}


def category_weights(categories: list[str], population: dict[str, int]) -> list[float]:
    """Inverse-probability weights for a sample with a fixed number of articles per old category."""
    drawn = Counter(categories)
    return [population[c] / drawn[c] for c in categories]


def weighted_shares(values: list[str], weights: list[float]) -> dict[str, float]:
    total = sum(weights)
    shares: Counter = Counter()
    for value, weight in zip(values, weights):
        shares[value] += weight / total
    return dict(shares)


def lab_ai_share(sample: dict, labeled: dict[int, dict]) -> tuple[int, int, int]:
    """(articles with `ai` among their topics, relabeled lab articles, of those with old category AI)."""
    lab = [a for a in labeled.values() if "lab" in a["strata"]]
    return (sum("ai" in a["topics"] for a in lab), len(lab), sum(a["old_category"] == "AI" for a in lab))


def pair_agreement(sample: dict, labeled: dict[int, dict]) -> dict:
    """Agreement over the pairs whose two articles are both relabeled."""
    old = {a["id"]: a["old_category"] for a in sample["articles"]}
    result = {"pairs": 0, "primary": 0, "kind": 0, "old": 0, "primary_diff": Counter(), "kind_diff": Counter()}
    for pair in sample["pairs"]:
        a, b = labeled.get(pair["a"]), labeled.get(pair["b"])
        if a is None or b is None:
            continue
        result["pairs"] += 1
        result["old"] += old[pair["a"]] == old[pair["b"]]
        if a["topics"][0] == b["topics"][0]:
            result["primary"] += 1
        else:
            result["primary_diff"][tuple(sorted((a["topics"][0], b["topics"][0])))] += 1
        if a["kind"] == b["kind"]:
            result["kind"] += 1
        else:
            result["kind_diff"][tuple(sorted((a["kind"], b["kind"])))] += 1
    return result


def spot_check_ids(sample: dict, labeled: dict[int, dict], n: int, seed: int) -> list[int]:
    pool = sorted(i for i, a in labeled.items() if "category" in a["strata"])
    return sorted(random.Random(seed).sample(pool, min(n, len(pool))))


def render_spot_sheet(sample: dict, labeled: dict[int, dict], ids: list[int]) -> str:
    out = ["# 分类抽检表", "", f"共 {len(ids)} 篇。判断模型标注的主领域（topics 的第一个）和类型是否正确，在方括号内填 `y` 或 `n`。", "",
           "领域：", ""]
    out += [f"- `{key}` {name}：{TOPIC_SCOPES[key]}" for key, name in TOPICS.items()]
    out += ["", "类型：", ""]
    out += [f"- `{key}` {name}：{KIND_SCOPES[key]}" for key, name in KINDS.items()]
    out += ["", "---", ""]
    for number, article_id in enumerate(ids, start=1):
        a = labeled[article_id]
        out += [f"## {number}. `A{article_id}`", "", f"**标题：** {a['title']}", "", f"**中文标题：** {a['title_zh']}", "",
                f"**摘要：** {a['summary']}", "", f"**模型标注：** topics = {a['topics']}，kind = {a['kind']}", "",
                answer_line("主领域正确"), answer_line("类型正确"), "", "---", ""]
    return "\n".join(out)


def spot_check_rates(sample: dict, labeled: dict[int, dict], sheet: Path) -> dict:
    """Weighted rates over the answered items of the sheet; blank or `?` items are excluded."""
    result = {}
    for field, name in (("主领域正确", "primary"), ("类型正确", "kind")):
        answers = read_choices(sheet, field)
        items = [(int(item_id[1:]), answer == "y") for item_id, answer in answers.items() if answer is not None]
        weights = category_weights([labeled[i]["old_category"] for i, _ in items], sample["category_population"])
        correct = sum(w for (_, ok), w in zip(items, weights) if ok)
        result[name] = {"answered": len(items), "total": len(answers), "rate": correct / sum(weights) if items else None}
    return result


def _line(name: str, value: float | None, threshold: float, *, at_least: bool = True) -> str:
    if value is None:
        return f"- {name}：无数据"
    ok = value >= threshold if at_least else value < threshold
    return f"- {name}：{100 * value:.1f}%，通过条件 {'不低于' if at_least else '低于'} {100 * threshold:.0f}%，{'通过' if ok else '未通过'}"


def build_report(sample: dict, labeled: dict[int, dict], sheet: Path | None) -> str:
    out = ["# 分类试标统计", "", f"样本 {len(sample['articles'])} 篇，已重标 {len(labeled)} 篇（`label_source = 'model'`）。"
           "未重标的篇不计入下列统计。", ""]

    ai, n, old_ai = lab_ai_share(sample, labeled)
    out += ["## 标题含 AI 实验室或模型名的文章", "",
            f"`lab` 层已重标 {n} 篇；全库同类文章旧分类为 AI 的比例 {pct(sample['lab_population_old_ai'], sample['lab_population'])}"
            f"（{sample['lab_population_old_ai']}/{sample['lab_population']}）；这 {n} 篇的旧分类为 AI 的比例 {pct(old_ai, n)}。",
            _line("带 `ai` 标签的比例", ai / n if n else None, LAB_AI_MIN), ""]

    pairs = pair_agreement(sample, labeled)
    total = len(sample["pairs"])
    out += ["## 同事件文章对", "", f"样本共 {total} 对，两篇都已重标的 {pairs['pairs']} 对；这些对的旧分类一致率 {pct(pairs['old'], pairs['pairs'])}。"]
    if pairs["pairs"]:
        out += [_line("主领域一致率", pairs["primary"] / pairs["pairs"], PRIMARY_PAIR_MIN),
                f"- 类型一致率：{pct(pairs['kind'], pairs['pairs'])}"
                + ("，低于 80%，重写类型的范围描述" if pairs["kind"] / pairs["pairs"] < KIND_PAIR_RECORD_MIN else ""),
                "- 主领域不一致的键对：" + ("，".join(f"{'/'.join(k)} {v}" for k, v in pairs["primary_diff"].most_common()) or "无"),
                "- 类型不一致的键对：" + ("，".join(f"{'/'.join(k)} {v}" for k, v in pairs["kind_diff"].most_common()) or "无")]
    out.append("")

    category = [a for a in labeled.values() if "category" in a["strata"]]
    out += ["## 各键占比", "", f"按旧分类层（{len(category)} 篇）加权到全库的旧分类分布；主领域取 topics 的第一个。", ""]
    if category:
        weights = category_weights([a["old_category"] for a in category], sample["category_population"])
        topic_share = weighted_shares([a["topics"][0] for a in category], weights)
        kind_share = weighted_shares([a["kind"] for a in category], weights)
        out += ["| 主领域 | 占比 | 备注 |", "| --- | --- | --- |"]
        for key in TOPICS:
            share = topic_share.get(key, 0.0)
            note = "低于 2%，按 8.5 节并入范围最近的键" if share < KEY_SHARE_MIN else ""
            if key == "general":
                note = (note + "；" if note else "") + ("未低于 15%" if share >= GENERAL_MAX else "低于 15%，通过")
            out.append(f"| {key} | {100 * share:.1f}% | {note} |")
        out += ["", "| 类型 | 占比 |", "| --- | --- |"]
        out += [f"| {key} | {100 * kind_share.get(key, 0.0):.1f}% |" for key in KINDS]
    out.append("")

    if sheet is not None:
        spot = spot_check_rates(sample, labeled, sheet)
        out += ["## 人工抽检", "",
                f"已填 {spot['primary']['answered']}/{spot['primary']['total']}（主领域），{spot['kind']['answered']}/{spot['kind']['total']}（类型）；"
                "按旧分类层加权。", "",
                _line("主领域正确率", spot["primary"]["rate"], SPOT_PRIMARY_MIN),
                _line("类型正确率", spot["kind"]["rate"], SPOT_KIND_MIN), ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description="Taxonomy trial: spot-check sheet and statistics.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sheet", "report"):
        command = commands.add_parser(name)
        command.add_argument("--sample", type=Path, default=Path(__file__).parent / "runs" / "sample.json")
        add_db_args(command)
    commands.choices["sheet"].add_argument("--out", type=Path, default=Path(__file__).parent / "runs" / "spot_check.md")
    commands.choices["sheet"].add_argument("--n", type=int, default=100)
    commands.choices["sheet"].add_argument("--seed", type=int, default=42)
    commands.choices["report"].add_argument("--sheet", type=Path, help="the filled spot-check sheet")
    commands.choices["report"].add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    sample = json.loads(args.sample.read_text(encoding="utf-8"))
    with readonly_cursor(args.env_file) as cur:
        labeled = labeled_articles(sample, load_labels(cur, [a["url_key"] for a in sample["articles"]]))
    if args.command == "sheet":
        ids = spot_check_ids(sample, labeled, args.n, args.seed)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render_spot_sheet(sample, labeled, ids), encoding="utf-8")
        print(f"wrote {len(ids)} items -> {args.out}")
    else:
        report = build_report(sample, labeled, args.sheet)
        print(report)
        if args.out:
            args.out.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
