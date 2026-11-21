"""Tests for Techmeme parsing, using a recorded feed (trimmed to 3 items)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ingest.discover.reference import parse_reference_feed, parse_techmeme_item
from ingest.models import ReferenceHeadline

CONTENT = (Path(__file__).resolve().parents[1] / "fixtures" / "ingest" / "reference_techmeme.xml").read_bytes()
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def test_headline_fields():
    first = parse_reference_feed(CONTENT, now=NOW)[0]
    assert first == ReferenceHeadline(
        external_id="https://www.techmeme.com/261118/p12#a261118p12",
        # the <B> headline text: no "(author/outlet)" suffix, Techmeme's own quotes
        title=(
            "An interview with Rockstar North's Rob Nelson on the \u201crisk\u201d of a romantic story, "
            "switching from GTA V's split storyline to co-dependent partnership, and more"
        ),
        target_url="https://www.theverge.com/entertainment/1035806/grand-theft-auto-6-rob-nelson-interview",
        outlet="The Verge",
        # pubDate is "Wed, 18 Nov 2026 06:25:00 -0500"
        published_at=datetime(2026, 11, 18, 11, 25, tzinfo=UTC),
    )


def test_target_and_outlet_per_item_layout():
    # with an author, without an author, and without a picture (the first HREF is then the Techmeme permalink)
    got = [(h.target_url, h.outlet) for h in parse_reference_feed(CONTENT, now=NOW)]
    assert got == [
        ("https://www.theverge.com/entertainment/1035806/grand-theft-auto-6-rob-nelson-interview", "The Verge"),
        ("https://www.ign.com/articles/how-long-is-gta-6", "IGN"),
        ("https://www.bloomberg.com/news/articles/2026-11-18/goldman-backs-fintech-startup-payhawk-at-2-billion-value", "Bloomberg"),
    ]


def test_description_without_headline_link():
    assert parse_techmeme_item("<P>no links here</P>") == (None, None, None)
