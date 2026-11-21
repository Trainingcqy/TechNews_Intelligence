"""One question-answer turn: runs the turn graph inside a request trace."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from agent.citations import ERROR_TEXT, contains_cjk
from agent.config import AgentConfig, load_config
from agent.trace import finalize_request_trace, request_trace_context
from agent.evidence import EvidenceRegistry
from agent.graph import build_turn_graph
from agent.graph.state import TurnRuntime
from agent.llm import ModelCallError, ModelHandle, build_models
from agent.tools.registry import TurnState
from agent.types import Answer, Event, Final


def run_turn(
    question: str,
    history: list[dict] | None = None,
    *,
    thread_id: str | None = None,
    request_id: str | None = None,
    user_key: str | None = None,
    on_event: Callable[[Event], None] | None = None,
    config: AgentConfig | None = None,
    models: tuple[ModelHandle, ModelHandle] | None = None,
) -> Answer:
    config = config or load_config()
    emit = _safe_emit(on_event)
    runtime = TurnRuntime(
        registry=EvidenceRegistry(), tool_state=TurnState(user_urls=set()), config=config,
        models=models, emit=emit, user_key=user_key,
        follows=[], tool_names=[], followed_queries=[],
    )
    error: Exception | None = None
    with request_trace_context(user_message=question, thread_id=thread_id, request_id=request_id) as trace:
        try:
            runtime.models = models or build_models()
            final_state = build_turn_graph().invoke(
                {"question": question, "history": history or []},
                config={"configurable": {"runtime": runtime},
                        "recursion_limit": 8 + 3 * config.max_steps},
            )
            answer = final_state["answer"]
        except ModelCallError as exc:
            answer = Answer(status="error", text=_error_text(question), error_code=exc.code)
        except Exception as exc:
            answer = Answer(status="error", text=_error_text(question),
                            error_code=f"runtime_{type(exc).__name__.lower()}")
            error = exc
        answer = replace(answer, request_id=trace.request_id,
                         tool_calls=tuple(runtime.tool_names),
                         followed_queries=tuple(runtime.followed_queries))
        trace.tool_call_chain[:] = runtime.tool_names
        finalize_request_trace(
            final_status=answer.status,
            evidence_count=len(answer.sources),
            final_answer_metadata={"answer_chars": len(answer.text), "source_count": len(answer.sources)},
            error_code=answer.error_code,
            error=error,
        )
    emit(Final(answer))
    return answer


def _safe_emit(on_event: Callable[[Event], None] | None) -> Callable[[Event], None]:
    def emit(event: Event) -> None:
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
