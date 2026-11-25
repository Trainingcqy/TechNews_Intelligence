"""Trace Console API and its static frontend."""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from common.db import close_db_pool, db_cursor, init_db_pool
from common.script_env import load_env

load_env()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TRACE_STATIC_DIR = Path(
    os.getenv(
        "TRACE_DASHBOARD_STATIC_DIR",
        str(Path(__file__).resolve().parents[1] / "web" / "trace" / "dist"),
    )
)

_RUN_COLUMNS = ("request_id, thread_id, channel, user_message, status, error_code, error_message, exception, "
                "started_at, latency_ms, token_usage, warnings, output")
_SPAN_COLUMNS = ("span_id, parent_span_id, kind, name, status, started_at_ms, finished_at_ms, input, output, "
                 "attributes, warnings, error_code, error_message, exception")

_bearer_scheme = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        init_db_pool()
        logger.info("trace dashboard database pool initialized")
    except Exception as exc:  # noqa: BLE001
        logger.warning("trace dashboard database pool initialization failed: %s", exc)
    try:
        yield
    finally:
        close_db_pool()
        logger.info("trace dashboard database pool closed")


app = FastAPI(title="TechNews Trace Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)


def _verify_trace_token(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)) -> None:
    expected = os.getenv("TRACE_DASHBOARD_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="trace_dashboard_token_not_configured")
    supplied = credentials.credentials if credentials else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="invalid_trace_dashboard_token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _fetch_all(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with db_cursor() as (_, cur):
        cur.execute(sql, params)
        columns = [item[0] for item in cur.description or []]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _fetch_one(sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    rows = _fetch_all(sql, params)
    return rows[0] if rows else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _parse_datetime(value: str | None, *, field_name: str) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_{field_name}") from exc


@app.get("/trace-api/runs")
def list_runs(
    _: None = Depends(_verify_trace_token),
    status: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    thread_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    started_from: str | None = Query(default=None),
    started_to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    filters: list[str] = []
    params: list[Any] = []
    if status:
        filters.append("status = %s")
        params.append(status)
    if channel:
        filters.append("channel = %s")
        params.append(channel)
    elif not thread_id:
        # Evaluation runs outnumber real ones; they are listed only when asked for.
        filters.append("channel <> 'eval'")
    if thread_id:
        filters.append("thread_id = %s")
        params.append(thread_id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        filters.append("(user_message ILIKE %s OR request_id ILIKE %s OR COALESCE(thread_id, '') ILIKE %s "
                       "OR COALESCE(error_code, '') ILIKE %s)")
        params.extend([term] * 4)
    for value, operator, name in ((started_from, ">=", "started_from"), (started_to, "<=", "started_to")):
        parsed = _parse_datetime(value, field_name=name)
        if parsed is not None:
            filters.append(f"started_at {operator} %s")
            params.append(parsed)

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    total = _fetch_one(f"SELECT COUNT(*) AS total FROM public.agent_runs {where}", tuple(params))
    rows = _fetch_all(
        f"""
        SELECT request_id, thread_id, channel, user_message, status, started_at, latency_ms,
               (token_usage->>'total_tokens')::int AS total_tokens, warnings, error_code
        FROM public.agent_runs
        {where}
        ORDER BY started_at DESC
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    return {"items": _json_safe(rows), "total": int((total or {}).get("total") or 0), "limit": limit,
            "offset": offset}


@app.get("/trace-api/runs/{request_id}")
def get_run(request_id: str, _: None = Depends(_verify_trace_token)) -> dict[str, Any]:
    run = _fetch_one(f"SELECT {_RUN_COLUMNS} FROM public.agent_runs WHERE request_id = %s", (request_id,))
    if not run:
        raise HTTPException(status_code=404, detail="trace_run_not_found")
    spans = _fetch_all(
        f"SELECT {_SPAN_COLUMNS} FROM public.agent_trace_spans WHERE request_id = %s "
        "ORDER BY started_at_ms, span_id",
        (request_id,),
    )
    return _json_safe({
        "run": run,
        "spans": spans,
        "articles": _articles(_referenced_article_ids(run, spans)),
    })


@app.get("/trace-api/runs/{request_id}/model-io/{span_id}")
def get_model_io(request_id: str, span_id: str, _: None = Depends(_verify_trace_token)) -> dict[str, Any]:
    row = _fetch_one(
        "SELECT input_messages, output FROM public.agent_model_io WHERE request_id = %s AND span_id = %s",
        (request_id, span_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="model_io_not_found")
    logger.info("trace model io accessed request_id=%s span_id=%s", request_id, span_id)
    return {"model_io": _json_safe(row)}


def _referenced_article_ids(run: dict[str, Any], spans: list[dict[str, Any]]) -> set[int]:
    """Article ids that the request record and its spans refer to."""
    article_ids: set[int] = set()

    def note(item: dict[str, Any]) -> None:
        if item.get("article_id") is not None:
            article_ids.add(item["article_id"])

    for row in (run.get("output") or {}).get("evidence") or []:
        note(row)
    for span in spans:
        output = span.get("output") or {}
        for story in output.get("stories") or []:
            article_ids.update([story["lead"], *story["related"]])
        for item in [*(output.get("items") or []), *(output.get("candidates") or []), *(output.get("ranked") or [])]:
            note(item)
    return article_ids


def _articles(article_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not article_ids:
        return {}
    rows = _fetch_all(
        """
        SELECT a.article_id, a.title, COALESCE(a.title_zh, '') AS title_zh, p.name AS publisher, a.url, a.event_at
        FROM news.article a
        JOIN news.publisher p ON p.publisher_id = a.publisher_id
        WHERE a.article_id = ANY(%s)
        """,
        (sorted(article_ids),),
    )
    return {row.pop("article_id"): row for row in rows}


def _render_missing_dashboard() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="zh-CN">
          <head><meta charset="utf-8"><title>Trace Dashboard</title></head>
          <body style="font-family: sans-serif; padding: 32px;">
            <h1>Trace Dashboard build missing</h1>
            <p>Run <code>npm install</code> and <code>npm run build</code> in <code>web/trace/</code>,
            or build the Docker trace image.</p>
          </body>
        </html>
        """,
        status_code=503,
    )


if (TRACE_STATIC_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=TRACE_STATIC_DIR / "assets"), name="trace-assets")


@app.get("/{path:path}", response_class=HTMLResponse)
def serve_dashboard(path: str = "") -> HTMLResponse:
    if path.startswith("trace-api"):
        raise HTTPException(status_code=404, detail="trace_api_route_not_found")
    index_file = TRACE_STATIC_DIR / "index.html"
    if not index_file.exists():
        return _render_missing_dashboard()
    return HTMLResponse(index_file.read_text(encoding="utf-8"))
