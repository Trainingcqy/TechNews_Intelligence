"""Model instructions from config/prompts/<file>.toml. A lowercase name in braces is a placeholder the code fills."""

from __future__ import annotations

import re
import tomllib
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "config" / "prompts"
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


@cache
def _load(file: str) -> dict:
    with (PROMPTS_DIR / f"{file}.toml").open("rb") as handle:
        return tomllib.load(handle)


def text(key: str) -> str:
    """The text at a dotted key that starts with the file name, e.g. "tools.search_news.description"."""
    file, *path = key.split(".")
    value = _load(file)
    for part in path:
        value = value[part]
    if not isinstance(value, str):
        raise TypeError(f"prompt {key} is not a string")
    return value


def render(key: str, **values: str) -> str:
    """Fill every placeholder in one pass, so that braces inside a value are never substituted."""
    template = text(key)
    names = set(_PLACEHOLDER_RE.findall(template))
    if names != values.keys():
        raise ValueError(f"prompt {key} takes {sorted(names)}, got {sorted(values)}")
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)
