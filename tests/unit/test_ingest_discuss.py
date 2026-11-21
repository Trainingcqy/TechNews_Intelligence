"""Tests for the HackerNews discussion formatting."""

from __future__ import annotations

import json

from ingest.discuss import parse_discussion


def _comment(cid, author, text, at, children=()):
    return {"id": cid, "author": author, "text": text, "created_at_i": at, "type": "comment", "children": list(children)}


def _parse(children, points=120):
    return parse_discussion(json.dumps({"id": 1, "type": "story", "points": points, "text": None, "children": children}).encode())


def test_orders_by_replies_then_time():
    reply = lambda cid: _comment(cid, "r", "ok", 50)  # noqa: E731
    discussion = _parse(
        [
            _comment(10, "early", "none", 1),
            _comment(11, "many", "two", 2, [reply(20), reply(21)]),
            _comment(12, "late", "one", 4, [reply(22)]),
            _comment(13, "tied", "one", 3, [reply(23)]),
        ]
    )
    heads = [block.split("\n")[0] for block in discussion.text.split("\n\n")]
    assert heads == ["[1] many · 回复 2 条", "[2] tied · 回复 1 条", "[3] late · 回复 1 条", "[4] early · 回复 0 条"]


def test_strips_html_and_truncates():
    discussion = _parse([_comment(10, "a", "<p>x&#x27;y</p><p>" + "z" * 900, 1)])
    assert discussion.text == "[1] a · 回复 0 条\n" + ("x'y\n" + "z" * 900)[:800]


def test_drops_nul():
    assert _parse([_comment(10, "a", "x\x00y", 1)]).text == "[1] a · 回复 0 条\nxy"


def test_keeps_the_top_thirty():
    discussion = _parse([_comment(i, f"u{i}", "t", i) for i in range(31)])
    assert discussion.text.count("\n\n") == 29
    assert discussion.text.endswith("[30] u29 · 回复 0 条\nt")


def test_counts_comments_with_text():
    deleted = _comment(11, None, None, 2, [_comment(20, "b", "orphan", 5)])
    thread = _comment(10, "a", "top", 1, [_comment(21, None, None, 6, [_comment(30, "c", "deep", 7)])])
    discussion = _parse([deleted, thread], points=77)
    assert (discussion.points, discussion.comment_total) == (77, 3)
    assert discussion.text == "[1] a · 回复 1 条\ntop"
