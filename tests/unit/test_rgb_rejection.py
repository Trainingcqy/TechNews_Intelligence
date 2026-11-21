"""Deterministic helpers for the RGB negative-rejection runner (no LLM / no network)."""

from __future__ import annotations

import json
from pathlib import Path

from eval.rgb.run_rgb_rejection import (
    _answer_leaks_into_docs,
    _format_negative_docs,
    _gold_answer_present,
    _int_to_cn,
    _load_cases,
    _resolve_final_prompt,
    _resolve_judge_config,
    _verdict_for,
)


def test_gold_answer_present_requires_all_elements():
    gold = ["东盟", "中国", "日本", "韩国", "澳大利亚", "新西兰"]
    full = "RCEP包括东盟十国以及中国、日本、韩国、澳大利亚和新西兰。"
    assert _gold_answer_present(gold, full)
    # only a partial mention -> not present (model did not surface the full answer)
    assert not _gold_answer_present(gold, "RCEP有中国、日本等成员国")


def test_gold_answer_present_numeral_normalized_and_refusal():
    assert _gold_answer_present(["12人"], "事故造成十二人死亡")  # 12 -> 十二
    assert not _gold_answer_present(["12人"], "现有资料不足，无法回答。")
    assert not _gold_answer_present(["x"], "")


def test_format_negative_docs_caps_count_and_numbers():
    block = _format_negative_docs(["aaa", "bbb", "ccc"], limit=2)
    assert block == (
        "[E1] 标题：RGB 文档 1\n正文，全文 3 字符：\naaa\n\n"
        "[E2] 标题：RGB 文档 2\n正文，全文 3 字符：\nbbb"
    )


def test_format_negative_docs_truncates_long_doc():
    block = _format_negative_docs(["x" * 5000], limit=1, max_chars=100)
    assert block == "[E1] 标题：RGB 文档 1\n正文，前 100 字符，全文 5000 字符：\n" + "x" * 100


def test_verdict_for_counts_insufficient_as_refusal_without_judge():
    verdict = _verdict_for("insufficient", "q", "evidence", "无法回答", client=None)
    assert verdict == {"answered": False, "misattributed": False, "reason": "STATUS: insufficient"}


def test_verdict_for_sends_answered_to_judge():
    class Client:
        def invoke(self, messages):
            return '{"answered": true, "misattributed": true, "reason": "r"}'

    verdict = _verdict_for(
        "answered", "q", "evidence", "答案是12人",
        client=Client(), coerce_text_fn=str, extract_json_fn=json.loads,
    )
    assert verdict == {"answered": True, "misattributed": True, "reason": "r"}


def test_load_cases_filters_no_negative_and_only_id(tmp_path: Path):
    data = tmp_path / "zh.json"
    rows = [
        {"id": 0, "query": "q0", "answer": ["a"], "positive": ["p"], "negative": ["n0"]},
        {"id": 1, "query": "q1", "answer": ["a"], "positive": ["p"], "negative": []},
        {"id": 2, "query": "q2", "answer": ["a"], "positive": ["p"], "negative": ["n2"]},
    ]
    data.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    cases = _load_cases(data, only_id=None)
    assert [c["id"] for c in cases] == [0, 2]  # id=1 dropped (no negative docs)

    one = _load_cases(data, only_id="2")
    assert [c["id"] for c in one] == [2]


def test_int_to_cn_small_numbers():
    assert _int_to_cn(5) == "五"
    assert _int_to_cn(12) == "十二"
    assert _int_to_cn(20) == "二十"
    assert _int_to_cn(105) == "一百零五"
    assert _int_to_cn(120) == "一百二十"


def test_answer_leaks_detects_chinese_numeral_form():
    # gold answer "12人" leaks as "十二人" in a negative doc -> not a valid rejection case
    assert _answer_leaks_into_docs(["12人"], ["事故造成十二人死亡、二十人受伤"], docs_limit=5)
    # genuine distractor: answer absent -> no leak
    assert not _answer_leaks_into_docs(["12人"], ["这是一篇无关的体育新闻"], docs_limit=5)


def test_answer_leaks_respects_docs_limit():
    # leak sits in the 6th doc; with docs_limit=5 it is not fed, so no leak
    docs = ["无关"] * 5 + ["答案是十二人"]
    assert not _answer_leaks_into_docs(["12人"], docs, docs_limit=5)


def test_resolve_judge_config_defaults_to_synth_client():
    # no override -> judge reuses the synth provider/model (current shared-client behavior)
    assert _resolve_judge_config(
        synth_provider="deepseek",
        synth_model="deepseek-v4-pro",
        judge_provider=None,
        judge_model=None,
    ) == ("deepseek", "deepseek-v4-pro")


def test_resolve_judge_config_override_separates_judge_from_synth():
    # deepseek synth + vertex judge -> clean, non-self-eval comparison
    assert _resolve_judge_config(
        synth_provider="deepseek",
        synth_model="deepseek-v4-pro",
        judge_provider="vertex",
        judge_model="gemini-3.1-pro-preview",
    ) == ("vertex", "gemini-3.1-pro-preview")


def test_resolve_judge_config_other_provider_without_model_takes_its_default():
    # a vertex judge without a model name must not receive the deepseek model name
    assert _resolve_judge_config(
        synth_provider="deepseek",
        synth_model="deepseek-v4-pro",
        judge_provider="vertex",
        judge_model=None,
    ) == ("vertex", "gemini-3.1-pro-preview")


def test_resolve_final_prompt_defaults_to_production():
    # no override file -> production _FINAL_SYSTEM_PROMPT is used unchanged
    assert _resolve_final_prompt(None, default="PROD_PROMPT") == "PROD_PROMPT"


def test_resolve_final_prompt_reads_override_file(tmp_path: Path):
    # external hardened prompt file overrides, without touching project prompts
    f = tmp_path / "hardened.txt"
    f.write_text("  HARDENED GROUNDING PROMPT\n", encoding="utf-8")
    assert _resolve_final_prompt(f, default="PROD_PROMPT") == "HARDENED GROUNDING PROMPT"
