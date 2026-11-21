"""Analysis smoke test: runs the analyze step on 20 articles from the old tables and reports whether the outputs
pass validation. Read-only; nothing is written to the database.

Articles: 12 with a full Reader body (2 per old source), 4 whose body is cut to 300 characters to stand in for a
feed excerpt, and 4 HackerNews items without a body. The old tables hold no feed excerpts.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import openai  # noqa: E402
import psycopg2  # noqa: E402

from ingest import store  # noqa: E402
from ingest.analyze import AnalyzeFailure, InvalidJson, SchemaViolation, analyze, parse_analysis  # noqa: E402
from ingest.config import IngestConfig  # noqa: E402
from ingest.fetch import clean_body  # noqa: E402
from ingest.reader import parse_reader_response  # noqa: E402
from ingest.sources import load_sources, resolve_publisher  # noqa: E402
from services.llm_provider import build_chat_model  # noqa: E402
from services.script_env import load_env  # noqa: E402

FULL_PER_SOURCE = 2
EXCERPT_SOURCES = ("techcrunch_feed", "google_ai_blog", "aws_ml_blog", "nvidia_dev_blog")
EXCERPT_CHARS = 300
NONE_COUNT = 4
_MIN_BODY_CHARS = 400

_BODY_ROWS_SQL = """
SELECT url, title, source_key, raw_content FROM (
    SELECT t.url, t.title, t.source_key, j.raw_content,
           row_number() OVER (PARTITION BY t.source_key ORDER BY t.id DESC) AS rn
    FROM tech_news t
    JOIN jina_raw_logs j ON j.url = t.url
    WHERE t.created_at > now() - interval '30 days' AND length(j.raw_content) >= 1000
) s
WHERE rn <= 6
ORDER BY source_key, rn
"""

_NO_BODY_ROWS_SQL = """
SELECT t.url, t.title, j.raw_content
FROM tech_news t
LEFT JOIN jina_raw_logs j ON j.url = t.url
WHERE t.source_key = 'hackernews_frontpage' AND (j.url IS NULL OR length(j.raw_content) < 1000)
ORDER BY t.id DESC
LIMIT 20
"""


@contextmanager
def _readonly_cursor():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "5555")),
        dbname=os.getenv("DB_NAME", "DB"),
        user=os.getenv("DB_USER", ""),
        password=os.getenv("DB_PASS", ""),
        options="-c default_transaction_read_only=on",
    )
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


def _reader_body(raw: str | None) -> str:
    if raw is None:
        return ""
    result = parse_reader_response(200, raw)
    return clean_body(result.body) if result.ok else ""


def sample(cur) -> list[store.AnalyzeItem]:
    cur.execute(_BODY_ROWS_SQL)
    by_source: dict[str, list[tuple[str, str, str]]] = {}
    for url, title, source_key, raw in cur.fetchall():
        body = _reader_body(raw)
        if len(body) >= _MIN_BODY_CHARS:
            by_source.setdefault(source_key, []).append((url, title, body))

    full, excerpt = [], []
    for source_key, rows in by_source.items():
        full.extend(rows[:FULL_PER_SOURCE])
        if source_key in EXCERPT_SOURCES and len(rows) > FULL_PER_SOURCE:
            excerpt.append(rows[FULL_PER_SOURCE])

    cur.execute(_NO_BODY_ROWS_SQL)
    none = [(url, title) for url, title, raw in cur.fetchall() if len(_reader_body(raw)) < _MIN_BODY_CHARS]

    rows = [(url, title, body, "full") for url, title, body in full]
    rows += [(url, title, body[:EXCERPT_CHARS], "excerpt") for url, title, body in excerpt]
    rows += [(url, title, "", "none") for url, title in none[:NONE_COUNT]]
    hosts = load_sources().hosts
    return [
        store.AnalyzeItem(n, title, resolve_publisher(url, hosts).name, body, body_level)
        for n, (url, title, body, body_level) in enumerate(rows, 1)
    ]


class _Recorder:
    """Keeps every raw output of the wrapped model."""

    def __init__(self, model) -> None:
        self._model = model
        self.outputs: list[str] = []

    def invoke(self, messages, **kwargs):
        try:
            message = self._model.invoke(messages, **kwargs)
        except openai.LengthFinishReasonError as exc:
            self.outputs.append(exc.completion.choices[0].message.content or "")
            raise
        self.outputs.append(message.text)
        return message


def _validation_error(raw: str) -> str:
    try:
        parse_analysis(raw)
    except (InvalidJson, SchemaViolation) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def run(items: list[store.AnalyzeItem], model, config: IngestConfig) -> bool:
    print(f"模型：{config.analyze_provider} / {config.analyze_model}    JSON 输出模式：{'开' if config.analyze_json_mode else '关'}")
    print(f"文章：{len(items)} 篇，" + "，".join(f"{k} {v}" for k, v in Counter(i.body_level for i in items).items()))
    print(flush=True)
    outcomes: list[tuple[str, str]] = []  # (body_level, outcome)
    elapsed: list[float] = []
    for item in items:
        recorder = _Recorder(model)
        started = time.monotonic()
        head = f"[{item.article_id:2d}/{len(items)}] {item.body_level:<7} {item.publisher[:24]:<24}"
        try:
            analysis = analyze(recorder, item, config)
        except AnalyzeFailure as exc:
            outcome, analysis, detail = exc.kind, None, exc.detail
        except Exception as exc:
            outcome, analysis, detail = "unexpected", None, f"{type(exc).__name__}: {exc}"
        else:
            outcome, detail = ("first" if len(recorder.outputs) == 1 else "retry"), ""
        seconds = time.monotonic() - started
        elapsed.append(seconds)
        outcomes.append((item.body_level, outcome))

        label = {"first": "通过（第 1 次输出）", "retry": "通过（重发后）"}.get(outcome, f"失败 {outcome}")
        print(f"{head} {seconds:5.1f}s  {label}")
        print(f"        原标题：{item.title}")
        if len(recorder.outputs) == 2 or analysis is None:
            for n, raw in enumerate(recorder.outputs, 1):
                error = _validation_error(raw)
                if error:
                    print(f"        第 {n} 次输出未通过：{error}")
                    print(f"        第 {n} 次输出前 300 字：{raw[:300]!r}")
        if analysis is not None:
            print(f"        title_zh：{analysis.title_zh}")
            print(f"        summary：{analysis.summary}")
            print(f"        topics={','.join(analysis.topics)}  kind={analysis.kind}")
        elif outcome not in ("invalid_json", "schema_violation"):
            print(f"        {detail[:500]}")
        print(flush=True)

    passed = [o for _, o in outcomes if o in ("first", "retry")]
    failed = Counter(o for _, o in outcomes if o not in ("first", "retry"))
    print("==== 汇总 ====")
    print(f"JSON 输出模式：{'开' if config.analyze_json_mode else '关'}")
    print(f"通过校验：{len(passed)}/{len(outcomes)}（第 1 次输出通过 {passed.count('first')}，重发后通过 {passed.count('retry')}）")
    print("失败：" + ("，".join(f"{k} {v}" for k, v in failed.items()) if failed else "无"))
    by_level = Counter(level for level, _ in outcomes)
    ok_by_level = Counter(level for level, o in outcomes if o in ("first", "retry"))
    print("按正文级别：" + "，".join(f"{level} {ok_by_level[level]}/{n}" for level, n in by_level.items()))
    if elapsed:
        print(f"耗时：平均 {sum(elapsed) / len(elapsed):.1f}s，最长 {max(elapsed):.1f}s")
    return len(passed) == len(outcomes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    load_env(args.env_file)
    config = IngestConfig.from_env()
    model = build_chat_model(provider=config.analyze_provider, model_name=config.analyze_model, temperature=0)
    with _readonly_cursor() as cur:
        items = sample(cur)
    return 0 if run(items, model, config) else 1


if __name__ == "__main__":
    sys.exit(main())
