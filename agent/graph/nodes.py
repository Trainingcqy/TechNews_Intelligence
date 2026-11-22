"""Nodes of the turn graph: the gathering loop, routing, synthesis and finalization."""

from __future__ import annotations

import logging
from datetime import date, datetime
from functools import wraps

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END
from langgraph.types import Send

from agent import memory, prompts
from agent.citations import NOT_FOUND_TEXT, clean_direct_reply, contains_cjk, finalize_answer
from agent.config import BEIJING
from agent.context import build_context
from agent.trace import trace_span
from agent.llm import call_text, call_with_tools, coerce_to_text
from agent.retrieval.corpus import corpus_info
from agent.tools.registry import (
    PreparedCall,
    ToolOutcome,
    ToolPayload,
    commit_call,
    prepare_call,
    run_call,
    tool_schemas,
)
from agent.types import Answer, Evidence, EvidenceFound, Progress
from common.urls import extract_urls, url_key

logger = logging.getLogger(__name__)

from .state import TurnRuntime

CALL_LIMIT_TEXT = "已超过单步调用上限，本次调用未执行。"


def _traced(fn):
    @wraps(fn)
    def wrapped(state, config):
        with trace_span("graph_node", fn.__name__):
            return fn(state, config)

    return wrapped


def _runtime(config) -> TurnRuntime:
    return config["configurable"]["runtime"]


def _today() -> str:
    return f"{_today_date():%Y-%m-%d}"


def _today_date() -> date:
    return datetime.now(BEIJING).date()


@_traced
def prepare(state, config) -> dict:
    runtime = _runtime(config)
    question = state["question"]
    context = build_context(state["history"], runtime.registry, runtime.config)
    runtime.tool_state.user_urls = context.user_urls | {url_key(url) for url in extract_urls(question)}
    follows = ""
    if runtime.user_key:
        try:
            runtime.follows = memory.list_follows(runtime.user_key, memory.MAX_FOLLOWS)
        except Exception:  # noqa: BLE001
            # A memory failure must not fail the turn; it runs without follows.
            logger.exception("follow lookup failed")
            runtime.follows = []
        follows = memory.render_follows(runtime.follows, _today_date())
    system = prompts.GATHER_SYSTEM_PROMPT.format(today=_today(), corpus=corpus_info(), follows=follows)
    runtime.emit(Progress("understanding", "正在理解问题"))
    return {
        "context_messages": context.messages,
        "messages": [SystemMessage(system), *context.messages, HumanMessage(question)],
        "step": 0,
    }


@_traced
def gather(state, config) -> dict:
    runtime = _runtime(config)
    limits = runtime.config
    if state["step"] >= limits.max_steps:
        return {"pending": [], "rejected": []}
    reply = call_with_tools(runtime.models[0], state["messages"], tool_schemas(), node="gather")
    if not reply.tool_calls:
        return {"direct_text": coerce_to_text(reply.content), "pending": [], "rejected": []}

    pending: list[PreparedCall] = []
    rejected: list[ToolOutcome] = []
    for index, call in enumerate(reply.tool_calls):
        if index >= limits.max_calls_per_step:
            rejected.append(ToolOutcome(index=index, call_id=call["id"], name=call["name"], ok=False,
                                        gather_text=CALL_LIMIT_TEXT, error=CALL_LIMIT_TEXT))
            continue
        prepared = prepare_call(index, call, state=runtime.tool_state, config=limits)
        if isinstance(prepared, PreparedCall):
            pending.append(prepared)
            runtime.emit(_progress(prepared))
        else:
            rejected.append(prepared)
    return {"messages": [*state["messages"], reply], "pending": pending, "rejected": rejected,
            "tools_called": True}


def after_gather(state) -> list[Send] | str:
    if state["pending"]:
        return [Send("run_tool", {"step": state["step"], "call": call}) for call in state["pending"]]
    return "collect" if state["rejected"] else "route"


@_traced
def run_tool(arg: dict, config) -> dict:
    runtime = _runtime(config)
    call = arg["call"]
    payload = run_call(call, registry=runtime.registry, state=runtime.tool_state)
    return {"payloads": {(arg["step"], call.index): payload}}


@_traced
def collect(state, config) -> dict:
    runtime = _runtime(config)
    step = state["step"]
    current = [payload for (payload_step, _), payload in state.get("payloads", {}).items() if payload_step == step]
    messages = list(state["messages"])
    if runtime.user_key:
        story_ids = [hit.stats.story_id for p in current if p.ok and p.name == "search_news" for hit in p.data.hits]
        runtime.registry.seen_dates.update(memory.seen_story_dates(runtime.user_key, story_ids))
    for item in sorted([*current, *state["rejected"]], key=lambda item: item.index):
        if isinstance(item, ToolPayload):
            runtime.tool_names.append(item.name)
            _record_followed(runtime, item)
            item = commit_call(item, registry=runtime.registry, state=runtime.tool_state, config=runtime.config)
        messages.append(ToolMessage(item.gather_text, tool_call_id=item.call_id))
        for evidence in item.new_evidence:
            runtime.emit(_evidence_found(evidence))
    return {"messages": messages, "step": step + 1, "pending": [], "rejected": []}


@_traced
def route(state, config) -> dict:
    runtime = _runtime(config)
    question = state["question"]
    if not state.get("tools_called"):
        return {"answer": Answer(status="direct", text=clean_direct_reply(state.get("direct_text", ""), question))}
    if not runtime.registry.citable_ids():
        return {"answer": Answer(status="not_found", text=NOT_FOUND_TEXT["zh" if contains_cjk(question) else "en"])}
    return {}


def after_route(state) -> str:
    return END if "answer" in state else "synthesize"


@_traced
def synthesize(state, config) -> dict:
    runtime = _runtime(config)
    runtime.emit(Progress("synthesizing", "正在生成回答"))
    user = prompts.render_final_user(state["question"], runtime.registry, runtime.config.synthesis_evidence_chars)
    messages = [SystemMessage(prompts.FINAL_SYSTEM_PROMPT), *state["context_messages"], HumanMessage(user)]
    return {"raw_answer": call_text(runtime.models[1], messages, node="final_synthesizer")}


@_traced
def finalize(state, config) -> dict:
    runtime = _runtime(config)
    return {"answer": finalize_answer(state["raw_answer"], runtime.registry, state["question"])}


def _record_followed(runtime: TurnRuntime, payload: ToolPayload) -> None:
    if not (payload.ok and payload.name == "search_news" and payload.args.query):
        return
    query = payload.args.query.lower()
    for follow in runtime.follows:
        if follow.query.lower() == query and follow.query not in runtime.followed_queries:
            runtime.followed_queries.append(follow.query)


def _progress(call: PreparedCall) -> Progress:
    if call.name == "read_articles":
        return Progress("reading", "正在读取正文")
    args = call.args
    if args.start_date is None:
        window = f"最近 {args.days} 天"
    elif args.end_date is None:
        window = f"{args.start_date} 至今"
    else:
        window = f"{args.start_date} 至 {args.end_date}"
    subject = args.query or args.topic
    return Progress("retrieving", "正在检索相关新闻", f"{subject} {window}" if subject else window)


def _evidence_found(evidence: Evidence) -> EvidenceFound:
    if evidence.doc == "discussion":
        discussion = evidence.discussion
        return EvidenceFound(evidence.eid, discussion.title, "HackerNews 讨论", discussion.url, "")
    article = evidence.article
    return EvidenceFound(evidence.eid, article.title, article.publisher, article.url,
                         f"{article.published_at.astimezone(BEIJING):%Y-%m-%d}")
