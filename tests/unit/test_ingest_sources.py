"""Tests for sources.toml loading and validation."""

from __future__ import annotations

import pytest

from ingest.sources import load_sources, parse_sources, split_host


def test_repository_sources_toml_is_valid() -> None:
    sources = load_sources()
    channels = {channel.key: channel for channel in sources.channels}
    names = {publisher.name for publisher in sources.publishers}

    assert len(channels) == 30
    assert all(channel.publisher in names for channel in sources.channels if channel.publisher)
    assert {channel.lang for channel in sources.channels} == {"en"}
    assert {channel.fetch_kind for channel in sources.channels} == {"feed", "sitemap"}
    assert channels["techmeme"].purpose == "reference"
    assert channels["anthropic_news"].config == {"url_pattern": "/news/"}
    assert {key for key, channel in channels.items() if not channel.enabled} == {
        "google_ai_blog",
        "aws_ml_blog",
        "nvidia_dev_blog",
    }
    assert sources.hosts[("huggingface.co", "qwen")].name == "Alibaba"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ('{ exclude_url = "(unclosed" }', "invalid exclude_url"),
        ('{ feed_timezone = "Pacific/Nowhere" }', "invalid feed_timezone"),
    ],
)
def test_invalid_channel_config_is_rejected(config: str, message: str) -> None:
    text = (
        '[[channel]]\nkey = "x"\nname = "X"\nfetch_kind = "feed"\nendpoint = "https://x.example/feed"\n'
        f'lang = "en"\nconfig = {config}\n'
    )
    with pytest.raises(ValueError, match=message):
        parse_sources(text)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("techcrunch.com", ("techcrunch.com", "")),
        ("huggingface.co/Qwen", ("huggingface.co", "qwen")),
        ("GitHub.com/a/B/", ("github.com", "a/b")),
    ],
)
def test_split_host(entry: str, expected: tuple[str, str]) -> None:
    assert split_host(entry) == expected


_PUBLISHER = '[[publisher]]\nname = "P"\nkind = "media"\nhosts = ["p.com"]\n'


def _channel(key: str, publisher: str = "P") -> str:
    return (
        f'[[channel]]\nkey = "{key}"\nname = "n"\nfetch_kind = "feed"\n'
        f'endpoint = "https://p.com/feed"\npublisher = "{publisher}"\nlang = "en"\n'
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_PUBLISHER + _channel("a") + _channel("a"), "duplicate channel key"),
        (_PUBLISHER + _channel("a", publisher="Q"), "unknown publisher"),
        (
            _PUBLISHER + '[[publisher]]\nname = "Q"\nkind = "media"\nhosts = ["P.com/"]\n',
            "duplicate host entry",
        ),
        ('[[publisher]]\nname = "P"\nkind = "media"\nhosts = ["p.com/x", "p.com/X/"]\n', "duplicate host entry"),
    ],
)
def test_parse_sources_rejects_invalid_definitions(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_sources(text)


def test_parse_sources_accepts_same_host_with_different_prefixes() -> None:
    sources = parse_sources(
        _PUBLISHER + '[[publisher]]\nname = "Q"\nkind = "media"\nhosts = ["p.com/q"]\n' + _channel("a")
    )
    assert sources.hosts[("p.com", "q")].name == "Q"
    assert sources.channels[0].publisher == "P"
