"""Thread context: recent turns as model messages, with their sources re-registered as evidence."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent.citations import rewrite_history_citations, strip_source_section
from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.retrieval.stories import fetch_articles_by_url_keys
from agent.trace import warn
from common.db import get_conn, put_conn
from common.urls import extract_urls, url_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnContext:
    messages: list[BaseMessage]
    user_urls: set[str]
    history_turns: int  # 历史中的完整轮数
    turns_used: int  # 带入本轮的轮数


def build_context(history: list[dict], registry: EvidenceRegistry, config: AgentConfig) -> TurnContext:
    turns: list[tuple[dict, dict]] = []
    pending_user: dict | None = None
    for message in history:
        if message.get("role") == "user":
            pending_user = message
        elif message.get("role") == "model" and pending_user is not None:
            turns.append((pending_user, message))
            pending_user = None
    history_turns = len(turns)
    turns = turns[max(0, len(turns) - config.history_turns):]

    turn_sources = [_sources(reply) for _, reply in turns]
    keys = list(dict.fromkeys(url_key(source["url"]) for sources in turn_sources for source in sources))
    _register(keys, registry)

    messages: list[BaseMessage] = []
    for (user, reply), sources in zip(turns, turn_sources):
        text = strip_source_section(_text(reply))
        text = rewrite_history_citations(text, sources, registry)[: config.history_answer_chars]
        messages.append(HumanMessage(content=_text(user)))
        messages.append(AIMessage(content=text))

    user_urls = {
        url_key(url)
        for message in history
        if message.get("role") == "user"
        for url in extract_urls(_text(message))
    }
    return TurnContext(messages=messages, user_urls=user_urls, history_turns=history_turns, turns_used=len(turns))


def _text(message: dict) -> str:
    return "".join(part.get("text", "") for part in message.get("parts", []))


def _sources(reply: dict) -> list[dict]:
    if reply.get("sources"):
        return reply["sources"]
    return [{"url": url} for url in reply.get("citation_urls") or []]


def _register(keys: list[str], registry: EvidenceRegistry) -> None:
    if not keys:
        return
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        articles = fetch_articles_by_url_keys(cur, keys)
        cur.close()
    except Exception:
        logger.exception("history source lookup failed")
        warn("history_sources_unavailable")
        return
    finally:
        if conn is not None:
            put_conn(conn)
    by_key = {url_key(article.url): article for article in articles}
    for key in keys:
        if key in by_key:
            registry.add_article(by_key[key], "summary")
