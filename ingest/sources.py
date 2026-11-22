"""Publisher and channel definitions from sources.toml, and publisher resolution."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from common.urls import host_of

SOURCES_PATH = Path(__file__).with_name("sources.toml")


@dataclass(frozen=True)
class Publisher:
    name: str
    kind: str
    hosts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    fetch_kind: str
    endpoint: str
    publisher: str | None
    lang: str
    purpose: str
    enabled: bool
    config: dict


@dataclass(frozen=True)
class Sources:
    publishers: tuple[Publisher, ...]
    channels: tuple[Channel, ...]
    hosts: dict[tuple[str, str], Publisher]


def split_host(entry: str) -> tuple[str, str]:
    host, _, prefix = entry.partition("/")
    return host.lower(), prefix.strip("/").lower()


def parse_sources(text: str) -> Sources:
    data = tomllib.loads(text)
    publishers = tuple(
        Publisher(
            name=raw["name"],
            kind=raw["kind"],
            hosts=tuple(split_host(entry) for entry in raw["hosts"]),
        )
        for raw in data.get("publisher", [])
    )
    hosts: dict[tuple[str, str], Publisher] = {}
    for publisher in publishers:
        for entry in publisher.hosts:
            if entry in hosts:
                raise ValueError(f"duplicate host entry {entry} in {publisher.name}")
            hosts[entry] = publisher

    names = {publisher.name for publisher in publishers}
    channels = []
    keys: set[str] = set()
    for raw in data.get("channel", []):
        channel = Channel(
            key=raw["key"],
            name=raw["name"],
            fetch_kind=raw["fetch_kind"],
            endpoint=raw["endpoint"],
            publisher=raw.get("publisher"),
            lang=raw["lang"],
            purpose=raw.get("purpose", "corpus"),
            enabled=raw.get("enabled", True),
            config=raw.get("config", {}),
        )
        if channel.key in keys:
            raise ValueError(f"duplicate channel key {channel.key}")
        if channel.publisher is not None and channel.publisher not in names:
            raise ValueError(f"channel {channel.key} references unknown publisher {channel.publisher}")
        keys.add(channel.key)
        channels.append(channel)
    return Sources(publishers=publishers, channels=tuple(channels), hosts=hosts)


def load_sources(path: Path = SOURCES_PATH) -> Sources:
    return parse_sources(path.read_text(encoding="utf-8"))


def resolve_publisher(url: str, hosts: Mapping[tuple[str, str], Publisher]) -> Publisher:
    host = host_of(url)
    if not host:
        raise ValueError(f"no host in {url!r}")
    segments = urlsplit(url.strip()).path.strip("/").lower().split("/")
    labels = host.split(".")
    for start in range(max(len(labels) - 1, 1)):
        candidate = ".".join(labels[start:])
        best: tuple[int, Publisher] | None = None
        for (entry_host, prefix), publisher in hosts.items():
            if entry_host != candidate:
                continue
            prefix_segments = prefix.split("/") if prefix else []
            if segments[: len(prefix_segments)] != prefix_segments:
                continue
            if best is None or len(prefix_segments) > best[0]:
                best = (len(prefix_segments), publisher)
        if best is not None:
            return best[1]
    return Publisher(name=host, kind="unknown", hosts=((host, ""),))
