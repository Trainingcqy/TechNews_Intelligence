"""URL normalization shared by the collection pipeline and the agent."""

from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit

# Stored article URLs are all ASCII, so a non-ASCII character such as Chinese text
# or full-width punctuation right after a link ends it.
_URL_RE = re.compile(r"https?://[^\s<>\])\"'\u0080-\U0010ffff]+")
_TRAILING_PUNCT_RE = re.compile(r"[)\]\}）】》\.,。，;；:：!！?？、]+$")
# f=rss is appended to every link in the 36Kr and Huxiu feeds.
_TRACKING_PARAMS = frozenset({"f", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src"})
_DEFAULT_PORT_SUFFIX = {"http": ":80", "https": ":443"}


def _parse(url: str) -> tuple[str, SplitResult | None]:
    cleaned = _TRAILING_PUNCT_RE.sub("", url.strip())
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned, None
    return cleaned, parts if parts.hostname else None


def url_key(url: str) -> str:
    cleaned, parts = _parse(url)
    if parts is None:
        return cleaned.lower()
    host = parts.netloc.rpartition("@")[2].lower().removeprefix("www.")
    host = host.removesuffix(_DEFAULT_PORT_SUFFIX.get(parts.scheme, ""))
    params = []
    for param in parts.query.split("&"):
        name = param.partition("=")[0]
        if param and not name.startswith("utm_") and name not in _TRACKING_PARAMS:
            params.append((name, param))
    params.sort(key=lambda item: item[0])
    key = host + parts.path.rstrip("/")
    if params:
        key += "?" + "&".join(param for _, param in params)
    return key


def host_of(url: str) -> str:
    parts = _parse(url)[1]
    return parts.hostname.removeprefix("www.") if parts else ""


def extract_urls(text: str) -> list[str]:
    urls = (_TRAILING_PUNCT_RE.sub("", url) for url in _URL_RE.findall(text or ""))
    return list(dict.fromkeys(urls))
