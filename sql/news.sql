CREATE SCHEMA IF NOT EXISTS news;

-- 1. Publisher: who wrote the article
CREATE TABLE IF NOT EXISTS news.publisher (
    publisher_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL DEFAULT 'unknown'
        CHECK (kind IN ('official', 'media', 'community', 'personal',
                        'code_host', 'paper_host', 'social', 'unknown')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS news.publisher_host (
    host         TEXT NOT NULL,
    path_prefix  TEXT NOT NULL DEFAULT '',
    publisher_id BIGINT NOT NULL REFERENCES news.publisher (publisher_id),
    PRIMARY KEY (host, path_prefix)
);

-- 2. Channel: an endpoint the pipeline polls
CREATE TABLE IF NOT EXISTS news.channel (
    channel_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key            TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    fetch_kind     TEXT NOT NULL CHECK (fetch_kind IN ('feed', 'sitemap', 'hn')),
    endpoint       TEXT NOT NULL,
    purpose        TEXT NOT NULL DEFAULT 'corpus' CHECK (purpose IN ('corpus', 'reference')),
    publisher_id   BIGINT REFERENCES news.publisher (publisher_id),
    lang           TEXT NOT NULL DEFAULT 'en',
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    poll_minutes   INTEGER NOT NULL DEFAULT 60,
    config         JSONB NOT NULL DEFAULT '{}'::jsonb,
    etag           TEXT,
    last_modified  TEXT,
    last_polled_at TIMESTAMPTZ,
    last_ok_at     TIMESTAMPTZ,
    fail_streak    INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. Article: one row per canonical URL, from discovery onwards
CREATE TABLE IF NOT EXISTS news.article (
    article_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url              TEXT NOT NULL,
    url_key          TEXT NOT NULL UNIQUE,
    publisher_id     BIGINT NOT NULL REFERENCES news.publisher (publisher_id),
    lang             TEXT,
    title            TEXT NOT NULL,
    published_at     TIMESTAMPTZ,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_at         TIMESTAMPTZ GENERATED ALWAYS AS (COALESCE(published_at, first_seen_at)) STORED,

    -- analysis output
    title_zh         TEXT,
    summary          TEXT,
    topics           TEXT[] NOT NULL DEFAULT '{}',
    kind             TEXT,
    taxonomy_version SMALLINT,
    label_source     TEXT CHECK (label_source IN ('model', 'mapped')),
    body_level       TEXT NOT NULL DEFAULT 'none' CHECK (body_level IN ('full', 'excerpt', 'none')),

    -- pipeline state; excluded is promotional content, kept only so that its URL is not inserted again
    status           TEXT NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered', 'fetched', 'analyzed', 'embedded', 'ready', 'failed', 'excluded')),
    attempts         SMALLINT NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_kind       TEXT,

    search_tsv       tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', COALESCE(title, '')), 'A')
        || setweight(to_tsvector('simple', COALESCE(title_zh, '')), 'A')
        || setweight(to_tsvector('english', COALESCE(summary, '')), 'B')
        || setweight(to_tsvector('simple', COALESCE(summary, '')), 'B')
    ) STORED,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_article_ready_event ON news.article (event_at DESC) WHERE status = 'ready';
CREATE INDEX IF NOT EXISTS idx_article_queue ON news.article (status, next_attempt_at)
    WHERE status NOT IN ('ready', 'failed');
CREATE INDEX IF NOT EXISTS idx_article_publisher ON news.article (publisher_id);
CREATE INDEX IF NOT EXISTS idx_article_topics ON news.article USING GIN (topics);
CREATE INDEX IF NOT EXISTS idx_article_search_tsv ON news.article USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS idx_article_title_trgm ON news.article USING GIN (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_article_title_zh_trgm ON news.article USING GIN (title_zh gin_trgm_ops);

-- CREATE TABLE IF NOT EXISTS leaves an existing table as it is, so checks added later are (re)created here.
-- A ready article must carry its labels; rows labelled by the legacy mapping are exempt until they are rebuilt.
ALTER TABLE news.article DROP CONSTRAINT IF EXISTS article_status_check;
ALTER TABLE news.article ADD CONSTRAINT article_status_check
    CHECK (status IN ('discovered', 'fetched', 'analyzed', 'embedded', 'ready', 'failed', 'excluded'));
ALTER TABLE news.article DROP CONSTRAINT IF EXISTS chk_article_ready_labels;
ALTER TABLE news.article ADD CONSTRAINT chk_article_ready_labels
    CHECK (status <> 'ready' OR label_source = 'mapped' OR (kind IS NOT NULL AND cardinality(topics) > 0));

-- 4. Body text
CREATE TABLE IF NOT EXISTS news.article_body (
    article_id BIGINT PRIMARY KEY REFERENCES news.article (article_id) ON DELETE CASCADE,
    source     TEXT NOT NULL CHECK (source IN ('feed', 'reader', 'hn')),
    text       TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. Summary embedding
CREATE TABLE IF NOT EXISTS news.article_embedding (
    article_id BIGINT PRIMARY KEY REFERENCES news.article (article_id) ON DELETE CASCADE,
    embedding  vector(1024) NOT NULL,
    model      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. Sighting: an article seen in a channel
CREATE TABLE IF NOT EXISTS news.sighting (
    sighting_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id    BIGINT NOT NULL REFERENCES news.article (article_id) ON DELETE CASCADE,
    channel_id    BIGINT NOT NULL REFERENCES news.channel (channel_id),
    external_id   TEXT,
    title         TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    score         INTEGER,
    comment_count INTEGER,
    CONSTRAINT uq_sighting_channel_article UNIQUE (channel_id, article_id)
);

CREATE INDEX IF NOT EXISTS idx_sighting_article ON news.sighting (article_id);

-- 7. Discussion text attached to a sighting (HackerNews)
CREATE TABLE IF NOT EXISTS news.discussion (
    sighting_id   BIGINT PRIMARY KEY REFERENCES news.sighting (sighting_id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    comment_total INTEGER NOT NULL,
    text          TEXT NOT NULL,
    char_count    INTEGER NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 8. Story membership (derived; rebuildable)
CREATE SEQUENCE IF NOT EXISTS news.story_id_seq;

CREATE TABLE IF NOT EXISTS news.story_member (
    article_id         BIGINT PRIMARY KEY REFERENCES news.article (article_id) ON DELETE CASCADE,
    story_id           BIGINT NOT NULL,
    nearest_article_id BIGINT,
    nearest_similarity REAL,
    assigned_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_member_story ON news.story_member (story_id);

-- 9. Operational records
CREATE TABLE IF NOT EXISTS news.channel_poll (
    poll_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id  BIGINT NOT NULL REFERENCES news.channel (channel_id),
    polled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN NOT NULL,
    http_status INTEGER,
    items_seen  INTEGER NOT NULL DEFAULT 0,
    items_new   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_channel_poll_channel ON news.channel_poll (channel_id, polled_at DESC);

CREATE TABLE IF NOT EXISTS news.stage_failure (
    failure_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES news.article (article_id) ON DELETE CASCADE,
    stage      TEXT NOT NULL,
    error_kind TEXT NOT NULL,
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stage_failure_created ON news.stage_failure (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stage_failure_article ON news.stage_failure (article_id);

-- 10. Reference headlines (acceptance only; never joined into the corpus)
CREATE TABLE IF NOT EXISTS news.reference_headline (
    headline_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id     BIGINT NOT NULL REFERENCES news.channel (channel_id),
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    target_url     TEXT,
    target_url_key TEXT,
    outlet         TEXT,
    published_at   TIMESTAMPTZ,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_reference_headline UNIQUE (channel_id, external_id)
);
