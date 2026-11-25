"""Agent smoke test: one gather call and one synthesis call against the real models. No database is used.

The gather model must answer a question that needs retrieval with tool calls that pass the argument validation of
the tool registry. The synthesis model must answer from a synthetic evidence block with a STATUS header line that
split_status can parse.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from agent import prompts  # noqa: E402
from agent.citations import split_status  # noqa: E402
from agent.config import DEFAULT_ZONE, AgentConfig  # noqa: E402
from agent.evidence import EvidenceRegistry  # noqa: E402
from agent.llm import ModelCallError, ModelHandle, build_models, call_text, call_with_tools, coerce_to_text  # noqa: E402
from agent.tools.registry import PreparedCall, TurnState, prepare_call, tool_schemas  # noqa: E402
from agent.types import Article  # noqa: E402
from common.script_env import load_env  # noqa: E402

QUESTION = "Google 最近发布了什么新的世界模型？"
CORPUS = "新闻库范围：共 12000 篇，2026-09-01 至 2026-11-20。领域：ai 人工智能；类型：news 新闻、release 发布。"
_PREVIEW_CHARS = 300


def _synthetic_registry() -> EvidenceRegistry:
    article = Article(
        article_id=1,
        url="https://example.com/genie-4",
        title="Google’s Genie 4 world model adds audio, more realistic worlds",
        title_zh="谷歌 Genie 4 世界模型新增音频与更真实的场景",
        summary="谷歌发布 Genie 4 世界模型，新增音频生成，生成的虚拟场景在物理表现上更接近真实。",
        topics=("ai",),
        kind="release",
        publisher="TechCrunch",
        publisher_kind="media",
        lang="en",
        published_at=datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc).astimezone(DEFAULT_ZONE),
        body_level="full",
        story_id=1,
    )
    registry = EvidenceRegistry()
    registry.add_article(article, "summary")
    return registry


def check_gather(handle: ModelHandle, config: AgentConfig) -> bool:
    print("==== 取证 ====")
    system = prompts.GATHER_SYSTEM_PROMPT.format(
        today=f"{datetime.now(DEFAULT_ZONE):%Y-%m-%d}", corpus=CORPUS, follows=""
    )
    messages = [SystemMessage(system), HumanMessage(QUESTION)]
    started = time.monotonic()
    try:
        reply = call_with_tools(handle, messages, tool_schemas(), node="gather")
    except ModelCallError as exc:
        print(f"失败：取证模型调用出错 {exc.code}：{exc}")
        return False
    seconds = time.monotonic() - started

    calls = list(reply.tool_calls or [])
    print(f"耗时 {seconds:.1f}s，返回工具调用 {len(calls)} 个")
    if not calls:
        text = coerce_to_text(reply.content).strip()
        print("失败：取证模型没有返回工具调用，而问题需要先检索")
        print(f"        回复前 {_PREVIEW_CHARS} 字：{text[:_PREVIEW_CHARS]!r}")
        return False

    ok = True
    for call in calls:
        # A fresh state per call, so that the duplicate and read-quota checks do not mask the argument validation.
        prepared = prepare_call(0, call, state=TurnState(user_urls=set()), config=config)
        args = json.dumps(call["args"], ensure_ascii=False)
        if isinstance(prepared, PreparedCall):
            print(f"  {call['name']}  通过参数校验  {args}")
        else:
            ok = False
            print(f"  {call['name']}  未通过：{prepared.gather_text}")
            print(f"      参数：{args}")
    if not ok:
        print("失败：存在未通过 16.1 参数校验的工具调用")
    return ok


def check_final(handle: ModelHandle, config: AgentConfig) -> bool:
    print("==== 综合 ====")
    user = prompts.render_final_user(QUESTION, _synthetic_registry(), config.synthesis_evidence_chars)
    messages = [SystemMessage(prompts.FINAL_SYSTEM_PROMPT), HumanMessage(user)]
    started = time.monotonic()
    try:
        raw = call_text(handle, messages, node="final_synthesizer")
    except ModelCallError as exc:
        print(f"失败：综合模型调用出错 {exc.code}：{exc}")
        return False
    seconds = time.monotonic() - started

    status, body = split_status(raw)
    print(f"耗时 {seconds:.1f}s，输出 {len(raw)} 字符")
    if status is None:
        print("失败：输出第一行不是 STATUS: answered 或 STATUS: insufficient，解析不出状态")
        print(f"        输出前 {_PREVIEW_CHARS} 字：{raw[:_PREVIEW_CHARS]!r}")
        return False
    print(f"状态：{status}")
    print("正文：")
    print(body.strip())
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    load_env(args.env_file)
    config = AgentConfig()
    try:
        gather, final = build_models()
    except ModelCallError as exc:
        print(f"失败：模型构建出错 {exc.code}：{exc}")
        return 1
    print(f"取证模型：{gather.provider} / {gather.model}")
    print(f"综合模型：{final.provider} / {final.model}")
    print(f"问题：{QUESTION}")
    print(flush=True)

    gather_ok = check_gather(gather, config)
    print(flush=True)
    final_ok = check_final(final, config)
    print(flush=True)

    print("==== 汇总 ====")
    print(f"取证：{'通过' if gather_ok else '失败'}")
    print(f"综合：{'通过' if final_ok else '失败'}")
    return 0 if gather_ok and final_ok else 1


if __name__ == "__main__":
    sys.exit(main())
