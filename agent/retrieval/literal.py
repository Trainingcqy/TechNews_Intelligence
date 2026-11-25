"""Literal matching of a query in text columns, shared by the exact leg of hybrid retrieval and story browsing."""

from __future__ import annotations

import re

_REGEX_SPECIAL = re.compile(r"[\\.^$|?*+()\[\]{}]")


def exact_pattern(query: str) -> str | None:
    """The regex for an ASCII query, which must not touch an ASCII letter or digit on either side; None for any
    other query, which is matched as a substring."""
    # Postgres regexes treat CJK characters as word characters, so \m and \M would miss an English name
    # written directly against Chinese text.
    if not query.isascii():
        return None
    pattern = _REGEX_SPECIAL.sub(lambda m: "\\" + m.group(0), query)
    if query[0].isalnum():
        pattern = "(?<![a-z0-9])" + pattern
    if query[-1].isalnum():
        pattern += "(?![a-z0-9])"
    return pattern


def literal_match(column: str, pattern: str | None, raw_query: str) -> str:
    """SQL matching the query in column: the parameter exact_pattern when there is a pattern, otherwise the SQL
    expression raw_query as a substring."""
    if pattern is None:
        return f"{column} ILIKE ('%%' || {raw_query} || '%%')"
    return f"{column} ~* %(exact_pattern)s"
