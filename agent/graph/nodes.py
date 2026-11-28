"""Nodes of the turn graph: the gathering loop, routing, synthesis and finalization."""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END
from langgraph.types import Send

from agent import memory, progress, prompts
from agent.citations import NOT_FOUND_TEXT, clean_direct_reply, contains_cjk, finalize_answer
from agent.context import build_context
from agent.evidence import EvidenceRegistry, counted
from agent.llm import call_text, call_with_tools, coerce_to_text
from agent.retrieval.corpus import corpus_info
from agent.tools.registry import (
    PreparedCall,
    ToolOutcome,
    ToolPayload,
    commit_call,
    prepare_call,
    reject,
    run_call,
    story_hits,
    tool_schemas,
)
from agent.trace import start_span, trace_span, warn, within
from agent.types import Answer
from common import prompt_text
from common.urls import extract_urls, url_key

logger = logging.getLogger(__name__)

from .state import TurnRuntime

CALL_LIMIT_TEXT = "This step exceeded its call limit; this call was not run."


def _runtime(config) -> TurnRuntime:
    return config["configurable"]["runtime"]


def prepare(state, config) -> dict:
    runtime = _runtime(config)
    question = state["question"]
    with trace_span("context") as span:
        context = build_context(state["history"], runtime.registry, runtime.config)
        runtime.tool_state.user_urls = context.user_urls | {url_key(url) for url in extract_urls(question)}
        follows = ""
        if runtime.user_key:
            try:
                runtime.follows = memory.list_follows(runtime.user_key, memory.MAX_FOLLOWS)
            except Exception:  # noqa: BLE001
                # A memory failure must not fail the turn; it runs without follows.
                logger.exception("follow lookup failed")
                warn("follows_unavailable")
                runtime.follows = []
            follows = memory.render_follows(runtime.follows, runtime.now.date())
            runtime.tool_state.follows = runtime.follows
            runtime.tool_state.today = runtime.now.date()
        system = prompts.render_gather_system(runtime.now, corpus_info(), follows)
        history_eids = [evidence.eid for evidence in runtime.registry.items()]
        for eid in history_eids:
            runtime.provenance[eid] = {"registered": "history", "upgraded": []}
        span.output = {
            "now": runtime.now.isoformat(),
            "timezone": getattr(runtime.now.tzinfo, "key", None),
            "history_turns": context.history_turns,
            "turns_used": context.turns_used,
            "history_evidence": history_eids,
            "user_urls": len(runtime.tool_state.user_urls),
            "follows": [{"query": follow.query,
                         "last_checked_at": follow.last_checked_at.isoformat() if follow.last_checked_at else None}
                        for follow in runtime.follows],
        }
    runtime.emit(progress.UNDERSTANDING)
    return {
        "context_messages": context.messages,
        "messages": [SystemMessage(system), *context.messages, HumanMessage(question)],
        "step": 0,
    }


def gather(state, config) -> dict:
    runtime = _runtime(config)
    runtime.check_stop()
    limits = runtime.config
    if state["step"] >= limits.max_steps:
        warn("step_budget_exhausted")
        return {"pending": [], "rejected": []}
    if state["step"]:
        runtime.emit(progress.analyzing(runtime.found))
    # The step stays open until collect has registered this step's evidence.
    step = runtime.step_span = start_span("step", attributes={"step": state["step"] + 1})
    with within(step):
        reply = call_with_tools(runtime.models[0], state["messages"], tool_schemas())
    if not reply.tool_calls:
        text = coerce_to_text(reply.content)
        step.output = {"calls": [], "reply": text}
        step.finish()
        runtime.step_span = None
        return {"direct_text": text, "pending": [], "rejected": []}

    pending: list[PreparedCall] = []
    rejected: list[ToolOutcome] = []
    for index, call in enumerate(reply.tool_calls):
        if index >= limits.max_calls_per_step:
            rejected.append(reject(index, call["id"], call["name"], CALL_LIMIT_TEXT, "call_limit"))
            continue
        prepared = prepare_call(index, call, state=runtime.tool_state, config=limits)
        if isinstance(prepared, PreparedCall):
            pending.append(prepared)
        else:
            rejected.append(prepared)
    if pending:
        follows = [follow.query for follow in runtime.follows]
        runtime.emit(progress.step(pending, runtime.registry, follows, runtime.now.date()))
    rejected_by_index = {outcome.index: outcome for outcome in rejected}
    step.output = {"calls": [_call_record(index, call, rejected_by_index.get(index))
                             for index, call in enumerate(reply.tool_calls)]}
    if rejected:
        step.warn("calls_rejected")
    return {"messages": [*state["messages"], reply], "pending": pending, "rejected": rejected,
            "tools_called": True}


def after_gather(state) -> list[Send] | str:
    if state["pending"]:
        return [Send("run_tool", {"step": state["step"], "call": call}) for call in state["pending"]]
    return "collect" if state["rejected"] else "route"


def run_tool(arg: dict, config) -> dict:
    runtime = _runtime(config)
    call = arg["call"]
    with within(runtime.step_span):
        payload = run_call(call, registry=runtime.registry, state=runtime.tool_state)
    return {"payloads": {(arg["step"], call.index): payload}}


