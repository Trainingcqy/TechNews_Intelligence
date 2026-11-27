"""Tool definitions and the prepare / run / commit stages of one tool call."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.memory import MAX_FOLLOWS, Follow
from agent.retrieval.search import SearchResult
from agent.trace import Span, trace_span, warn
from agent.types import Evidence
from common import prompt_text
from common.taxonomy import KINDS, TOPICS

from .follows import FollowsResult, commit_check_follows, follows_trace_output, run_check_follows
from .read import ReadResult, commit_read_articles, read_trace_output, run_read_articles
from .search import commit_search_news, run_search_news, search_trace_output


def _field(tool: str, name: str, table: str = "fields") -> str:
    return prompt_text.text(f"tools.{tool}.{table}.{name}")


# An argument the schema does not define is rejected, so the model learns of it instead of it being dropped.
_STRICT = ConfigDict(extra="forbid")


class SearchNewsArgs(BaseModel):
    model_config = _STRICT
    query: str | None = Field(default=None, description=_field("search_news", "query"))
    days: int = Field(default=14, ge=1, le=365, description=_field("search_news", "days"))
    start_date: date | None = Field(default=None, description=_field("search_news", "start_date"))
    end_date: date | None = Field(default=None, description=_field("search_news", "end_date"))
    topic: Literal[tuple(TOPICS)] | None = Field(default=None, description=_field("search_news", "topic"))
    kind: Literal[tuple(KINDS)] | None = Field(default=None, description=_field("search_news", "kind"))
    publisher_kind: Literal["official", "media"] | None = Field(
        default=None, description=_field("search_news", "publisher_kind"))
    sort: Literal["relevance", "importance", "time"] | None = Field(
        default=None, description=_field("search_news", "sort"))
    limit: int = Field(default=8, ge=1, le=20, description=_field("search_news", "limit"))

    @field_validator("query")
    @classmethod
    def _blank_query_is_none(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None

    @model_validator(mode="after")
    def _check_combination(self) -> SearchNewsArgs:
        if self.sort is None:
            self.sort = "relevance" if self.query else "importance"
        if self.sort == "relevance" and not self.query:
            raise ValueError("sort=relevance requires query")
        if self.end_date is not None and self.start_date is None:
            raise ValueError("end_date requires start_date")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self


class ReadArticlesArgs(BaseModel):
    model_config = _STRICT
    ids: list[str] = Field(default_factory=list, max_length=5, description=_field("read_articles", "ids"))
    urls: list[str] = Field(default_factory=list, max_length=5, description=_field("read_articles", "urls"))
    max_chars: int = Field(default=4000, ge=800, le=12000, description=_field("read_articles", "max_chars"))

    @model_validator(mode="after")
    def _check_targets(self) -> ReadArticlesArgs:
        if not self.ids and not self.urls:
            raise ValueError("ids and urls must not both be empty")
        return self


class FollowQuery(BaseModel):
    model_config = _STRICT
    follow: str = Field(description=_field("check_follows", "follow", "query_fields"))
    query: str = Field(description=_field("check_follows", "query", "query_fields"))


class CheckFollowsArgs(BaseModel):
    model_config = _STRICT
    queries: list[FollowQuery] = Field(default_factory=list, max_length=MAX_FOLLOWS,
                                       description=_field("check_follows", "queries"))


@dataclass
class TurnState:
    user_urls: set[str]  # 当前问题和历史用户消息中出现过的地址，已经过 url_key
    read_count: int = 0
    seen_calls: set[str] = field(default_factory=set)
    allow_any_library_url: bool = False  # 只有 MCP 入口设为 True，见第 23.8 节
    follows: list[Follow] = field(default_factory=list)  # the user's follows, set in prepare
    today: date | None = None  # the user's date, for the check_follows windows


@dataclass(frozen=True)
class PreparedCall:
    index: int  # 在本步调用列表中的序号
    call_id: str
    name: str
    args: BaseModel
    dropped: tuple[str, ...] = ()  # 读取额度不足时截掉的编号与链接


@dataclass(frozen=True)
class ToolPayload:
    index: int
    call_id: str
    name: str
    args: BaseModel
    ok: bool
    data: Any  # SearchResult, ReadResult or FollowsResult
    error: str | None = None
    span: Span | None = None  # collect 登记后把返回文本补进这个 span


@dataclass(frozen=True)
class ToolOutcome:
    index: int
    call_id: str
    name: str
    ok: bool
    gather_text: str  # 返回给取证模型的文本
    new_evidence: tuple[Evidence, ...] = ()
    error: str | None = None
    reason: str | None = None  # 拒绝的原因：call_limit、unknown_tool、duplicate、invalid_args、read_quota


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[..., SearchResult | ReadResult | FollowsResult]  # reads only; takes a PreparedCall
    commit: Callable[..., ToolOutcome]  # 登记证据并生成文本
    trace_output: Callable[[Any], dict[str, Any]]  # tool span 的输出


def _run_search(prepared: PreparedCall, *, registry: EvidenceRegistry, state: TurnState) -> SearchResult:
    result = run_search_news(prepared.args)
    if result.weak_match:
        warn("low_relevance")
    return result


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_news",
        description=prompt_text.text("tools.search_news.description"),
        args_model=SearchNewsArgs,
        run=_run_search,
        commit=commit_search_news,
        trace_output=search_trace_output,
    ),
    ToolSpec(
        name="read_articles",
        description=prompt_text.text("tools.read_articles.description"),
        args_model=ReadArticlesArgs,
        run=run_read_articles,
        commit=commit_read_articles,
        trace_output=read_trace_output,
    ),
    ToolSpec(
        name="check_follows",
        description=prompt_text.text("tools.check_follows.description"),
        args_model=CheckFollowsArgs,
        run=run_check_follows,
        commit=commit_check_follows,
        trace_output=follows_trace_output,
    ),
)
_SPECS = {spec.name: spec for spec in TOOLS}


def tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.args_model.model_json_schema(),
            },
        }
        for spec in TOOLS
    ]


def reject(index: int, call_id: str, name: str, text: str, reason: str | None = None) -> ToolOutcome:
    return ToolOutcome(index=index, call_id=call_id, name=name, ok=False, gather_text=text, error=text, reason=reason)


def _validation_text(exc: ValidationError) -> str:
    reasons = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        reasons.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return "Invalid arguments: " + "; ".join(reasons)


def prepare_call(index: int, call: dict, *, state: TurnState, config: AgentConfig) -> PreparedCall | ToolOutcome:
    name, raw_args, call_id = call["name"], call["args"], call["id"]
    spec = _SPECS.get(name)
    if spec is None:
        return reject(index, call_id, name, f"Unknown tool: {name}. Available tools: {', '.join(_SPECS)}.",
                      "unknown_tool")

    try:
        args = spec.args_model.model_validate(raw_args)
    except ValidationError as exc:
        return reject(index, call_id, name, _validation_text(exc), "invalid_args")

    # Compared after validation, so that spelling out a default value does not make a call new.
    key = name + json.dumps(args.model_dump(mode="json"), sort_keys=True)
    if key in state.seen_calls:
        return reject(index, call_id, name, "Identical to an earlier call; see its result above.", "duplicate")
    state.seen_calls.add(key)

    dropped: tuple[str, ...] = ()
    if isinstance(args, ReadArticlesArgs):
        remaining = max(config.max_read_articles - state.read_count, 0)
        if remaining == 0:
            return reject(index, call_id, name, "The read limit of this request has been reached.", "read_quota")
        ids = args.ids[:remaining]
        urls = args.urls[:remaining - len(ids)]
        dropped = (*args.ids[len(ids):], *args.urls[len(urls):])
        args = args.model_copy(update={"ids": ids, "urls": urls})
        state.read_count += len(ids) + len(urls)

    return PreparedCall(index=index, call_id=call_id, name=name, args=args, dropped=dropped)


def run_call(prepared: PreparedCall, *, registry: EvidenceRegistry, state: TurnState) -> ToolPayload:
    name = prepared.name
    attributes = {"call_id": prepared.call_id, "index": prepared.index}
    with trace_span("tool", name, input=prepared.args.model_dump(mode="json"), attributes=attributes) as span:
        try:
            data = _SPECS[name].run(prepared, registry=registry, state=state)
        except Exception as exc:
            span.fail(exc)
            return ToolPayload(index=prepared.index, call_id=prepared.call_id, name=name, args=prepared.args,
                               ok=False, data=None, error=f"Tool failed: {type(exc).__name__}", span=span)
        span.output = _SPECS[name].trace_output(data)
        return ToolPayload(index=prepared.index, call_id=prepared.call_id, name=name, args=prepared.args,
                           ok=True, data=data, span=span)


def commit_call(payload: ToolPayload, *, registry: EvidenceRegistry, state: TurnState,
                config: AgentConfig) -> ToolOutcome:
    if not payload.ok:
        return reject(payload.index, payload.call_id, payload.name, payload.error)
    outcome = _SPECS[payload.name].commit(payload.args, payload.data, registry=registry)
    return replace(outcome, index=payload.index, call_id=payload.call_id)
