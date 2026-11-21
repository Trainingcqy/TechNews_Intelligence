"""Shared helpers for the phase-1 evaluation scripts: read-only DB access, time parsing, answer sheets."""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ".env"


def configure_stdout() -> None:
    """Titles contain arbitrary Unicode; the default Windows console encoding cannot print all of it."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")


def add_db_args(parser) -> None:
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)


@contextmanager
def readonly_cursor(env_file: str | None = DEFAULT_ENV_FILE):
    import psycopg2

    from services.script_env import load_env

    load_env(env_file)
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "5555")),
        dbname=os.getenv("DB_NAME", "DB"),
        user=os.getenv("DB_USER", ""),
        password=os.getenv("DB_PASS", ""),
        options="-c default_transaction_read_only=on -c statement_timeout=300000",
    )
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


def parse_time(text: str) -> datetime:
    """ISO 8601; a value without an offset is read as UTC."""
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# Answer sheets are Markdown. Each item is a `## N. \`id\`` heading followed by `- field：\`[ ]\`` lines.
_HEADING_RE = re.compile(r"^## \d+\. `([^`]+)`[ \t]*$", re.M)
_ANSWER_RE = re.compile(r"^- ([^：\n]+)：`\[([^\]]*)\]`[ \t]*$", re.M)


def answer_line(field: str) -> str:
    return f"- {field}：`[ ]`"


def parse_answers(text: str) -> dict[str, dict[str, str]]:
    text = text.replace("\r\n", "\n")
    headings = list(_HEADING_RE.finditer(text))
    answers: dict[str, dict[str, str]] = {}
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        block = text[heading.end():end]
        answers[heading.group(1)] = {field.strip(): value.strip().lower() for field, value in _ANSWER_RE.findall(block)}
    return answers


def choice(value: str | None, allowed: tuple[str, ...] = ("y", "n")) -> str | None:
    """None for a blank or `?` answer (excluded from statistics); ValueError for anything else outside `allowed`."""
    if value is None or value in ("", "?"):
        return None
    if value not in allowed:
        raise ValueError(f"unexpected answer {value!r}, expected one of {allowed} or blank")
    return value


def read_choices(path: Path, field: str, allowed: tuple[str, ...] = ("y", "n")) -> dict[str, str | None]:
    answers = parse_answers(path.read_text(encoding="utf-8"))
    out: dict[str, str | None] = {}
    for item_id, fields in answers.items():
        try:
            out[item_id] = choice(fields.get(field), allowed)
        except ValueError as exc:
            raise ValueError(f"{path}: {item_id}: {field}: {exc}") from None
    return out


def pct(numerator: float, denominator: float) -> str:
    return f"{100 * numerator / denominator:.1f}%" if denominator else "n/a"
