"""Tool definitions and the prepare / run / commit stages of one tool call."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.retrieval.search import SearchResult
from agent.trace import Span, trace_span, warn
from agent.types import Evidence
from common.taxonomy import KINDS, TOPICS

from .read import ReadResult, commit_read_articles, read_trace_output, run_read_articles
from .search import commit_search_news, run_search_news, search_trace_output


class SearchNewsArgs(BaseModel):
    query: str | None = Field(default=None, description="检索词。问题没有具体对象时留空。")
    days: int = Field(default=14, ge=1, le=365, description="时间窗：最近多少天。")
    start_date: date | None = Field(default=None, description="过去某一时段的起始日期，YYYY-MM-DD。填写后 days 不生效。")
    end_date: date | None = Field(default=None, description="该时段的结束日期，含当天。只能与 start_date 一起填写，不填时到今天。")
    topic: Literal[tuple(TOPICS)] | None = None
    kind: Literal[tuple(KINDS)] | None = None
    publisher_kind: Literal["official", "media"] | None = None
    sort: Literal["relevance", "importance", "time", "hn"] | None = None
    limit: int = Field(default=8, ge=1, le=20, description="返回的故事数。")

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
        if self.sort in ("importance", "hn") and self.query:
            raise ValueError(f"sort={self.sort} requires query to be empty")
        if self.end_date is not None and self.start_date is None:
            raise ValueError("end_date requires start_date")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self


class ReadArticlesArgs(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=5, description="证据编号，如 E3。")
    urls: list[str] = Field(default_factory=list, max_length=5, description="用户消息里出现的链接。")
    max_chars: int = Field(default=4000, ge=800, le=12000)

    @model_validator(mode="after")
    def _check_targets(self) -> ReadArticlesArgs:
        if not self.ids and not self.urls:
            raise ValueError("ids and urls must not both be empty")
        return self


@dataclass
class TurnState:
    user_urls: set[str]  # 当前问题和历史用户消息中出现过的地址，已经过 url_key
    read_count: int = 0
    seen_calls: set[str] = field(default_factory=set)
    allow_any_library_url: bool = False  # 只有 MCP 入口设为 True，见第 23.8 节


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
    data: Any  # SearchResult 或 ReadResult
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
    run: Callable[..., SearchResult | ReadResult]  # 只做读取，参数为 PreparedCall
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
        description=(
            "在新闻库中查找报道，结果按故事归并。有具体对象时填 query；没有具体对象、想看一段时间内发生了什么时不填 query，"
            "用 topic 和 days 限定范围。问的是过去的某一时段，例如\"10 月份\"\"上周\"时，用 start_date 和 end_date，"
            "此时 days 不生效。topic 是领域，kind 是文章类型，publisher_kind 只看官方发布或只看媒体报道。"
            "sort：relevance 按相关性，time 按时间，两者用于有 query 的检索；importance 按报道的发布方数量和社区分数，"
            "hn 按 HackerNews 分数，两者用于没有 query 的浏览。"
        ),
        args_model=SearchNewsArgs,
        run=_run_search,
        commit=commit_search_news,
        trace_output=search_trace_output,
    ),
    ToolSpec(
        name="read_articles",
        description=(
            "读取正文。ids 填检索结果里的证据编号，文章和 HackerNews 讨论都可以读；urls 只能填用户消息中出现过的链接。"
            "摘要不足以回答细节，或需要了解社区评论时使用。"
        ),
        args_model=ReadArticlesArgs,
        run=run_read_articles,
        commit=commit_read_articles,
        trace_output=read_trace_output,
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
        reasons.append(f"{loc}：{error['msg']}" if loc else error["msg"])
    return "参数错误：" + "；".join(reasons)


def prepare_call(index: int, call: dict, *, state: TurnState, config: AgentConfig) -> PreparedCall | ToolOutcome:
    name, raw_args, call_id = call["name"], call["args"], call["id"]
    spec = _SPECS.get(name)
    if spec is None:
        return reject(index, call_id, name, f"未知工具：{name}。可用工具：search_news、read_articles。", "unknown_tool")

    key = name + json.dumps(raw_args, sort_keys=True)
    if key in state.seen_calls:
        return reject(index, call_id, name, "与之前的调用完全相同，结果见上文。", "duplicate")
    state.seen_calls.add(key)

    try:
        args = spec.args_model.model_validate(raw_args)
    except ValidationError as exc:
        return reject(index, call_id, name, _validation_text(exc), "invalid_args")

    dropped: tuple[str, ...] = ()
    if isinstance(args, ReadArticlesArgs):
        remaining = max(config.max_read_articles - state.read_count, 0)
        if remaining == 0:
            return reject(index, call_id, name, "已达到本次请求的读取上限。", "read_quota")
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
                               ok=False, data=None, error=f"工具执行失败：{type(exc).__name__}", span=span)
        span.output = _SPECS[name].trace_output(data)
        return ToolPayload(index=prepared.index, call_id=prepared.call_id, name=name, args=prepared.args,
                           ok=True, data=data, span=span)


def commit_call(payload: ToolPayload, *, registry: EvidenceRegistry, state: TurnState,
                config: AgentConfig) -> ToolOutcome:
    if not payload.ok:
        return reject(payload.index, payload.call_id, payload.name, payload.error)
    outcome = _SPECS[payload.name].commit(payload.args, payload.data, registry=registry)
    return replace(outcome, index=payload.index, call_id=payload.call_id)
