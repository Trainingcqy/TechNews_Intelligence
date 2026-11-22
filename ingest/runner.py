"""One round of the pipeline, under a process-wide advisory lock."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from ingest import monitor, store
from ingest.analyze import run_analyze
from ingest.config import IngestConfig
from ingest.discover import run_discover
from ingest.discuss import run_discuss
from ingest.embed import run_embed
from ingest.fetch import run_fetch
from ingest.stories import link_pending
from common.db import db_transaction, get_conn, put_conn

logger = logging.getLogger(__name__)

LOCKED_MESSAGE = "已有采集进程在运行"
STAGES = ("discover", "fetch", "analyze", "embed", "link", "discuss")

# Kept in memory only: after a restart the monitor runs at most once more that day.
_monitor_done_on: date | None = None


@contextmanager
def transaction() -> Iterator:
    with db_transaction() as (_, cur):
        yield cur


def _run_stage(name: str, config: IngestConfig, transaction: store.Transaction, channel_key: str | None) -> None:
    if name == "discover":
        run_discover(config, transaction=transaction, channel_key=channel_key)
    elif name == "fetch":
        run_fetch(config, transaction=transaction)
    elif name == "analyze":
        run_analyze(config, transaction=transaction)
    elif name == "embed":
        run_embed(config, transaction=transaction)
    elif name == "link":
        link_pending(transaction=transaction)
    elif name == "discuss":
        run_discuss(config, transaction=transaction)


def _run_monitor_if_due(transaction: store.Transaction) -> None:
    global _monitor_done_on
    day = monitor.due_day(datetime.now(UTC), _monitor_done_on)
    if day is None:
        return
    try:
        monitor.run_monitor(transaction=transaction)
    except Exception:
        logger.exception("stage monitor failed")
        return
    _monitor_done_on = day


def run_round(
    config: IngestConfig,
    *,
    transaction: store.Transaction = transaction,
    stage: str | None = None,
    channel_key: str | None = None,
    with_monitor: bool = False,
) -> bool:
    """Returns False when another process holds the lock. Only loop passes with_monitor: the day the monitor
    last ran is kept in process memory, so every manual run after 9:00 Beijing time would run it again."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            locked = store.try_advisory_lock(cur)
        # The lock is session-level and survives the end of the transaction.
        conn.rollback()
        if not locked:
            print(LOCKED_MESSAGE)
            return False
        try:
            for name in (stage,) if stage else STAGES:
                try:
                    _run_stage(name, config, transaction, channel_key)
                except Exception:
                    logger.exception("stage %s failed", name)
            if with_monitor:
                _run_monitor_if_due(transaction)
        finally:
            with conn.cursor() as cur:
                store.release_advisory_lock(cur)
            conn.rollback()
        return True
    except BaseException:
        # A connection that may still hold the lock must not go back to the pool.
        conn.close()
        raise
    finally:
        put_conn(conn)


def loop(config: IngestConfig, *, interval: int, transaction: store.Transaction = transaction) -> None:
    while True:
        run_round(config, transaction=transaction, with_monitor=True)
        time.sleep(interval)
