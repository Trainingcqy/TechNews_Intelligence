"""Backfill of the news schema from the old tables. Rerunnable: every write skips rows that already exist."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from ingest import store
from ingest.fetch import clean_body
from ingest.reader import _EMPTY_BODY_CHARS, parse_reader_response
from ingest.sources import resolve_publisher
from common.taxonomy import LEGACY_CATEGORY_MAP
from common.urls import url_key

EMBEDDING_MODEL = "jina-embeddings-v3"
HN_SOURCE_KEY = "hackernews_frontpage"
HN_CHANNEL = "hn_frontpage"
_CHANNEL_KEYS = {HN_SOURCE_KEY: HN_CHANNEL, "techcrunch_feed": "techcrunch", "microsoft_ai_blog": "microsoft_blog"}

_CATEGORY_PREFIX_RE = re.compile(r"^\[([^\]]*)\]\s*")
_DIGITS_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class LegacyBody:
    full: bool
    published_at: datetime | None


def channel_key(source_key: str) -> str:
    return _CHANNEL_KEYS.get(source_key, source_key)


def split_title_cn(title_cn: str) -> tuple[str, str | None]:
    """'[AI] 标题' -> ('标题', 'AI')."""
    match = _CATEGORY_PREFIX_RE.match(title_cn)
    if match is None:
        return title_cn, None
    return title_cn[match.end():], match.group(1)


def hn_item_id(source_id: str | None) -> str | None:
    """The old workflow stored '=50497621\\n' from 2026-03 on."""
    match = _DIGITS_RE.search(source_id or "")
    return match.group() if match else None


def legacy_body(raw: str) -> LegacyBody:
    # Measured before cleaning, as fetch does.
    result = parse_reader_response(200, raw)
    return LegacyBody(full=len(result.body) >= _EMPTY_BODY_CHARS, published_at=result.published_at)


def initial_status(*, has_summary: bool, has_embedding: bool, full_body: bool) -> str:
    if has_summary:
        return "embedded" if has_embedding else "analyzed"
    return "fetched" if full_body else "discovered"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _first_per_key(rows: list) -> dict:
    kept: dict = {}
    for row in sorted(rows, key=lambda r: r.id):
        kept.setdefault(url_key(row.url), row)
    return kept


def news_articles(
    rows: list[store.LegacyNews],
    bodies: dict[str, LegacyBody],
    embedded_urls: set[str],
    langs: dict[str, str],
) -> list[tuple[store.MigratedArticle, str | None]]:
    """One article per url_key, from the row with the smallest id. Returns (article, old category)."""
    articles = []
    for key, row in _first_per_key(rows).items():
        body = bodies.get(row.url)
        full = body is not None and body.full
        title_zh, category = split_title_cn(row.title_cn or "")
        if row.source_key == HN_SOURCE_KEY:
            published_at = body.published_at if body else None
        else:
            published_at = _utc(row.created_at)
        article = store.MigratedArticle(
            url=row.url,
            url_key=key,
            lang=langs.get(channel_key(row.source_key)),
            title=row.title,
            title_zh=title_zh or None,
            summary=row.summary,
            topics=LEGACY_CATEGORY_MAP.get(category, ()),
            label_source="mapped",
            first_seen_at=_utc(row.created_at),
            published_at=published_at,
            body_level="full" if full else "none",
            status=initial_status(
                has_summary=bool(row.summary), has_embedding=row.url in embedded_urls, full_body=full
            ),
        )
        articles.append((article, category))
    return articles


def failed_articles(
    rows: list[store.LegacyFailed], bodies: dict[str, LegacyBody], langs: dict[str, str]
) -> list[store.MigratedArticle]:
    articles = []
    for key, row in _first_per_key(rows).items():
        body = bodies.get(row.url)
        full = body is not None and body.full
        articles.append(
            store.MigratedArticle(
                url=row.url,
                url_key=key,
                # The old channels are all English; rows without source_key came from HackerNews or TechCrunch.
                lang=langs.get(channel_key(row.source_key)) if row.source_key else "en",
                title=row.title,
                title_zh=None,
                summary=None,
                topics=(),
                label_source=None,
                first_seen_at=_utc(row.created_at),
                published_at=body.published_at if body else None,
                body_level="full" if full else "none",
                status=initial_status(has_summary=False, has_embedding=False, full_body=full),
            )
        )
    return articles


def _sighting(channel: str, key: str, rows: list[store.LegacyNews]) -> store.MigratedSighting:
    """rows are in id order. On HackerNews the row with the highest score gives external_id, title and score,
    the smallest id on a tie, as section 7.3 keeps the highest-scoring submission; the seen times span all rows."""
    hn = channel == HN_CHANNEL
    best = max(rows, key=lambda r: r.points or 0) if hn else rows[0]
    return store.MigratedSighting(
        url_key=key,
        channel_key=channel,
        external_id=hn_item_id(best.source_id) if best.source_key == HN_SOURCE_KEY else None,
        title=best.title,
        first_seen_at=min(_utc(r.created_at) for r in rows),
        last_seen_at=max(_utc(r.created_at) for r in rows),
        score=best.points if hn else None,
    )


def news_sightings(rows: list[store.LegacyNews]) -> list[store.MigratedSighting]:
    """One sighting per channel and url_key, plus a HackerNews sighting for the other rows with points.
    Rows whose URLs normalize to the same key share an article."""
    own: dict[tuple[str, str], list[store.LegacyNews]] = {}
    extra: dict[str, list[store.LegacyNews]] = {}
    for row in sorted(rows, key=lambda r: r.id):
        key = url_key(row.url)
        own.setdefault((channel_key(row.source_key), key), []).append(row)
        if row.source_key != HN_SOURCE_KEY and (row.points or 0) > 0:
            extra.setdefault(key, []).append(row)
    sightings = [_sighting(channel, key, group) for (channel, key), group in own.items()]
    # Only where no HackerNews row has the URL, so that a HackerNews row keeps its item id.
    sightings += [_sighting(HN_CHANNEL, key, group) for key, group in extra.items() if (HN_CHANNEL, key) not in own]
    return sightings


def _resolve(
    cur, articles: list[store.MigratedArticle], *, dry_run: bool
) -> tuple[list[tuple[store.MigratedArticle, str]], int]:
    """Pairs each article with its publisher name. Returns the pairs and the number of auto publishers created."""
    hosts = store.load_publisher_hosts(cur)
    created = 0
    pairs = []
    for article in articles:
        publisher = resolve_publisher(article.url, hosts)
        # A miss returns a fresh auto publisher whose only entry is not registered.
        if publisher.hosts[0] not in hosts:
            if not dry_run:
                store.ensure_auto_publisher(cur, publisher.name)
            hosts[publisher.hosts[0]] = publisher
            created += 1
        pairs.append((article, publisher.name))
    return pairs, created


def _write_bodies(cur, article_ids: dict[str, int]) -> int:
    """article_ids maps an old URL to its new article."""
    count = 0
    batch: list[tuple[int, str]] = []
    for url, raw in store.legacy_bodies(cur, sorted(article_ids)):
        batch.append((article_ids[url], clean_body(parse_reader_response(200, raw).body)))
        if len(batch) == 200:
            count += store.insert_migrated_bodies(cur, batch)
            batch = []
    if batch:
        count += store.insert_migrated_bodies(cur, batch)
    return count


def migrate(cur, *, dry_run: bool) -> None:
    langs = store.channel_langs(cur)
    news_rows = store.legacy_news(cur)
    failed_rows = store.legacy_failed(cur)
    missing = sorted({channel_key(row.source_key) for row in news_rows} - langs.keys())
    if missing:
        raise RuntimeError(f"channels missing from news.channel, run sync-sources first: {missing}")

    news_keys = {url_key(row.url) for row in news_rows}
    taken = store.existing_url_keys(cur, sorted(news_keys | {url_key(row.url) for row in failed_rows}))
    new_news_rows = [row for row in news_rows if url_key(row.url) not in taken]
    taken |= news_keys
    new_failed_rows = [row for row in failed_rows if url_key(row.url) not in taken]
    bodies = {
        url: legacy_body(raw)
        for url, raw in store.legacy_bodies(cur, sorted({row.url for row in new_news_rows + new_failed_rows}))
    }
    news = news_articles(new_news_rows, bodies, store.legacy_embedded_urls(cur), langs)
    failed = failed_articles(new_failed_rows, bodies, langs)
    existing = store.existing_sightings(cur, sorted(news_keys))
    sightings = [s for s in news_sightings(news_rows) if (s.channel_key, s.url_key) not in existing]
    pairs, auto_publishers = _resolve(cur, [article for article, _ in news] + failed, dry_run=dry_run)

    full = [article for article, _ in pairs if article.body_level == "full"]
    embedded = [article for article, _ in news if article.status == "embedded"]
    if dry_run:
        body_count, embedding_count, sighting_count = len(full), len(embedded), len(sightings)
    else:
        inserted = store.insert_migrated_articles(cur, pairs)
        news = [(a, c) for a, c in news if a.url_key in inserted]
        failed = [a for a in failed if a.url_key in inserted]
        body_count = _write_bodies(cur, {a.url: inserted[a.url_key] for a in full if a.url_key in inserted})
        embedding_count = store.copy_legacy_embeddings(
            cur, [inserted[a.url_key] for a in embedded if a.url_key in inserted], EMBEDDING_MODEL
        )
        sighting_count = store.insert_migrated_sightings(cur, sightings)

    hn_keys = {url_key(row.url) for row in news_rows if row.source_key == HN_SOURCE_KEY}
    extra_hn = sum(1 for s in sightings if s.channel_key == HN_CHANNEL and s.url_key not in hn_keys)
    print("将写入：" if dry_run else "已写入：")
    print(f"  publisher（自动建）\t{auto_publishers}")
    print(f"  article（tech_news）\t{len(news)}")
    for category, count in sorted(Counter(c or "无" for _, c in news).items()):
        print(f"    旧分类 {category}\t{count}")
    for status, count in sorted(Counter(a.status for a, _ in news).items()):
        print(f"    {status}\t{count}")
    print(f"  article（tech_news_failed）\t{len(failed)}")
    for status, count in sorted(Counter(a.status for a in failed).items()):
        print(f"    {status}\t{count}")
    print(f"  sighting\t{sighting_count}")
    print(f"    非 HackerNews 行另建的 HackerNews 发现记录\t{extra_hn}")
    print(f"  article_body\t{body_count}")
    print(f"  article_embedding\t{embedding_count}")
