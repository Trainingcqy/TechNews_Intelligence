"""Ingest tuning knobs. Every field can be overridden by the environment variable INGEST_<FIELD>."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class IngestConfig:
    discover_workers: int = 8
    fetch_workers: int = 3
    fetch_limit: int = 60
    analyze_workers: int = 4
    analyze_limit: int = 60
    embed_batch_size: int = 32

    # Request timeouts, seconds.
    discover_timeout: int = 30
    reader_timeout: int = 60
    embed_timeout: int = 30

    fetch_max_attempts: int = 5
    analyze_max_attempts: int = 3
    embed_max_attempts: int = 5
    # Minutes before the next attempt; attempts beyond the table length use the last entry.
    backoff_minutes: tuple[int, ...] = (5, 30, 180, 720)

    analyze_body_chars: int = 30_000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> IngestConfig:
        env = os.environ if environ is None else environ
        overrides: dict[str, object] = {}
        for f in fields(cls):
            raw = env.get(f"INGEST_{f.name.upper()}", "").strip()
            if not raw:
                continue
            if f.name == "backoff_minutes":
                overrides[f.name] = tuple(int(part) for part in raw.split(","))
            else:
                overrides[f.name] = int(raw)
        return cls(**overrides)
