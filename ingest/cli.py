"""Subcommands of python -m ingest."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ingest import monitor, runner, store
from ingest.config import IngestConfig
from ingest.relabel import run_relabel
from ingest.sources import load_sources
from common.db import init_db_pool
from common.script_env import load_env


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync-sources")
    run = commands.add_parser("run")
    run.add_argument("--stage", choices=runner.STAGES)
    run.add_argument("--channel")
    loop = commands.add_parser("loop")
    loop.add_argument("--interval", type=int, default=300)
    commands.add_parser("status")
    requeue = commands.add_parser("requeue")
    requeue.add_argument("--status", choices=["failed"], required=True)
    requeue.add_argument("--error-kind")
    monitor_command = commands.add_parser("monitor")
    monitor_command.add_argument("--dry-run", action="store_true")
    relabel = commands.add_parser("relabel")
    relabel.add_argument("--limit", type=int)
    relabel.add_argument("--keys-file", type=Path)
    return parser


def _status() -> None:
    with runner.transaction() as cur:
        last_ok = store.channel_last_ok(cur)
        backlog = store.backlog_counts(cur)
        failures = store.recent_failure_counts(cur)
    print("渠道最近一次成功轮询:")
    for key, last_ok_at in last_ok:
        print(f"  {key}\t{last_ok_at.isoformat() if last_ok_at else '-'}")
    print("积压:")
    for status, count in backlog:
        print(f"  {status}\t{count}")
    print("近 24 小时失败:")
    for stage, error_kind, count in failures:
        print(f"  {stage}\t{error_kind}\t{count}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()
    config = IngestConfig.from_env()
    init_db_pool(maxconn=max(config.discover_workers, config.fetch_workers, config.analyze_workers) + 1)

    if args.command == "sync-sources":
        with runner.transaction() as cur:
            store.sync_sources(cur, load_sources())
    elif args.command == "run":
        if args.channel is not None:
            with runner.transaction() as cur:
                channel = store.enabled_channel(cur, args.channel)
            if channel is None:
                print(f"渠道不存在或未启用：{args.channel}")
                return 2
        if not runner.run_round(config, stage=args.stage, channel_key=args.channel):
            return 1
    elif args.command == "loop":
        runner.loop(config, interval=args.interval)
    elif args.command == "status":
        _status()
    elif args.command == "requeue":
        with runner.transaction() as cur:
            count = store.requeue_failed(cur, args.error_kind)
        print(f"恢复 {count} 篇")
    elif args.command == "monitor":
        monitor.run_monitor(transaction=runner.transaction, dry_run=args.dry_run)
    elif args.command == "relabel":
        keys = None
        if args.keys_file is not None:
            keys = [line.strip() for line in args.keys_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        done, failed = run_relabel(config, transaction=runner.transaction, limit=args.limit, url_keys=keys)
        print(f"重标 {done} 篇，失败 {failed} 篇")
    return 0
