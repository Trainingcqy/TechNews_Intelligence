"""Precision and recall of the story-merge threshold from the labeled pair sheet (plan 28.1).

Reads the filled sheet and the key file written by build_label_sheet.py. The sample is stratified by
similarity bin, so every labeled pair is weighted by its bin's population divided by the labeled pairs
in that bin. Pairs above the top bin (>= 0.95) are not sampled and are counted as same-event; pairs below
0.75 are not in the population, so recall is relative to the positives at or above 0.75.

    python eval/stories/compute_threshold.py --sheet runs/pair_sheet.md --key runs/pair_key.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import configure_stdout, read_choices  # noqa: E402

GROUPS = ("same", "cross")
SEPARATE_IF_GAP_OVER = 0.03
EPS = 1e-9


def thresholds(lo: float = 0.75, hi: float = 0.95, step: float = 0.01) -> list[float]:
    return [round(lo + i * step, 4) for i in range(round((hi - lo) / step) + 1)]


def weighted_pairs(pairs: list[dict], labels: dict[str, str | None], bins: list[dict], group: str) -> list[tuple[float, bool, float]]:
    """(similarity, same event, weight) for the labeled pairs of a group, plus one entry per unsampled top-bin pair."""
    labeled = [p for p in pairs if p["group"] == group and labels.get(p["pair_id"]) is not None]
    drawn = Counter(p["bin_lo"] for p in labeled)
    population = {b["lo"]: b["population"] for b in bins if b["group"] == group}
    items = [(p["similarity"], labels[p["pair_id"]] == "y", population[p["bin_lo"]] / drawn[p["bin_lo"]]) for p in labeled]
    top = next(b for b in bins if b["group"] == group and b["hi"] is None)
    if top["population"]:
        items.append((top["lo"], True, float(top["population"])))
    return items


def precision_recall(items: list[tuple[float, bool, float]], threshold: float) -> tuple[float | None, float | None]:
    predicted = sum(w for sim, _, w in items if sim >= threshold - EPS)
    true_positive = sum(w for sim, same, w in items if sim >= threshold - EPS and same)
    positives = sum(w for _, same, w in items if same)
    return (true_positive / predicted if predicted else None, true_positive / positives if positives else None)


def f1(precision: float | None, recall: float | None) -> float:
    return 2 * precision * recall / (precision + recall) if precision and recall else 0.0


def best_threshold(items: list[tuple[float, bool, float]]) -> float:
    """The threshold with the highest F1; the lowest one on ties."""
    return max(thresholds(), key=lambda t: (f1(*precision_recall(items, t)), -t))


def build_report(pairs: list[dict], labels: dict[str, str | None], bins: list[dict]) -> str:
    out = ["# 故事归并阈值", "",
           f"已标 {sum(v is not None for v in labels.values())} / {len(labels)} 对。按相似度分段的总体数量加权；"
           "0.95 及以上未抽样，按同一事件计；0.75 以下不在总体内。", ""]
    best = {}
    for group in GROUPS:
        items = weighted_pairs(pairs, labels, bins, group)
        out += [f"## {'同语言' if group == 'same' else '跨语言'}", "", "| 阈值 | 精确率 | 召回率 | F1 |", "| --- | --- | --- | --- |"]
        for t in thresholds():
            p, r = precision_recall(items, t)
            out.append(f"| {t:.2f} | {'-' if p is None else f'{100 * p:.1f}%'} | {'-' if r is None else f'{100 * r:.1f}%'} | {100 * f1(p, r):.1f}% |")
        best[group] = best_threshold(items)
        out += ["", f"F1 最高的阈值：{best[group]:.2f}", ""]
    gap = abs(best["same"] - best["cross"])
    out.append(f"跨语言与同语言的最优阈值相差 {gap:.2f}，" + ("超过 0.03，两者分别设阈值。" if gap > SEPARATE_IF_GAP_OVER + EPS else "不超过 0.03，共用一个阈值。"))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    here = Path(__file__).parent / "runs"
    parser = argparse.ArgumentParser(description="Story threshold precision/recall from labeled pairs.")
    parser.add_argument("--sheet", type=Path, default=here / "pair_sheet.md")
    parser.add_argument("--key", type=Path, default=here / "pair_key.json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    key = json.loads(args.key.read_text(encoding="utf-8"))
    labels = read_choices(args.sheet, "同一事件")
    report = build_report(key["pairs"], labels, key["bins"])
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
