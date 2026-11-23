"""State of the turn graph and the runtime objects its nodes share."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import BaseMessage

from agent.citations import CitationCheck
from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.llm import ModelHandle
from agent.tools.registry import PreparedCall, ToolOutcome, ToolPayload, TurnState
from agent.trace import Span
from agent.types import Answer, Event


def merge_payloads(left: dict, right: dict) -> dict:
    return {**left, **right}


class TurnGraphState(TypedDict, total=False):
    question: str
    history: list[dict]
    context_messages: list[BaseMessage]  # 历史对话，prepare 写入，综合时复用
    messages: list[BaseMessage]  # 取证循环的消息序列
    step: int
    pending: list[PreparedCall]  # 本步通过校验、待执行的调用
    rejected: list[ToolOutcome]  # 本步在校验阶段被拒绝的调用
    payloads: Annotated[dict[tuple[int, int], ToolPayload], merge_payloads]  # 键为步数与调用序号
    tools_called: bool
    direct_text: str
    raw_answer: str
    answer: Answer


@dataclass
class TurnRuntime:
    registry: EvidenceRegistry
    tool_state: TurnState
    config: AgentConfig
    models: tuple[ModelHandle, ModelHandle] | None  # run_turn 在 try 内构建
    emit: Callable[[Event], None]
    user_key: str | None
    follows: list[Any]  # prepare 写入，元素为 agent.memory.Follow；没有 user_key 时为空
    tool_names: list[str]  # 已执行的工具名，按调用顺序
    followed_queries: list[str]
    # 以下供请求记录使用。step 跨 gather、run_tool、collect 三个节点，节点之间不能经 ContextVar 传递，由这里持有。
    step_span: Span | None = None
    provenance: dict[str, dict] = field(default_factory=dict)  # 证据编号到登记与升级的位置
    route: dict | None = None
    dropped_bodies: list[str] = field(default_factory=list)  # 证据块中被去掉正文的编号
    citations: CitationCheck | None = None
