"""One question-answer turn: runs the turn graph inside a request trace."""

from __future__ import annotations

import threading
from dataclasses import asdict, replace
from datetime import datetime, tzinfo
from typing import Callable

from agent import progress
from agent.citations import ERROR_TEXT, contains_cjk
from agent.config import DEFAULT_ZONE, AgentConfig, load_config, using_zone
from agent.evidence import EvidenceRegistry
from agent.graph import build_turn_graph
from agent.graph.state import TurnRuntime, TurnStopped
from agent.llm import ModelCallError, ModelHandle, build_models
from agent.tools.registry import TurnState
from agent.trace import request_trace
from agent.types import Answer, Progress
from common.urls import url_key


def run_turn(
    question: str,
    history: list[dict] | None = None,
    *,
    channel: str,
    thread_id: str | None = None,
    request_id: str | None = None,
    user_key: str | None = None,
    zone: tzinfo | None = None,
    on_event: Callable[[Progress], None] | None = None,
    stop: threading.Event | None = None,
    config: AgentConfig | None = None,
    models: tuple[ModelHandle, ModelHandle] | None = None,
    history_reader: Callable[[], list[dict]] | None = None,
) -> Answer:
    """channel is web, telegram, cli or eval and only labels the trace. zone is the user's clock zone, DEFAULT_ZONE when
    not given. stop, set from another thread, ends the turn with status stopped before its next model call; a call
    already running finishes and its result is dropped. history_reader returns the user's past messages for
    read_history; without it there are none on record."""
    config = config or load_config()
    zone = zone or DEFAULT_ZONE
    emit = _safe_emit(on_event)
    runtime = TurnRuntime(
        registry=EvidenceRegistry(), tool_state=TurnState(user_urls=set(), history_reader=history_reader),
        config=config, models=models, emit=emit, user_key=user_key,
        follows=[], tool_names=[], followed_queries=[], now=datetime.now(zone), stop=stop,
    )
    with using_zone(zone), request_trace(question=question, channel=channel, thread_id=thread_id,
                                         request_id=request_id) as trace:
        error_message = None
        try:
            runtime.models = models or build_models()
            final_state = build_turn_graph().invoke(
                {"question": question, "history": history or []},
                config={"configurable": {"runtime": runtime},
                        "recursion_limit": 8 + 3 * config.max_steps},
            )
            answer = final_state["answer"]
        except TurnStopped:
            answer = Answer(status="stopped", text="")
        except ModelCallError as exc:
            # The failing llm span already holds the exception.
            answer = Answer(status="error", text=_error_text(question), error_code=exc.code)
            error_message = str(exc)
        except Exception as exc:
            answer = Answer(status="error", text=_error_text(question),
                            error_code=f"runtime_{type(exc).__name__.lower()}")
            trace.fail(exc)
        # A stop during the last model call drops the answer that call produced.
        if stop is not None and stop.is_set() and answer.status != "error":
            answer = Answer(status="stopped", text="")
        answer = replace(answer, request_id=trace.request_id,
                         tool_calls=tuple(runtime.tool_names),
                         followed_queries=tuple(runtime.followed_queries),
                         process=progress.process(runtime.process))
        trace.output = _request_output(runtime, answer)
        trace.finish(status=answer.status, error_code=answer.error_code, error_message=error_message)
    return answer


def _request_output(runtime: TurnRuntime, answer: Answer) -> dict:
    check = runtime.citations
    return {
        "route": runtime.route,
        "citations": None if check is None else {
            "status_line": check.status_line,
            "cited": dict(check.cited),
            "removed_eids": list(check.removed_eids),
            "removed_urls": check.removed_urls,
            "removed_numeric": check.removed_numeric,
        },
        "answer": {"text": answer.text, "sources": [asdict(source) for source in answer.sources]},
        "evidence": _evidence_table(runtime),
    }


def _evidence_table(runtime: TurnRuntime) -> list[dict]:
    cited = dict(runtime.citations.cited) if runtime.citations else {}
    synthesized = bool(runtime.route and runtime.route["decision"] == "synthesize")
    rows = []
    for evidence in runtime.registry.items():
        row = {"eid": evidence.eid, "level": evidence.level, **runtime.provenance.get(evidence.eid, {}),
               "article_id": evidence.article.article_id, "story_id": evidence.article.story_id}
        told_on = runtime.registry.told.get(url_key(evidence.article.url))
        if told_on is not None:
            row["told_on"] = told_on.isoformat()
        if synthesized and evidence.level == "fulltext":
            row["body_in_block"] = evidence.eid not in runtime.left_out
        elif synthesized and evidence.eid in runtime.left_out:
            row["in_block"] = False
        if evidence.eid in cited:
            row["cited"] = cited[evidence.eid]
        rows.append(row)
    return rows


def _safe_emit(on_event: Callable[[Progress], None] | None) -> Callable[[Progress], None]:
    def emit(event: Progress) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:
            # A failing consumer, such as a closed stream, must not fail the turn.
            pass

    return emit


def _error_text(question: str) -> str:
    return ERROR_TEXT["zh" if contains_cjk(question) else "en"]
