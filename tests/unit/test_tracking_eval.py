"""Tests for the tracking-task acceptance runner (plan 28.3)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.types import Answer, Source
from eval.common import read_choices
from eval.tracking import run_tracking as rt

CHECKED = datetime(2026, 11, 10, tzinfo=rt.BEIJING)


def src(n, url, name="TechCrunch", kind="media", story=None, article=None, article_kind="news", event_at="2026-11-12T00:00:00+00:00"):
    return {"n": n, "title": f"t{n}", "source": name, "published_at": "2026-11-12", "url": url,
            "article_id": article, "story_id": story, "kind": article_kind, "publisher_kind": kind, "event_at": event_at,
            "discussion_story_id": None}


def hn(n, story=None):
    return {**src(n, f"https://news.ycombinator.com/item?id={n}", name=rt.DISCUSSION_SOURCE, kind=None, article_kind=None, event_at=None),
            "discussion_story_id": story}


# ---------------------------------------------------------------- inventory

def test_inventory_recall_counts_expected_stories_covered_by_cited_sources() -> None:
    sources = [src(1, "u1", story=1, article=11), src(2, "u2", story=2, article=12), src(3, "u3", story=9, article=13)]
    m = rt.inventory_metrics([1, 2, 3, 4], sources, "- a [1]\n- b [2]\n- c [3]")
    assert (m["expected"], m["hit"]) == (4, 2)


def test_inventory_non_event_counts_distinct_cited_articles_of_the_three_kinds() -> None:
    sources = [src(1, "u1", article=11, article_kind="tutorial"), src(2, "u2", article=12, article_kind="opinion"),
               src(3, "u3", article=13, article_kind="promo"), src(4, "u4", article=14, article_kind="news"),
               src(5, "u5", article=15, article_kind="project"), src(6, "u6", article=11, article_kind="tutorial"), hn(7)]
    assert rt.inventory_metrics([1], sources, "")["non_event"] == 3


@pytest.mark.parametrize("line,is_list", [
    ("- item [1]", True), ("* item [1]", True), ("1. item [1]", True), ("12) item [1]", True), ("3、item [1]", True),
    ("**bold** [1]", False), ("2026.11.20 发布 [1]", False), ("-5 degrees", False), ("plain [1]", False),
])
def test_list_lines_are_recognized_by_marker(line: str, is_list: bool) -> None:
    m = rt.inventory_metrics([1], [], line.replace(" [1]", ""))
    assert m["list_lines"] == (1 if is_list else 0)


def test_all_cited_requires_a_marker_on_every_list_line() -> None:
    ok = rt.inventory_metrics([1], [], "intro\n- a [1]\n1. b [2][3]\n   * c [4]")
    bad = rt.inventory_metrics([1], [], "- a [1]\n- b\n2. c [2]")
    assert (ok["list_lines"], ok["uncited_lines"], ok["all_cited"]) == (3, 0, True)
    assert (bad["list_lines"], bad["uncited_lines"], bad["all_cited"]) == (3, 1, False)


def test_a_marker_needs_its_closing_bracket() -> None:
    assert rt.inventory_metrics([1], [], "- a [1")["uncited_lines"] == 1
    assert rt.inventory_metrics([1], [], "- a [x]")["uncited_lines"] == 1
    assert rt.first_positions("a [1 b [2]") == {2: 7}


# ---------------------------------------------------------------- tracking

def progress(*story_ids):
    return [{"article_id": 100 + i, "label": f"p{i}", "story_id": s} for i, s in enumerate(story_ids)]


def test_tracking_completeness_is_progress_whose_story_is_cited() -> None:
    sources = [src(1, "u1", story=1), src(2, "u2", story=3)]
    m = rt.tracking_metrics(progress(1, 2, 3, None), sources, "a [1] b [2]")
    assert (m["total"], m["covered"], m["covered_steps"]) == (4, 2, [True, False, True, False])


def test_tracking_order_follows_first_citation_position_of_covered_progress() -> None:
    sources = [src(1, "u1", story=1), src(2, "u2", story=2), src(3, "u3", story=3)]
    assert rt.tracking_metrics(progress(1, 2, 3), sources, "x [1] y [2] z [3]")["order_ok"]
    # progress 2 is introduced before progress 1
    assert not rt.tracking_metrics(progress(1, 2, 3), sources, "x [2] y [1] z [3]")["order_ok"]
    # the first citation of a progress counts, a later repeat does not
    assert rt.tracking_metrics(progress(1, 2), sources, "x [1] y [2] z [1]")["order_ok"]
    # an uncovered progress does not break the order of the others
    assert rt.tracking_metrics(progress(1, 9, 3), sources, "x [1] z [3]")["order_ok"]


def test_tracking_order_accepts_two_progress_items_sharing_one_citation() -> None:
    assert rt.tracking_metrics(progress(1, 1, 2), [src(1, "u1", story=1), src(2, "u2", story=2)], "x [1] y [2]")["order_ok"]


def test_tracking_progress_without_a_story_is_never_covered_by_a_source_without_one() -> None:
    m = rt.tracking_metrics(progress(None, 1), [src(1, "u1", story=None), src(2, "u2", story=1)], "x [1] y [2]")
    assert m["covered_steps"] == [False, True]


def test_tracking_order_uses_the_earliest_marker_among_sources_of_one_story() -> None:
    # story 1 is cited by [2] before story 2's [3], and again by [1] after it
    sources = [src(1, "u1", story=1), src(2, "u2", story=1), src(3, "u3", story=2)]
    assert rt.tracking_metrics(progress(1, 2), sources, "x [2] y [3] z [1]")["order_ok"] is True
    sources = [src(1, "u1", story=1), src(2, "u2", story=1), src(3, "u3", story=2)]
    assert rt.tracking_metrics(progress(1, 2), sources, "x [3] y [2]")["order_ok"] is False
    assert rt.tracking_metrics(progress(1, 2), sources, "x [2] y [1] z [3]")["order_ok"] is True


def test_old_progress_is_a_cited_progress_story_source_before_the_checked_date() -> None:
    steps = progress(1, 2)
    old = src(1, "u1", story=1, event_at="2026-11-09T15:00:00+00:00")  # 2026-11-09 23:00 Beijing, before 11-10 00:00
    new = src(2, "u2", story=2, event_at="2026-11-09T16:00:00+00:00")  # exactly 11-10 00:00 Beijing
    other = src(3, "u3", story=7, event_at="2026-01-01T00:00:00+00:00")  # old, but not a progress story
    assert rt.old_progress_citations([old, new, other], steps, CHECKED) == [1]
    assert rt.old_progress_citations([new, other], steps, CHECKED) == []
    assert rt.old_progress_citations([src(4, "u4", story=1, event_at=None)], steps, CHECKED) == []


# ---------------------------------------------------------------- contrast

def test_contrast_roles_need_official_media_and_the_discussion_source_of_the_story() -> None:
    full = [src(1, "u", kind="official", story=3), src(2, "u", kind="media", story=3), hn(3, story=3)]
    assert rt.contrast_roles(full, 3) == ["discussion", "media", "official"]
    assert rt.contrast_roles([src(1, "u", kind="community", story=3), src(2, "u", kind="personal", story=3),
                              src(3, "u", kind=None, story=3)], 3) == []
    # a source named like a discussion is a discussion whatever article it matched
    assert rt.contrast_roles([{**src(1, "u", name=rt.DISCUSSION_SOURCE, kind="official", story=3), "discussion_story_id": 3}], 3) == ["discussion"]


def test_contrast_roles_ignore_sources_outside_the_story() -> None:
    assert rt.contrast_roles([src(1, "u", kind="official", story=4), src(2, "u", kind="media", story=3), hn(3, story=3)], 3) == ["discussion", "media"]
    assert rt.contrast_roles([src(1, "u", kind="official", story=3), src(2, "u", kind="media", story=3), hn(3, story=4)], 3) == ["media", "official"]
    # unresolved sources belong to no story
    assert rt.contrast_roles([src(1, "u", kind="official", story=None), hn(2, story=None)], 3) == []


def test_discussion_story_does_not_count_for_recall_or_coverage() -> None:
    sources = [hn(1, story=1)]
    assert rt.cited_story_ids(sources) == set()
    assert rt.tracking_metrics(progress(1), sources, "x [1]")["covered"] == 0


# ---------------------------------------------------------------- source matching

def test_resolve_sources_matches_by_url_key_and_skips_discussions() -> None:
    asked = []
    table = {"techcrunch.com/a": {"article_id": 5, "story_id": 50, "kind": "news", "publisher_kind": "media", "event_at": "2026-11-12T00:00:00+00:00"}}

    def lookup(keys):
        asked.extend(keys)
        return table

    sources = (
        Source(1, "http://www.techcrunch.com/a/?utm_source=x", "A", "", "TechCrunch", "2026-11-12"),
        Source(2, "https://news.ycombinator.com/item?id=1", "B", "", rt.DISCUSSION_SOURCE, ""),
        Source(3, "https://other.com/z", "C", "", "Other", "2026-11-12"),
    )
    asked_discussions = []

    def lookup_discussions(keys):
        asked_discussions.extend(keys)
        return {"news.ycombinator.com/item?id=1": 60}

    out = rt.resolve_sources(sources, lookup, lookup_discussions)
    assert asked == ["techcrunch.com/a", "other.com/z"] and asked_discussions == ["news.ycombinator.com/item?id=1"]
    assert [(s["article_id"], s["story_id"], s["kind"], s["publisher_kind"]) for s in out] == [
        (5, 50, "news", "media"), (None, None, None, None), (None, None, None, None)]
    assert [s["discussion_story_id"] for s in out] == [None, 60, None]


def test_discussion_stories_come_from_the_discussed_article() -> None:
    class Cur:
        def __init__(self):
            self.results, self.sqls = [[(1, 5, "https://news.ycombinator.com/item?id=9", "t", 10, 3)],
                                       [("k", 5, 50, "news", "media", None)]], []

        def execute(self, sql, params):
            self.sqls.append((sql, params))

        def fetchall(self):
            return self.results.pop(0)

    cur = Cur()
    assert rt.lookup_discussion_stories(cur, ["news.ycombinator.com/item?id=9"]) == {"news.ycombinator.com/item?id=9": 50}
    assert cur.sqls[0][1] == (["https://news.ycombinator.com/item?id=9"],) and cur.sqls[1][1] == ([5],)


def test_lookup_queries_map_rows_to_dicts() -> None:
    class Cur:
        def __init__(self, rows):
            self.rows, self.sql = rows, None

        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def fetchall(self):
            return self.rows

    at = datetime(2026, 11, 12, tzinfo=timezone.utc)
    cur = Cur([("k", 5, 50, "news", "media", at)])
    assert rt.lookup_by_url_keys(cur, ["k"]) == {"k": {"article_id": 5, "story_id": 50, "kind": "news", "publisher_kind": "media", "event_at": at.isoformat()}}
    assert "a.url_key = ANY" in cur.sql and cur.params == (["k"],)
    cur = Cur([("k", 5, None, "news", "media", at)])
    assert rt.lookup_story_ids(cur, [5]) == {5: None}
    assert "a.article_id = ANY" in cur.sql


# ---------------------------------------------------------------- input

def write_jsonl(path: Path, *items) -> Path:
    path.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8")
    return path


def test_load_cases_reports_a_missing_file_with_an_example(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        rt.load_cases(tmp_path / "inventory.jsonl", "inventory")
    assert "not found" in str(exc.value) and "expected_story_ids" in str(exc.value)


@pytest.mark.parametrize("set_name,item,message", [
    ("inventory", {"case_id": "a", "question": "q"}, "expected_story_ids"),
    ("inventory", {"case_id": "a", "question": "q", "expected_story_ids": [True]}, "expected_story_ids"),
    ("inventory", {"case_id": "a" * 41, "question": "q", "expected_story_ids": [1]}, "longer than"),
    ("inventory", {"question": "q", "expected_story_ids": [1]}, "required"),
    ("tracking", {"case_id": "a", "question": "q", "progress": [{"article_id": 1}]}, "progress item"),
    ("tracking", {"case_id": "a", "question": "q", "progress": [{"article_id": 1, "label": "x"}], "follow_query": "f"}, "together"),
    ("tracking", {"case_id": "a", "question": "q", "progress": [{"article_id": 1, "label": "x"}],
                  "follow_query": "f", "checked_at": "11/10", "followup_question": "q2"}, "checked_at"),
    ("contrast", {"case_id": "a", "question": "q", "story_id": "7"}, "story_id"),
])
def test_load_cases_rejects_malformed_lines(tmp_path: Path, set_name: str, item: dict, message: str) -> None:
    path = write_jsonl(tmp_path / "s.jsonl", item)
    with pytest.raises(SystemExit) as exc:
        rt.load_cases(path, set_name)
    assert message in str(exc.value)


def test_load_cases_rejects_duplicate_case_ids_and_accepts_a_full_tracking_case(tmp_path: Path) -> None:
    case = {"case_id": "t1", "question": "q", "progress": [{"article_id": 1, "label": "x"}],
            "follow_query": "f", "checked_at": "2026-11-10", "followup_question": "q2"}
    assert rt.load_cases(write_jsonl(tmp_path / "t.jsonl", case), "tracking")[0]["checked_at"] == "2026-11-10"
    with pytest.raises(SystemExit, match="duplicate"):
        rt.load_cases(write_jsonl(tmp_path / "d.jsonl", case, case), "tracking")


# ---------------------------------------------------------------- fakes

class World:
    """Library rows, model answers and the event log shared by the fake dependencies."""

    def __init__(self, answers, articles, stories=None, discussions=None):
        self.answers, self.articles, self.stories, self.log = answers, articles, stories or {}, []
        self.discussions = discussions or {}
        self.fail_on = set()

    def ask(self, question, *, request_id, user_key=None):
        self.log.append(("ask", question, user_key, request_id))
        if question in self.fail_on:
            raise RuntimeError("boom")
        return self.answers[question]

    def deps(self):
        return rt.Deps(ask=self.ask, lookup_urls=lambda keys: {k: self.articles[k] for k in keys if k in self.articles},
                       lookup_discussions=lambda keys: {k: self.discussions[k] for k in keys if k in self.discussions},
                       lookup_stories=lambda ids: {i: self.stories.get(i) for i in ids}, follows=self)

    # follow store
    def add(self, user_key, query):
        self.log.append(("add", user_key, query))
        return 7

    def set_checked(self, follow_id, when):
        self.log.append(("set_checked", follow_id, when))

    def delete(self, user_key):
        self.log.append(("delete", user_key))


def row(article, story, kind="news", publisher_kind="media", event_at="2026-11-12T00:00:00+00:00"):
    return {"article_id": article, "story_id": story, "kind": kind, "publisher_kind": publisher_kind, "event_at": event_at}


def answer(text, *sources, status="answered"):
    return Answer(status=status, text=text, sources=tuple(sources), tool_calls=("search_news",), request_id="r")


def s(n, host, name="TechCrunch"):
    return Source(n, f"https://{host}/p", f"t{n}", "", name, "2026-11-12")


# ---------------------------------------------------------------- since-last check

def tracking_case(**extra):
    case = {"case_id": "t1", "question": "q", "progress": [{"article_id": 1, "label": "a"}, {"article_id": 2, "label": "b"}],
            "follow_query": "f", "checked_at": "2026-11-10", "followup_question": "q2"}
    return {**case, **extra}


def test_since_last_runs_create_set_ask_delete_in_order_with_the_user_key() -> None:
    world = World({"q2": answer("x [1]", s(1, "a.com"))}, {"a.com/p": row(11, 1, event_at="2026-11-11T00:00:00+00:00")})
    result = rt.check_since_last({**tracking_case(), "progress": progress(1, 2)}, world.deps())
    kinds = [e[0] for e in world.log]
    assert kinds == ["delete", "add", "set_checked", "ask", "delete"]
    assert world.log[1] == ("add", "eval:tracking", "f")
    assert world.log[2] == ("set_checked", 7, CHECKED)
    assert world.log[3][1:3] == ("q2", "eval:tracking")
    assert world.log[0][1] == world.log[4][1] == "eval:tracking"
    assert result["passed"] is True and result["old_cited"] == []


def test_since_last_fails_when_an_old_progress_is_cited() -> None:
    world = World({"q2": answer("x [1]", s(1, "a.com"))}, {"a.com/p": row(11, 1, event_at="2026-11-01T00:00:00+00:00")})
    result = rt.check_since_last({**tracking_case(), "progress": progress(1, 2)}, world.deps())
    assert result["passed"] is False and result["old_cited"] == [1]


@pytest.mark.parametrize("failing_step", ["add", "set_checked", "ask"])
def test_since_last_deletes_the_follow_when_a_step_fails(failing_step: str) -> None:
    world = World({"q2": answer("x")}, {})
    if failing_step == "ask":
        world.fail_on.add("q2")
    else:
        original = getattr(world, failing_step)

        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("boom")

        setattr(world, failing_step, broken)
    result = rt.check_since_last({**tracking_case(), "progress": progress(1)}, world.deps())
    assert world.log[-1] == ("delete", "eval:tracking")
    assert result["status"] == "error" and result["error_code"] == "runtime_runtimeerror" and result["passed"] is None
    assert rt.needs_rerun({"status": "answered", "followup": result})


def test_request_ids_fit_the_trace_column() -> None:
    for kind in rt.REQUEST_PREFIX:
        assert len(rt._request_id(kind, "x" * rt.MAX_CASE_ID_CHARS)) <= 64


# ---------------------------------------------------------------- runs

def inventory_world():
    return World(
        {"qi": answer("- a [1]\n- b [2]", s(1, "a.com"), s(2, "b.com"))},
        {"a.com/p": row(11, 1), "b.com/p": row(12, 2, kind="promo")},
    )


def test_run_set_writes_records_and_resumes_only_errored_cases(tmp_path: Path) -> None:
    world = inventory_world()
    world.answers["qe"] = Answer(status="error", text="sorry", error_code="x")
    cases = [{"case_id": "i1", "question": "qi", "expected_story_ids": [1, 2, 3]},
             {"case_id": "i2", "question": "qe", "expected_story_ids": [1]}]
    out = tmp_path / "runs" / "inventory.jsonl"
    assert rt.run_set("inventory", cases, out, world.deps(), 0) == ["i1", "i2"]
    first = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["case_id"] for r in first] == ["i1", "i2"]
    assert first[0]["metrics"] == {"expected": 3, "hit": 2, "non_event": 1, "list_lines": 2, "uncited_lines": 0, "all_cited": True}
    assert first[0]["sources"][1]["story_id"] == 2 and first[0]["tool_calls"] == ["search_news"]

    world.log.clear()
    world.answers["qe"] = answer("- a [1]", s(1, "a.com"))
    assert rt.run_set("inventory", cases, out, world.deps(), 0) == ["i2"]
    assert [e[1] for e in world.log] == ["qe"]
    assert rt.run_set("inventory", cases, out, world.deps(), 0) == []
    assert len(out.read_text(encoding="utf-8").splitlines()) == 3  # the rerun was appended, not rewritten


def test_ask_for_the_main_question_has_no_user_key_and_a_set_prefixed_request_id() -> None:
    world = inventory_world()
    rt.run_case("inventory", {"case_id": "i1", "question": "qi", "expected_story_ids": [1]}, world.deps())
    kind, question, user_key, request_id = world.log[0]
    assert user_key is None and request_id.startswith("eval_inv_i1_")


def test_tracking_case_resolves_progress_stories_and_runs_the_followup() -> None:
    world = World({"q": answer("a [1] b [2]", s(1, "a.com"), s(2, "b.com")), "q2": answer("c [1]", s(1, "b.com"))},
                  {"a.com/p": row(11, 5), "b.com/p": row(12, 6, event_at="2026-11-12T00:00:00+00:00")},
                  stories={1: 5, 2: 6})
    record = rt.run_case("tracking", tracking_case(question="q", progress=[{"article_id": 1, "label": "a"}, {"article_id": 2, "label": "b"}]), world.deps())
    assert [p["story_id"] for p in record["progress"]] == [5, 6]
    assert record["metrics"]["covered"] == 2 and record["metrics"]["order_ok"] is True
    assert record["followup"]["passed"] is True and record["follow_query"] == "f"
    assert [e[0] for e in world.log] == ["ask", "delete", "add", "set_checked", "ask", "delete"]


def test_tracking_case_without_follow_fields_makes_no_followup_and_no_writes() -> None:
    world = World({"q": answer("a [1]", s(1, "a.com"))}, {"a.com/p": row(11, 1)}, stories={1: 1})
    case = {"case_id": "t1", "question": "q", "progress": [{"article_id": 1, "label": "a"}]}
    record = rt.run_case("tracking", case, world.deps())
    assert "followup" not in record and [e[0] for e in world.log] == ["ask"]


def test_report_counts_unmatched_sources_but_not_discussions() -> None:
    record = {"case_id": "i1", "question": "q", "request_id": "r", "status": "answered", "error_code": None, "answer": "- a [1][2][3]",
              "sources": [src(1, "u1", article=1, story=1), src(2, "u2"), hn(3)],
              "metrics": {"expected": 1, "hit": 1, "non_event": 0, "list_lines": 1, "uncited_lines": 0, "all_cited": True}}
    record["sources"][1]["article_id"] = None
    report = rt.build_report({"inventory": [record]}, None)
    assert "未对回库内文章的来源：1 个" in report


def test_contrast_case_counts_only_sources_of_its_story() -> None:
    discussion = Source(3, "https://news.ycombinator.com/item?id=3", "d", "", rt.DISCUSSION_SOURCE, "")
    world = World({"qc": answer("x [1][2][3]", s(1, "o.com", "Official"), s(2, "m.com"), discussion)},
                  {"o.com/p": row(21, 3, publisher_kind="official"), "m.com/p": row(22, 3)},
                  discussions={"news.ycombinator.com/item?id=3": 3})
    case = {"case_id": "c1", "question": "qc", "story_id": 3}
    assert rt.run_case("contrast", case, world.deps())["metrics"]["roles"] == ["discussion", "media", "official"]
    assert rt.run_case("contrast", {**case, "story_id": 4}, world.deps())["metrics"]["roles"] == []
    world.discussions = {"news.ycombinator.com/item?id=3": 8}
    assert rt.run_case("contrast", case, world.deps())["metrics"]["roles"] == ["media", "official"]


def test_errored_main_answer_skips_the_followup() -> None:
    world = World({"q": Answer(status="error", text="x", error_code="e")}, {}, stories={1: 1, 2: 2})
    record = rt.run_case("tracking", tracking_case(question="q"), world.deps())
    assert "followup" not in record and [e[0] for e in world.log] == ["ask"]


# ---------------------------------------------------------------- summaries

def inv_record(hit, expected, non_event=0, all_cited=True):
    return {"metrics": {"hit": hit, "expected": expected, "non_event": non_event, "all_cited": all_cited}}


def test_inventory_thresholds_are_inclusive_for_recall_and_strict_for_the_rest() -> None:
    def verdicts(*records):
        return [row[3] for row in rt.inventory_summary(list(records))]

    assert verdicts(inv_record(7, 10)) == ["通过", "通过", "通过"]
    assert verdicts(inv_record(6, 10))[0] == "未通过"
    assert verdicts(inv_record(7, 10, non_event=1))[1] == "未通过"
    assert verdicts(inv_record(7, 10), inv_record(7, 10, all_cited=False))[2] == "未通过"
    assert verdicts(inv_record(5, 10), inv_record(9, 10))[0] == "通过"  # pooled over all expected events
    assert rt.inventory_summary([])[0][3] == "未通过"


def test_tracking_summary_pools_completeness_and_counts_orders_and_followups() -> None:
    def rec(total, covered, order_ok=True, passed=None):
        out = {"metrics": {"total": total, "covered": covered, "order_ok": order_ok}}
        if passed is not None:
            out["followup"] = {"passed": passed}
        return out

    rows = rt.tracking_summary([rec(5, 4, passed=True), rec(5, 4, passed=True)])
    assert [r[3] for r in rows] == ["通过", "通过", "通过"]
    mixed = rt.tracking_summary([rec(5, 4, passed=True), rec(5, 4, passed=False)])
    assert mixed[2][1:] == ("1 / 2", "全部", "未通过")
    rows = rt.tracking_summary([rec(5, 3), rec(5, 4, order_ok=False, passed=False)])
    assert [r[3] for r in rows] == ["未通过", "未通过", "未通过"] and rows[2][1] == "0 / 1"
    assert rt.tracking_summary([rec(5, 5)])[2][3] == "无用例"
    assert rt.tracking_summary([rec(5, 4)])[0][3] == "通过"  # 80% exactly


def test_contrast_summary_role_coverage_and_manual_columns() -> None:
    full = {"metrics": {"roles": ["discussion", "media", "official"]}}
    part = {"metrics": {"roles": ["media", "official"]}}
    rows = rt.contrast_summary([full] * 9 + [part], None)
    assert rows[0][3] == "通过" and [r[3] for r in rows[1:]] == ["待判断", "待判断"]
    assert rt.contrast_summary([full] * 8 + [part] * 2, None)[0][3] == "未通过"
    stats = {"归属错误": {"judged": 10, "flagged": 0, "total": 10}, "评论表述为事实": {"judged": 4, "flagged": 1, "total": 10}}
    rows = rt.contrast_summary([full], stats)
    assert [r[3] for r in rows[1:]] == ["通过", "未通过"]
    stats["归属错误"] = {"judged": 4, "flagged": 0, "total": 10}
    assert rt.contrast_summary([full], stats)[1][3] == "待判断"


# ---------------------------------------------------------------- sheet

def contrast_record(case_id, status="answered"):
    return {"case_id": case_id, "question": f"q-{case_id}", "story_id": 3, "status": status, "answer": "line1\n## 1. `c9`\n- 归属错误：`[y]`",
            "sources": [src(1, "u1", kind="official", story=3)], "metrics": {"roles": ["official"]}}


def fill(text: str, values: dict[str, tuple[str, str]]) -> str:
    out, current = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.split("`")[1]
        for i, field in enumerate(rt.SHEET_FIELDS):
            if line.startswith(f"- {field}：") and current in values:
                line = f"- {field}：`[{values[current][i]}]`"
        out.append(line)
    return "\n".join(out) + "\n"


def test_sheet_read_back_counts_flagged_judged_and_ignores_quoted_answer_text(tmp_path: Path) -> None:
    records = [contrast_record(f"c{i}") for i in range(4)]
    sheet = tmp_path / "contrast_sheet.md"
    sheet.write_text(rt.render_sheet(records, {}), encoding="utf-8")
    assert rt.sheet_scores(sheet) == {f: {"judged": 0, "flagged": 0, "total": 4} for f in rt.SHEET_FIELDS}
    sheet.write_text(fill(sheet.read_text(encoding="utf-8"), {"c0": ("n", "n"), "c1": ("y", "n"), "c2": ("?", "y"), "c3": ("", "N")}), encoding="utf-8")
    assert rt.sheet_scores(sheet) == {"归属错误": {"judged": 2, "flagged": 1, "total": 4}, "评论表述为事实": {"judged": 4, "flagged": 1, "total": 4}}
    assert list(read_choices(sheet, "归属错误")) == ["c0", "c1", "c2", "c3"]


def test_sheet_rejects_a_value_other_than_y_n_or_blank(tmp_path: Path) -> None:
    sheet = tmp_path / "s.md"
    sheet.write_text(fill(rt.render_sheet([contrast_record("c0")], {}), {"c0": ("yes", "")}), encoding="utf-8")
    with pytest.raises(ValueError, match="c0"):
        rt.sheet_scores(sheet)


def test_write_sheet_keeps_filled_judgments_except_for_rerun_cases(tmp_path: Path) -> None:
    sheet = tmp_path / "s.md"
    records = [contrast_record("c0"), contrast_record("c1"), contrast_record("c2", status="error")]
    rt.write_sheet(sheet, records, set())
    assert "`c2`" not in sheet.read_text(encoding="utf-8")  # errored cases are not judged
    sheet.write_text(fill(sheet.read_text(encoding="utf-8"), {"c0": ("n", "y"), "c1": ("y", "n")}), encoding="utf-8")
    rt.write_sheet(sheet, records, {"c1"})
    choices = {f: read_choices(sheet, f) for f in rt.SHEET_FIELDS}
    assert choices["归属错误"] == {"c0": "n", "c1": None} and choices["评论表述为事实"] == {"c0": "y", "c1": None}


# ---------------------------------------------------------------- end to end

def write_sets(directory: Path) -> None:
    write_jsonl(directory / "inventory.jsonl", {"case_id": "i1", "question": "qi", "expected_story_ids": [1, 2]})
    write_jsonl(directory / "tracking.jsonl", tracking_case(question="qt"))
    write_jsonl(directory / "contrast.jsonl", {"case_id": "c1", "question": "qc", "story_id": 3})


def full_world():
    return World(
        {"qi": answer("- a [1]\n- b [2]", s(1, "a.com"), s(2, "b.com")),
         "qt": answer("a [1] b [2]", s(1, "a.com"), s(2, "b.com")),
         "q2": answer("c [1]", s(1, "b.com")),
         "qc": answer("官方说 [1]，媒体报道 [2]，社区认为 [3]", s(1, "o.com", "Official"), s(2, "m.com"),
                      Source(3, "https://news.ycombinator.com/item?id=3", "d", "", rt.DISCUSSION_SOURCE, ""))},
        {"a.com/p": row(11, 1), "b.com/p": row(12, 2), "o.com/p": row(21, 3, publisher_kind="official"), "m.com/p": row(22, 3)},
        stories={1: 1, 2: 2},
        discussions={"news.ycombinator.com/item?id=3": 3},
    )


def run_args(directory: Path, **kw):
    return argparse.Namespace(dir=directory, set=kw.get("set", "all"), only_case_id=kw.get("only"), sleep_seconds=0, env_file=".env")


def test_run_all_three_sets_writes_runs_report_and_sheet_and_resumes(tmp_path: Path) -> None:
    write_sets(tmp_path)
    world = full_world()
    assert rt.cmd_run(run_args(tmp_path), world.deps()) == 0
    for name in rt.SETS:
        assert (tmp_path / "runs" / f"{name}.jsonl").exists()
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    for text in ("## 盘点", "## 追踪", "## 对照", "不低于 70%", "不低于 80%", "不低于 90%", "自上次以来", "contrast_sheet.md"):
        assert text in report
    contrast = json.loads((tmp_path / "runs" / "contrast.jsonl").read_text(encoding="utf-8"))
    assert contrast["metrics"]["roles"] == ["discussion", "media", "official"]
    assert read_choices(tmp_path / "contrast_sheet.md", "归属错误") == {"c1": None}

    asks = [e for e in world.log if e[0] == "ask"]
    assert [e[2] for e in asks] == [None, None, "eval:tracking", None]
    world.log.clear()
    assert rt.cmd_run(run_args(tmp_path), world.deps()) == 0
    assert world.log == []  # nothing pending, so no questions and no writes


def test_only_case_id_runs_a_single_case_and_reports_no_match(tmp_path: Path) -> None:
    write_sets(tmp_path)
    world = full_world()
    assert rt.cmd_run(run_args(tmp_path, only="i1"), world.deps()) == 0
    assert [e[1] for e in world.log] == ["qi"]
    assert not (tmp_path / "runs" / "tracking.jsonl").exists()
    assert "未运行" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert rt.cmd_run(run_args(tmp_path, only="zz"), world.deps()) == 1


def test_score_prints_statistics_and_refreshes_the_report(tmp_path: Path, capsys) -> None:
    write_sets(tmp_path)
    rt.cmd_run(run_args(tmp_path), full_world().deps())
    sheet = tmp_path / "contrast_sheet.md"
    sheet.write_text(fill(sheet.read_text(encoding="utf-8"), {"c1": ("n", "y")}), encoding="utf-8")
    assert rt.cmd_score(argparse.Namespace(dir=tmp_path)) == 0
    out = capsys.readouterr().out
    assert "归属错误：0 条（已判断 1 / 1）" in out and "评论表述为事实：1 条（已判断 1 / 1）" in out
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "未通过" in report.split("## 对照")[1]


def test_score_without_a_sheet_says_what_to_run_first(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="contrast set"):
        rt.cmd_score(argparse.Namespace(dir=tmp_path))


def test_missing_input_file_stops_before_any_question(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "inventory.jsonl", {"case_id": "i1", "question": "qi", "expected_story_ids": [1]})
    world = full_world()
    with pytest.raises(SystemExit, match="tracking.jsonl"):
        rt.cmd_run(run_args(tmp_path), world.deps())
    assert world.log == []
