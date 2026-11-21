"""HTTP GET with a timeout and conditional-request validators, and HTTP POST for API calls."""

from __future__ import annotations

from dataclasses import dataclass

import requests
from urllib3.exceptions import ReadTimeoutError

# Every collection request sends a browser UA: huxiu's feed does not return 200 to a non-browser one.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    etag: str | None
    last_modified: str | None
    content: bytes


class TransportError(Exception):
    """The request got no complete response. kind is "timeout" or "network"."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def get(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    etag: str | None = None,
    last_modified: str | None = None,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    request_headers = {"User-Agent": user_agent, **(headers or {})}
    if etag:
        request_headers["If-None-Match"] = etag
    if last_modified:
        request_headers["If-Modified-Since"] = last_modified
    return _request("GET", url, headers=request_headers, timeout=timeout)


def post(url: str, *, json: object, headers: dict[str, str], timeout: float) -> HttpResponse:
    return _request("POST", url, json=json, headers=headers, timeout=timeout)


def _request(method: str, url: str, **kwargs) -> HttpResponse:
    try:
        response = requests.request(method, url, **kwargs)
    except requests.Timeout as exc:
        raise TransportError("timeout", str(exc)) from exc
    except requests.ConnectionError as exc:
        # requests reports a read timeout during the body download as ConnectionError.
        kind = "timeout" if exc.args and isinstance(exc.args[0], ReadTimeoutError) else "network"
        raise TransportError(kind, str(exc)) from exc
    except requests.exceptions.ChunkedEncodingError as exc:
        raise TransportError("network", str(exc)) from exc
    return HttpResponse(
        status=response.status_code,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
        content=response.content,
    )
