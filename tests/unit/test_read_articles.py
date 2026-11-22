from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from agent.config import AgentConfig
from agent.evidence import EvidenceRegistry
from agent.tools import read
from agent.tools.registry import ReadArticlesArgs, TurnState, prepare_call
from agent.types import Article, Discussion
from common.urls import url_key

ARTICLE = Article(
    article_id=30, url="https://www.techcrunch.com/genie-4/", title="Genie 4 adds audio", title_zh="",
    summary="谷歌发布 Genie 4。", topics=("ai",), kind="release", publisher="TechCrunch", publisher_kind="media",
    lang="en", published_at=datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc), body_level="full",
    hn_score=None, hn_comments=None, hn_sighting_id=None, story_id=5,
)
DISCUSSION = Discussion(sighting_id=70, article_id=30, url="https://news.ycombinator.com/item?id=1",
                        title="Genie 4 adds audio", score=955, comment_total=312)


def _fake_db(monkeypatch, *, articles=(), discussions=(), bodies=None, discussion_texts=None):
    class Conn:
        def cursor(self):
            return self

        def close(self):
            pass

    def by_keys(cur, keys):
        return [a for a in articles if url_key(a.url) in keys]

    discussion_lookups = []

    def discussions_by_keys(cur, keys):
        discussion_lookups.append(list(keys))
        return [d for d in discussions if url_key(d.url) in keys]

    monkeypatch.setattr(read, "get_conn", Conn)
    monkeypatch.setattr(read, "put_conn", lambda conn: None)
    monkeypatch.setattr(read, "fetch_articles_by_url_keys", by_keys)
    monkeypatch.setattr(read, "fetch_discussions_by_url_keys", discussions_by_keys)
    monkeypatch.setattr(read, "fetch_bodies", lambda cur, ids: {i: t for i, t in (bodies or {}).items() if i in ids})
    monkeypatch.setattr(read, "fetch_discussion_texts",
                        lambda cur, ids: {i: t for i, t in (discussion_texts or {}).items() if i in ids})
    return discussion_lookups


def _read(registry, state=None, **fields):
    args = ReadArticlesArgs(**fields)
    result = read.run_read_articles(args, registry=registry, state=state or TurnState(user_urls=set()))
    return result, read.commit_read_articles(args, result, registry=registry)


def test_unknown_id_fails_and_all_failed_is_not_ok(monkeypatch):
    _fake_db(monkeypatch)
    result, outcome = _read(EvidenceRegistry(), ids=["E9"])
    assert result.items[0].error == "未知编号"
    assert outcome.ok is False
    assert outcome.gather_text == "read_articles 结果：\n[E9] 读取失败：未知编号。"


def test_url_must_come_from_user_unless_mcp(monkeypatch):
    _fake_db(monkeypatch, articles=[ARTICLE], bodies={30: "正文"})
    url = "https://techcrunch.com/genie-4"

    result, outcome = _read(EvidenceRegistry(), urls=[url])
    assert result.items[0].error == "该链接不是用户提供的"
    assert outcome.gather_text.endswith(f"[{url}] 读取失败：该链接不是用户提供的。")

    for state in (TurnState(user_urls={url_key(url)}), TurnState(user_urls=set(), allow_any_library_url=True)):
        result, outcome = _read(EvidenceRegistry(), state, urls=[url])
        assert result.items[0].error is None and outcome.ok is True


def test_url_not_in_library(monkeypatch):
    _fake_db(monkeypatch)
    url = "https://example.com/other"
    result, outcome = _read(EvidenceRegistry(), TurnState(user_urls={url_key(url)}), urls=[url])
    assert result.items[0].error == "该链接不在新闻库内"
    assert outcome.ok is False


def test_read_limit_exhausted():
    state = TurnState(user_urls=set(), read_count=8)
    outcome = prepare_call(0, {"name": "read_articles", "args": {"ids": ["E1"]}, "id": "c"},
                           state=state, config=AgentConfig(max_read_articles=8))
    assert (outcome.ok, outcome.gather_text) == (False, "已达到本次请求的读取上限。")
    assert state.read_count == 8


