"""Article text in Jina Reader markdown.

Reader returns either the article alone or the whole page around it: navigation, cookie and privacy notices, related
links, newsletter boxes, the footer. The article is one contiguous block of the page, so it is taken as the run of lines
with the highest total score. Prose scores its word count, short lines such as menu items cost a little, boilerplate
costs its length and a section label such as "Related" costs a lot. When the page repeats the title as a heading, the
run starts after it.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’.-]*")
_MARKUP = re.compile(r"^\s*(?:#{1,6}\s+|[*+-]\s+|\d+[.)]\s+|>\s*)+")
_EMPHASIS = re.compile(r"^[\s*_`~]+|[\s*_`~]+$")
_TERMINAL = re.compile(r"[.!?:;。！？…][\"'”’)\]]*$")
_INVISIBLE = re.compile("[\u200b-\u200d\u2060\ufeff]")
_NEW_WINDOW = re.compile(r"\(opens in (?:a )?new (?:window|tab)\)", re.I)
# Reader's placeholder for an embedded player: [Video 3](https://...).
_MEDIA = re.compile(r"\[(?:Video|Audio|Image) \d+\]\([^)]*\)")
# Matched only in lines shorter than _LONG_WORDS: in a paragraph these phrases are more often what the article is about.
_BOILERPLATE = re.compile(
    r"we use cookies|cookies? (?:policy|preferences|settings|notice)|accept all|reject all|all rights reserved"
    r"|privacy policy|terms of (?:use|service)|by signing up|by submitting your email|follow us on|share this article"
    r"|found this article interesting|your privacy choices|manage (?:your )?(?:consent|preferences|cookies)"
    r"|do not sell or share|protected by recaptcha|(?:sign up|subscribe) (?:for|to) (?:our|the) .{0,40}newsletter",
    re.I,
)
_CALL_TO_ACTION = re.compile(
    r"(?:^|[.?!]\s+)(?:subscribe|sign up|sign in|log in)\b|\bnewsletters?\b|we may earn a (?:small )?commission", re.I
)
_COOKIE = re.compile(r"\bcookies?\b", re.I)
_VISITOR = re.compile(r"\b(?:you|your)\b", re.I)
_PARTNERS = re.compile(r"\bwe and our (?:\d+ )?(?:partners|vendors)\b", re.I)
_PRIVACY = re.compile(
    r"cookies?|consent|opt(?:ed)?[- ]?outs?|personal (?:data|information|info)|targeted (?:advertising|ads)"
    r"|privacy (?:policy|choices|notice|rights)|legitimate interest",
    re.I,
)
# A section label after the article. "Read more" also introduces links inside an article or a newsletter, so it costs
# as much but does not end the article.
_SECTION_END = re.compile(
    r"more like this|related(?: articles| stories| posts| content| reading| coverage)?|read next|keep reading"
    r"|most (?:read|popular|viewed)|recommended(?: for you| stories| articles)?|you (?:may|might) also like"
    r"|more (?:from|on|in) .{1,40}|trending(?: now| stories)?|latest (?:news|stories|articles)"
    r"|popular (?:posts|stories|articles)|editor.s picks|this post is for paid members only|comments?|\d+ comments",
    re.I,
)
_READ_MORE = re.compile(r"read more", re.I)
_TITLE_WORD = re.compile(r"[a-z0-9]{3,}")
_TITLE_SEPARATOR = re.compile(r"\s+[|–—·-]\s+")
_LIST_ITEM = re.compile(r"^\s*(?:(?:[*+-]|\d+[.)])\s+|[•▪◦‣\u2600-\u27bf\U0001f300-\U0001faff]\ufe0f?\s*)\S")
# What a paywalled page shows after its free part: Ghost sites such as 404 Media, and SCMP Plus.
_PAYWALL = re.compile(
    r"this post is for paid members only|to access our exclusive content you['’]ll need to subscribe", re.I
)

_LONG_WORDS, _PROSE_WORDS, _SHORT_WORDS = 20, 10, 3
_SHORT_COST = 3
_LABEL_COST = 100
# Score from which a run counts as the article found, so that a section label or a stretch without text ends it.
_FOUND = 40
# A list of short items is more likely navigation, but its first items cost 1, as a short list in an article does.
_LENIENT_ITEMS = 8
_STATEMENT_WORDS = 3
_DIVERSITY_TOKENS, _MIN_DIVERSITY = 40, 0.5
_REPEATED = 3
_KEY_CHARS = 20
_PREFIX_WORDS = 40
_MAX_FILLER = 15
_EXTEND_FILLER = 2


def extract_article(content: str, title: str | None) -> str:
    """The article text of a Reader page, or the whole content when no line reads as prose.

    title is the article's title as the page or the feed gives it; the heading that repeats it marks the start.
    """
    lines = content.splitlines()
    span = _article_span(lines, title)
    if span is None:
        return content.strip()
    start, end = span
    kept, seen = [], set()
    for line in lines[start:end + 1]:
        key = _text(line).lower()
        if _MEDIA.fullmatch(line.strip()) or (len(key) >= _KEY_CHARS and key in seen):
            continue
        if not line.strip() and kept and not kept[-1].strip():
            continue
        seen.add(key)
        kept.append(line)
    # A horizontal rule or other line of markup only does not open or close the article.
    while kept and not _text(kept[0]):
        kept.pop(0)
    while kept and not _text(kept[-1]):
        kept.pop()
    return "\n".join(kept)


def is_paywalled(content: str) -> bool:
    """Whether Reader content is the free part of a paywalled article."""
    return bool(_PAYWALL.search(content))


def _text(line: str) -> str:
    return _EMPHASIS.sub("", _MARKUP.sub("", _INVISIBLE.sub("", _NEW_WINDOW.sub("", line)).strip()))


def _is_notice(text: str) -> bool:
    """A cookie or privacy notice addresses the visitor; an article about privacy mostly does not."""
    if _PARTNERS.search(text):
        return True
    if not _VISITOR.search(text):
        return False
    return bool(_COOKIE.search(text)) or len({m.lower() for m in _PRIVACY.findall(text)}) >= 2


def _line_score(line: str, in_code: bool, repeated: bool) -> int:
    stripped = line.strip()
    if not stripped or in_code or stripped.startswith("|"):
        return 0
    text = _text(stripped)
    if not text:
        return 0
    tokens = _WORD.findall(text)
    words = len(tokens)
    label = text.rstrip(":")
    if _SECTION_END.fullmatch(label) or _READ_MORE.fullmatch(label):
        return -_LABEL_COST
    if repeated or _is_notice(text) or (
            words < _LONG_WORDS and (_BOILERPLATE.search(text) or _CALL_TO_ACTION.search(text))):
        return -max(words, _SHORT_COST)
    head = [token.lower() for token in tokens[:_DIVERSITY_TOKENS]]
    if len(head) >= _PROSE_WORDS and len(set(head)) / len(head) < _MIN_DIVERSITY:
        # A row of repeated link labels, such as "Share on Facebook Share on X".
        return -_SHORT_COST
    terminal = bool(_TERMINAL.search(text))
    if words >= _LONG_WORDS or (words >= _SHORT_WORDS and terminal):
        return words
    if words >= _PROSE_WORDS:
        return 0
    return -_SHORT_COST


def _title_line(lines: list[str], title: str | None) -> int | None:
    """Index of the first heading that repeats the title, or a part of it when the title also carries the site name."""
    parts = [words for part in _TITLE_SEPARATOR.split(title or "")
             if len(words := set(_TITLE_WORD.findall(part.lower()))) >= 2]
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            found = set(_TITLE_WORD.findall(line.lower()))
            if any(len(words & found) / len(words) >= 0.8 for words in parts):
                return i
    return None


def _list_blocks(lines: list[str]) -> list[list[int]]:
    """Runs of list items; blank lines and lines of markup only, such as a horizontal rule, do not end a run."""
    blocks, current = [], []
    for i, line in enumerate(lines):
        if not line.strip() or not _text(line):
            continue
        if _LIST_ITEM.match(line):
            current.append(i)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _score_lines(lines: list[str]) -> tuple[list[int], list[bool | None]]:
    """Each line's score, and whether it is filler: text that is not the article. Blank lines are neither (None)."""
    counts: dict[str, int] = {}
    for line in lines:
        key = _text(line).lower()
        if len(key) >= _KEY_CHARS:
            counts[key] = counts.get(key, 0) + 1
    scores, neutral, in_code = [], [], False
    for line in lines:
        fence = line.strip().startswith("```")
        repeated = counts.get(_text(line).lower(), 0) >= _REPEATED
        scores.append(0 if fence else _line_score(line, in_code, repeated))
        neutral.append(fence or in_code or line.strip().startswith("|"))
        if fence:
            in_code = not in_code
    for block in _list_blocks(lines):
        sizes = sorted(len(_WORD.findall(_text(lines[i]))) for i in block)
        statements = len(block) >= 2 and sizes[len(sizes) // 2] >= _STATEMENT_WORDS
        for position, i in enumerate(block):
            if scores[i] == -_SHORT_COST:
                scores[i] = 0 if statements else (-1 if position < _LENIENT_ITEMS else -_SHORT_COST)
            if statements and scores[i] == 0:
                neutral[i] = True
    filler = [None if not line.strip() or not _text(line) else not neutral[i] and scores[i] <= 0
              for i, line in enumerate(lines)]
    return scores, filler


def _article_span(lines: list[str], title: str | None) -> tuple[int, int] | None:
    scores, filler = _score_lines(lines)
    first = _title_line(lines, title)
    # The title heading starts the article only when little prose comes before it.
    if first is not None and sum(s for s in scores[:first] if s > 0) >= _PREFIX_WORDS:
        first = None
    begin = 0 if first is None else first + 1
    best, span, run, start, streak = 0, None, 0, begin, 0
    for i in range(begin, len(lines)):
        if filler[i] is not None:
            streak = streak + 1 if filler[i] else 0
        # After the title heading, the article found so far ends at a section label or a long stretch without text.
        # Without the heading the run is not cut, since a long table can separate an article from its later parts.
        section_end = scores[i] == -_LABEL_COST and _SECTION_END.fullmatch(_text(lines[i]).rstrip(":"))
        if first is not None and best >= _FOUND and (streak >= _MAX_FILLER or section_end):
            break
        if run <= 0:
            run, start = 0, i
        run += scores[i]
        if run > best:
            best, span = run, (start, i)
    if span is None:
        return None
    start, end = span
    # Lines next to the run that cost nothing, such as a standfirst, a table or a list of statements, belong to it.
    taken = 0
    while start - 1 >= begin and scores[start - 1] >= 0 and taken < _EXTEND_FILLER:
        start -= 1
        taken += bool(filler[start])
    taken = 0
    while end + 1 < len(lines) and scores[end + 1] >= 0 and taken < _EXTEND_FILLER:
        end += 1
        taken += bool(filler[end])
    return start, end