def collect(state, config) -> dict:
    runtime = _runtime(config)
    step = state["step"]
    current = [payload for (payload_step, _), payload in state.get("payloads", {}).items() if payload_step == step]
    messages = list(state["messages"])
    with within(runtime.step_span):
        if runtime.user_key:
            story_ids = [hit.stats.story_id for payload in current if payload.ok for hit in story_hits(payload)]
            runtime.registry.seen_dates.update(memory.seen_story_dates(runtime.user_key, story_ids))
        for item in sorted([*current, *state["rejected"]], key=lambda item: item.index):
            span = None
            if isinstance(item, ToolPayload):
                runtime.tool_names.append(item.name)
                _record_followed(runtime, item)
                before = _levels(runtime.registry)
                outcome = commit_call(item, registry=runtime.registry, state=runtime.tool_state,
                                      config=runtime.config)
                _note_registration(runtime, before, step + 1, item.index)
                runtime.process.extend(progress.record(item, runtime.now.date(), runtime.read_ids))
                span = item.span
                if span is not None:
                    span.output["text"] = outcome.gather_text
                item = outcome
            messages.append(ToolMessage(item.gather_text, tool_call_id=item.call_id))
    runtime.found = progress.found(current)
    left = runtime.config.max_steps - (step + 1)
    if left > 0:
        # The budget follows the step's last result, which the tool span records as the model saw it.
        text = f"{messages[-1].content}\n\n{_budget_note(runtime, step + 1, left)}"
        messages[-1] = ToolMessage(text, tool_call_id=messages[-1].tool_call_id)
        if span is not None:
            span.output["text"] = text
    if runtime.step_span is not None:
        runtime.step_span.finish()
        runtime.step_span = None
    return {"messages": messages, "step": step + 1, "pending": [], "rejected": []}


def route(state, config) -> dict:
    runtime = _runtime(config)
    question = state["question"]
    tools_called = bool(state.get("tools_called"))
    citable = len(runtime.registry.citable_ids())
    decision = "synthesize" if tools_called and citable else "not_found" if tools_called else "direct"
    runtime.route = {"decision": decision, "tools_called": tools_called, "citable": citable}
    if decision == "direct":
        return {"answer": Answer(status="direct", text=clean_direct_reply(state.get("direct_text", ""), question))}
    if decision == "not_found":
        return {"answer": Answer(status="not_found", text=NOT_FOUND_TEXT["zh" if contains_cjk(question) else "en"])}
    return {}


def after_route(state) -> str:
    return END if "answer" in state else "synthesize"


def synthesize(state, config) -> dict:
    runtime = _runtime(config)
    runtime.check_stop()
    registry = runtime.registry
    user, runtime.left_out = prompts.render_final_user(
        state["question"], registry, runtime.config.synthesis_evidence_chars, runtime.now)
    if runtime.left_out:
        warn("evidence_truncated")
    # A left-out history item is not in the brief; an item whose body is left out keeps its entry.
    reports = len(registry.citable_ids()) - sum(registry.get(eid).history for eid in runtime.left_out)
    runtime.emit(progress.writing(reports))
    messages = [SystemMessage(prompts.FINAL_SYSTEM_PROMPT), *state["context_messages"], HumanMessage(user)]
    return {"raw_answer": call_text(runtime.models[1], messages)}


def finalize(state, config) -> dict:
    runtime = _runtime(config)
    answer, check = finalize_answer(state["raw_answer"], runtime.registry, state["question"])
    runtime.citations = check
    if check.removed_any:
        warn("citations_removed")
    return {"answer": answer}


def _budget_note(runtime: TurnRuntime, step: int, left: int) -> str:
    limits = runtime.config
    reads_left = max(limits.max_read_articles - runtime.tool_state.read_count, 0)
    note = prompt_text.render("gather.budget", step=str(step), max_steps=str(limits.max_steps),
                              steps_left=counted(left, "step", "steps"), max_calls=str(limits.max_calls_per_step),
                              reads_left=counted(reads_left, "read", "reads"))
    return f"{note} {prompt_text.text('gather.last_step')}" if left == 1 else note


def _call_record(index: int, call: dict, rejected: ToolOutcome | None) -> dict:
    record = {"index": index, "call_id": call["id"], "name": call["name"], "args": call["args"]}
    if rejected is None:
        return {**record, "disposition": "executed"}
    return {**record, "disposition": "rejected", "reason": rejected.reason, "text": rejected.gather_text}


def _levels(registry: EvidenceRegistry) -> dict[str, str]:
    return {evidence.eid: evidence.level for evidence in registry.items()}


def _note_registration(runtime: TurnRuntime, before: dict[str, str], step: int, index: int) -> None:
    where = {"step": step, "call": index}
    for eid, level in _levels(runtime.registry).items():
        if eid not in before:
            runtime.provenance[eid] = {"registered": where, "upgraded": []}
        elif level != before[eid]:
            runtime.provenance[eid]["upgraded"].append({**where, "level": level})


def _record_followed(runtime: TurnRuntime, payload: ToolPayload) -> None:
    # Only check_follows checks a follow, so a search for the same words does not count.
    if not (payload.ok and payload.name == "check_follows"):
        return
    for check in payload.data.checks:
        if check.error is None and check.follow.query not in runtime.followed_queries:
            runtime.followed_queries.append(check.follow.query)
