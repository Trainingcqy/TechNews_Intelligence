"""State of the turn graph and the runtime objects its nodes share."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import BaseMessage

from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.llm import ModelHandle
from agent.tools.registry import PreparedCall, ToolOutcome, ToolPayload, TurnState
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
    follows: list[Any]  # prepare 写入；三期启用前恒为空
    tool_names: list[str]  # 已执行的工具名，按调用顺序
    followed_queries: list[str]
