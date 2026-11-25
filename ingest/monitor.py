"""Daily contract metrics over the last 24 hours, judged against the 14-day medians, with an alert email. The
discovery-lag rule looks at the last 7 days, since official channels publish only a few articles a week."""

from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timedelta, timezone
from statistics import median

from ingest import store
from common.mail import _send

logger = logging.getLogger(__name__)

BASELINE_DAYS = 14
MIN_BASELINE_DAYS = 7
FAILING_STREAK = 3
LAG_DAYS = 7
LAG_MIN_ARTICLES = 3
LAG_GRACE_MINUTES = 120
RUN_HOUR = 9
_BEIJING = timezone(timedelta(hours=8))

UNLABELED = "可检索文章中 topics 或 kind 为空"
TOO_LONG = f"摘要超过 {store.SUMMARY_TOO_LONG_CHARS} 字符的比例"
VOLUME = "新增可检索文章数"
ANALYZE_FAILED = "analyze 阶段失败率"
DEGRADED = "正文降级率"
FAILING_CHANNELS = "连续失败不少于 3 次的渠道"
DISCOVERY_LAG = f"首次发现比发布时间晚超过轮询间隔加 {LAG_GRACE_MINUTES // 60} 小时的渠道"


def _ratio(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def too_long_rate(c: store.WindowCounts) -> float:
    return _ratio(c.too_long, c.ready)


def analyze_failed_rate(c: store.WindowCounts) -> float:
    return _ratio(c.analyze_failed, c.articles)


def degraded_rate(c: store.WindowCounts) -> float:
    return _ratio(c.degraded, c.fetched)


_RATE_RULES = (
    (TOO_LONG, too_long_rate, 0.05),
    (ANALYZE_FAILED, analyze_failed_rate, 0.12),
    (DEGRADED, degraded_rate, 0.15),
)


def evaluate(
    current: store.WindowCounts,
    baseline: list[store.WindowCounts],
    failing_channels: list[str],
    lagging_channels: list[tuple[str, float, int]],
) -> list[str]:
    """Breached rules as "name：detail". baseline holds one entry per whole day of polling, at most 14."""
    breaches = []
    if current.unlabeled > 0:
        breaches.append(f"{UNLABELED}：{current.unlabeled} 篇，上限 0")
    if len(baseline) >= MIN_BASELINE_DAYS:
        for name, rate, floor in _RATE_RULES:
            base = median(rate(day) for day in baseline)
            limit = max(2 * base, floor)
            if rate(current) > limit:
                breaches.append(f"{name}：{rate(current):.1%}，上限 {limit:.1%}（14 天中位数 {base:.1%}）")
        base_ready = median(day.ready for day in baseline)
        if current.ready < base_ready / 2:
            breaches.append(f"{VOLUME}：{current.ready}，下限为 14 天中位数 {base_ready:g} 的一半")
    if failing_channels:
        breaches.append(f"{FAILING_CHANNELS}：{', '.join(failing_channels)}")
    if lagging_channels:
        details = "；".join(f"{key} 中位数 {minutes / 60:.1f} 小时（{count} 篇）" for key, minutes, count in lagging_channels)
        breaches.append(f"{DISCOVERY_LAG}：{details}")
    return breaches


def metrics_line(
    current: store.WindowCounts,
    baseline: list[store.WindowCounts],
    failing_channels: list[str],
    lagging_channels: list[tuple[str, float, int]],
) -> str:
    line = (
        f"窗口内文章 {current.articles}，可检索 {current.ready}，topics 或 kind 为空 {current.unlabeled}，"
        f"摘要超长 {current.too_long}（{too_long_rate(current):.1%}），"
        f"analyze 失败 {current.analyze_failed}（{analyze_failed_rate(current):.1%}），"
        f"正文降级 {current.degraded}/{current.fetched}（{degraded_rate(current):.1%}），"
        f"连续失败渠道 {', '.join(failing_channels) or '无'}，"
        f"发现滞后渠道 {', '.join(key for key, _, _ in lagging_channels) or '无'}；基线 {len(baseline)} 天"
    )
    if baseline:
        line += (
            f"，中位数：可检索 {median(day.ready for day in baseline):g}，"
            f"摘要超长 {median(too_long_rate(day) for day in baseline):.1%}，"
            f"analyze 失败 {median(analyze_failed_rate(day) for day in baseline):.1%}，"
            f"正文降级 {median(degraded_rate(day) for day in baseline):.1%}"
        )
    return line


def due_day(now: datetime, done_on: date | None) -> date | None:
    """The Beijing date when the round at now is the first one after 9:00 that day, otherwise None."""
    local = now.astimezone(_BEIJING)
    if local.hour < RUN_HOUR or local.date() == done_on:
        return None
    return local.date()


def run_monitor(*, transaction: store.Transaction, dry_run: bool = False) -> list[str]:
    with transaction() as cur:
        polled_days, windows = store.monitor_counts(cur, BASELINE_DAYS + 1)
        failing = store.failing_channels(cur, FAILING_STREAK)
        lagging = store.lagging_channels(
            cur, days=LAG_DAYS, min_articles=LAG_MIN_ARTICLES, grace_minutes=LAG_GRACE_MINUTES
        )
    empty = store.WindowCounts()
    current = windows.get(0, empty)
    baseline = [windows.get(day, empty) for day in range(1, min(polled_days, BASELINE_DAYS) + 1)]
    breaches = evaluate(current, baseline, failing, lagging)
    line = metrics_line(current, baseline, failing, lagging)
    logger.info("monitor: %s", line)
    if breaches:
        logger.warning("monitor 越限：%s", "；".join(breaches))
        if not dry_run:
            items = "".join(f"<li>{html.escape(breach)}</li>" for breach in breaches)
            _send(
                os.getenv("ADMIN_EMAIL", ""),
                f"[TechNews] 质量监控 {len(breaches)} 项越限",
                f"<p>越限项：</p><ul>{items}</ul><p>当日指标：{html.escape(line)}</p>",
            )
    return breaches
