"""config/prompts/*.toml, their loader, and the wording the synthesis prompt shares with the evidence brief."""

from __future__ import annotations

import tomllib
from datetime import date, datetime, timezone

import pytest

from agent.evidence import EvidenceRegistry, render_for_synthesis
from agent.prompts import FINAL_SYSTEM_PROMPT
from agent.tools.registry import tool_schemas
from agent.types import Article, StoryStats
from common import prompt_text

# Every prompt and the placeholders the code fills in it.
PLACEHOLDERS = {
    "gather.system": {"clock", "corpus", "follows"},
    "gather.clock": {"now", "zone"},
    "gather.budget": {"step", "max_steps", "steps_left", "max_calls", "reads_left"},
    "gather.last_step": set(),
    "gather.follows": {"items"},
    "final.system": set(),
    "final.user": {"clock", "question", "brief"},
    "final.clock": {"now", "zone"},
    "tools.search_news.description": set(),
    "tools.search_news.no_result_hint": set(),
    "tools.search_news.fields.query": set(),
    "tools.search_news.fields.days": set(),
    "tools.search_news.fields.start_date": set(),
    "tools.search_news.fields.end_date": set(),
    "tools.search_news.fields.topic": set(),
    "tools.search_news.fields.kind": set(),
    "tools.search_news.fields.publisher_kind": set(),
    "tools.search_news.fields.sort": set(),
    "tools.search_news.fields.limit": set(),
    "tools.read_articles.description": set(),
    "tools.read_articles.fields.ids": set(),
    "tools.read_articles.fields.urls": set(),
    "tools.read_articles.fields.max_chars": set(),
    "tools.check_follows.description": set(),
    "tools.check_follows.fields.queries": set(),
    "tools.check_follows.query_fields.follow": set(),
    "tools.check_follows.query_fields.query": set(),
    "analyze.system": set(),
    "analyze.user": {"body_note", "topic_table", "kind_table", "title", "publisher", "body"},
    "analyze.no_body_note": set(),
    "analyze.retry_note": {"error"},
}


def _keys(table: dict, prefix: str) -> list[str]:
    keys = []
    for name, value in table.items():
        key = f"{prefix}.{name}"
        keys.extend(_keys(value, key) if isinstance(value, dict) else [key])
    return keys


def test_every_prompt_takes_exactly_its_placeholders():
    found = []
    for path in prompt_text.PROMPTS_DIR.glob("*.toml"):
        with path.open("rb") as file:
            found += _keys(tomllib.load(file), path.stem)
    assert sorted(found) == sorted(PLACEHOLDERS)
    for key, names in PLACEHOLDERS.items():
        prompt_text.render(key, **{name: "x" for name in names})


def test_tool_schemas_carry_every_description_of_tools_toml():
    for schema in tool_schemas():
        name = schema["function"]["name"]
        assert schema["function"]["description"] == prompt_text.text(f"tools.{name}.description")
        parameters = schema["function"]["parameters"]
        for table, models in (("fields", [parameters]), ("query_fields", parameters.get("$defs", {}).values())):
            described = {field: spec["description"] for model in models
                         for field, spec in model["properties"].items() if "description" in spec}
            prefix = f"tools.{name}.{table}."
            assert described == {key.removeprefix(prefix): prompt_text.text(key)
                                 for key in PLACEHOLDERS if key.startswith(prefix)}


def test_render_does_not_substitute_inside_values():
    assert "{question}" in prompt_text.render("final.user", clock="{question}", question="Q", brief="B")


def test_render_rejects_missing_and_extra_values():
    with pytest.raises(ValueError):
        prompt_text.render("final.user", clock="c", question="q")
    with pytest.raises(ValueError):
        prompt_text.render("final.user", clock="c", question="q", brief="b", extra="e")


def test_the_synthesis_prompt_and_the_evidence_block_use_the_same_wording():
    published = datetime(2026, 11, 12, tzinfo=timezone.utc)
    article = Article(article_id=1, url="https://example.com/a", title="t", title_zh="", summary="s", topics=(),
                      kind=None, publisher="OpenAI", publisher_kind="official", lang="en", published_at=published,
                      body_level="excerpt", story_id=1)
    registry = EvidenceRegistry()
    registry.add_article(article, "summary", StoryStats(1, 1, 1, ("official",), published, published))
    registry.seen_dates[1] = date(2026, 11, 10)
    block, _ = render_for_synthesis(registry, 60000)
    # What the prompt quotes, and what the block shows for it.
    for quoted, shown in [
        ('"Coverage:"', "Coverage: "),
        ('"No full text stored"', "No full text stored"),
        ('"already mentioned in the answer on MM-DD"', "already mentioned in the answer on 11-10"),
        ('publisher role is "official"', ", official |"),
    ]:
        assert quoted in FINAL_SYSTEM_PROMPT
        assert shown in block