def test_article_without_body_registers_summary_and_skips_fetch(monkeypatch):
    excerpt = replace(ARTICLE, body_level="excerpt")
    _fake_db(monkeypatch, articles=[excerpt], bodies={30: "不应读到"})
    registry = EvidenceRegistry()
    url = "https://techcrunch.com/genie-4"

    result, outcome = _read(registry, TurnState(user_urls={url_key(url)}), urls=[url])

    assert (result.items[0].text, result.items[0].error) == (None, "没有入库正文")
    assert registry.get("E1").level == "summary"
    assert outcome.gather_text == "read_articles 结果：\n[E1] 读取失败：没有入库正文。"
    assert outcome.ok is False


def test_read_discussion_by_id(monkeypatch):
    _fake_db(monkeypatch, discussion_texts={70: "评论" * 10})
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")
    evidence = registry.add_discussion(DISCUSSION)

    result, outcome = _read(registry, ids=[evidence.eid])

    assert registry.get("E2").level == "fulltext"
    assert registry.get("E2").fulltext == "评论" * 10
    assert outcome.gather_text == (
        "read_articles 结果：\n[E2] HackerNews 讨论｜全文 20 字符\n开头：" + "评论" * 10
    )
    assert outcome.new_evidence == (registry.get("E2"),)


def test_read_discussion_by_url(monkeypatch):
    _fake_db(monkeypatch, discussions=[DISCUSSION], discussion_texts={70: "评论" * 10})
    url = "http://news.ycombinator.com/item?id=1"

    result, outcome = _read(EvidenceRegistry(), TurnState(user_urls=set()), urls=[url])
    assert result.items[0].error == "该链接不是用户提供的"

    registry = EvidenceRegistry()
    result, outcome = _read(registry, TurnState(user_urls={url_key(url)}), urls=[url])

    assert (result.items[0].article, result.items[0].discussion) == (None, DISCUSSION)
    evidence = registry.get("E1")
    assert (evidence.doc, evidence.level, evidence.fulltext) == ("discussion", "fulltext", "评论" * 10)
    assert outcome.gather_text == (
        "read_articles 结果：\n[E1] HackerNews 讨论｜全文 20 字符\n开头：" + "评论" * 10
    )
    assert outcome.new_evidence == (evidence,)
    assert outcome.ok is True


def test_discussion_lookup_only_for_urls_without_article(monkeypatch):
    lookups = _fake_db(monkeypatch, articles=[ARTICLE], discussions=[DISCUSSION],
                       bodies={30: "正文"}, discussion_texts={70: "评论"})
    article_url = "https://techcrunch.com/genie-4"
    discussion_url = "https://news.ycombinator.com/item?id=1"
    state = TurnState(user_urls=set(), allow_any_library_url=True)

    result, _ = _read(EvidenceRegistry(), state, urls=[article_url, discussion_url])

    assert [(i.article, i.discussion) for i in result.items] == [(ARTICLE, None), (None, DISCUSSION)]
    assert lookups == [[url_key(discussion_url)]]


def test_discussion_url_not_in_library(monkeypatch):
    _fake_db(monkeypatch)
    url = "https://news.ycombinator.com/item?id=2"
    result, outcome = _read(EvidenceRegistry(), TurnState(user_urls={url_key(url)}), urls=[url])
    assert result.items[0].error == "该链接不在新闻库内"
    assert outcome.ok is False


def test_new_evidence_leaves_out_items_already_citable(monkeypatch):
    _fake_db(monkeypatch, bodies={30: "正文"}, discussion_texts={70: "评论"})
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "summary")
    registry.add_discussion(DISCUSSION)

    _, outcome = _read(registry, ids=["E1", "E2"])

    assert registry.get("E1").level == "fulltext"
    assert [evidence.eid for evidence in outcome.new_evidence] == ["E2"]


def test_truncation_and_preview(monkeypatch):
    body = "a" * 700 + "b" * 300 + "c" * 500
    _fake_db(monkeypatch, bodies={30: body})
    registry = EvidenceRegistry()
    registry.add_article(ARTICLE, "listed")

    result, outcome = _read(registry, ids=["E1", "E5"], max_chars=1000)

    evidence = registry.get("E1")
    assert (evidence.level, evidence.fulltext, evidence.fulltext_total_chars, evidence.truncated) == (
        "fulltext", body[:1000], 1500, True)
    assert outcome.gather_text.splitlines() == [
        "read_articles 结果：",
        "[E1] 《Genie 4 adds audio》TechCrunch · 2026-11-12｜全文 1500 字符，已读取前 1000 字符",
        "开头：" + body[:800],
        "[E5] 读取失败：未知编号。",
    ]
    assert outcome.ok is True
