-- [DDL] PostgreSQL Table

-- 0. Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. Entity Registry and Alias Tables
CREATE TABLE IF NOT EXISTS public.entity_registry (
    entity_id BIGSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    entity_type VARCHAR(50) NOT NULL DEFAULT 'unknown',
    wikidata_id VARCHAR(64),
    source VARCHAR(50) NOT NULL DEFAULT 'manual',
    confidence NUMERIC(4, 3) NOT NULL DEFAULT 1.000,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT entity_registry_canonical_unique UNIQUE (canonical_name)
);

CREATE TABLE IF NOT EXISTS public.entity_alias (
    alias_id BIGSERIAL PRIMARY KEY,
    entity_id BIGINT NOT NULL
        REFERENCES public.entity_registry(entity_id)
        ON DELETE CASCADE,
    alias TEXT NOT NULL,
    language VARCHAR(20) NOT NULL DEFAULT 'unknown',
    alias_type VARCHAR(50) NOT NULL DEFAULT 'manual',
    weight NUMERIC(6, 3) NOT NULL DEFAULT 1.000,
    is_exact BOOLEAN NOT NULL DEFAULT TRUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.entity_alias_candidate (
    candidate_id BIGSERIAL PRIMARY KEY,
    alias TEXT NOT NULL,
    suggested_entity_id BIGINT
        REFERENCES public.entity_registry(entity_id)
        ON DELETE SET NULL,
    canonical_name TEXT,
    entity_type VARCHAR(50) NOT NULL DEFAULT 'unknown',
    source VARCHAR(50) NOT NULL DEFAULT 'corpus',
    confidence NUMERIC(4, 3) NOT NULL DEFAULT 0.000,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    evidence_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    reason TEXT,
    aliases_to_add JSONB NOT NULL DEFAULT '[]'::jsonb,
    reviewed_at TIMESTAMPTZ,
    promoted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT entity_alias_candidate_status_chk
        CHECK (status IN ('pending', 'auto_approved', 'approved', 'rejected'))
);

-- 2. Access_Tokens
CREATE TABLE IF NOT EXISTS access_tokens (
    id          SERIAL       PRIMARY KEY,
    email       VARCHAR(255) NOT NULL,
    token       VARCHAR(64)  NOT NULL UNIQUE,
    quota       INT          NOT NULL DEFAULT 10,
    used        INT          NOT NULL DEFAULT 0,
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',   -- active / pending / capped
    notified    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    upgraded_at TIMESTAMPTZ,
    tier        SMALLINT     NOT NULL DEFAULT 0,
    unlimited   BOOLEAN      NOT NULL DEFAULT FALSE
);

-- 3. Conversation Threads (persistent chat sessions)
CREATE TABLE IF NOT EXISTS public.conversation_threads (
    id BIGSERIAL PRIMARY KEY,
    thread_id VARCHAR(64) NOT NULL UNIQUE,
    channel VARCHAR(32) NOT NULL DEFAULT 'generic',
    subject TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ
);

-- 4. Conversation Messages (history entries per thread)
CREATE TABLE IF NOT EXISTS public.conversation_messages (
    id BIGSERIAL PRIMARY KEY,
    thread_id VARCHAR(64) NOT NULL,
    role VARCHAR(20) NOT NULL,
    parts JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_conversation_messages_thread
        FOREIGN KEY (thread_id)
        REFERENCES public.conversation_threads(thread_id)
        ON DELETE CASCADE
);

-- 5. User memory: follows and seen stories
CREATE TABLE IF NOT EXISTS public.user_follow (
    follow_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_key        TEXT NOT NULL,
    query           TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_follow_query
    ON public.user_follow (user_key, LOWER(query));

CREATE TABLE IF NOT EXISTS public.user_story_seen (
    user_key      TEXT NOT NULL,
    story_id      BIGINT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_key, story_id)
);

-- 6. Agent request traces
CREATE TABLE IF NOT EXISTS public.agent_runs (
    request_id VARCHAR(64) PRIMARY KEY,
    thread_id VARCHAR(128),
    channel VARCHAR(16) NOT NULL,
    user_message TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    error_code VARCHAR(128),
    error_message TEXT,
    exception TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    latency_ms INTEGER,
    token_usage JSONB,
    warnings TEXT[] NOT NULL DEFAULT '{}',
    output JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chk_agent_runs_channel CHECK (channel IN ('web', 'telegram', 'cli', 'eval')),
    CONSTRAINT chk_agent_runs_status CHECK (status IN (
        'running', 'answered', 'insufficient', 'direct', 'not_found', 'blocked', 'error'
    ))
);

CREATE TABLE IF NOT EXISTS public.agent_trace_spans (
    request_id VARCHAR(64) NOT NULL REFERENCES public.agent_runs(request_id) ON DELETE CASCADE,
    span_id VARCHAR(32) NOT NULL,
    parent_span_id VARCHAR(32),
    kind VARCHAR(16) NOT NULL,
    name VARCHAR(64),
    status VARCHAR(16) NOT NULL,
    started_at_ms BIGINT NOT NULL,
    finished_at_ms BIGINT,
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    output JSONB NOT NULL DEFAULT '{}'::jsonb,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    warnings TEXT[] NOT NULL DEFAULT '{}',
    error_code VARCHAR(128),
    error_message TEXT,
    exception TEXT,
    PRIMARY KEY (request_id, span_id),
    CONSTRAINT chk_agent_trace_spans_kind
        CHECK (kind IN ('context', 'step', 'llm', 'tool', 'embedding', 'retrieval', 'rerank')),
    CONSTRAINT chk_agent_trace_spans_status CHECK (status IN ('ok', 'error', 'aborted'))
);

CREATE TABLE IF NOT EXISTS public.agent_model_io (
    request_id VARCHAR(64) NOT NULL,
    span_id VARCHAR(32) NOT NULL,
    input_messages JSONB NOT NULL,
    output JSONB,
    PRIMARY KEY (request_id, span_id),
    FOREIGN KEY (request_id, span_id) REFERENCES public.agent_trace_spans(request_id, span_id) ON DELETE CASCADE
);

-- 7. Performance Indexes
CREATE INDEX IF NOT EXISTS idx_access_tokens_email ON access_tokens (email);
CREATE INDEX IF NOT EXISTS idx_conversation_threads_recent
    ON public.conversation_threads (COALESCE(last_message_at, created_at) DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_threads_channel_recent
    ON public.conversation_threads (channel, COALESCE(last_message_at, created_at) DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_thread_order
    ON public.conversation_messages (thread_id, id);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_thread_created
    ON public.conversation_messages (thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON public.agent_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_thread ON public.agent_runs(thread_id, started_at);
CREATE INDEX IF NOT EXISTS idx_entity_registry_active_type
    ON public.entity_registry(is_active, entity_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_alias_entity_lower_alias
    ON public.entity_alias(entity_id, LOWER(alias));
CREATE INDEX IF NOT EXISTS idx_entity_alias_lower_alias
    ON public.entity_alias(LOWER(alias));
CREATE INDEX IF NOT EXISTS idx_entity_alias_trgm
    ON public.entity_alias USING GIN (alias gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entity_alias_candidate_status
    ON public.entity_alias_candidate(status, confidence DESC, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_alias_candidate_source_lower_alias
    ON public.entity_alias_candidate(source, LOWER(alias));

-- 8. Update Trigger Function
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_entity_registry_modtime ON public.entity_registry;
CREATE TRIGGER update_entity_registry_modtime
    BEFORE UPDATE ON public.entity_registry
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

DROP TRIGGER IF EXISTS update_entity_alias_modtime ON public.entity_alias;
CREATE TRIGGER update_entity_alias_modtime
    BEFORE UPDATE ON public.entity_alias
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

DROP TRIGGER IF EXISTS update_entity_alias_candidate_modtime ON public.entity_alias_candidate;
CREATE TRIGGER update_entity_alias_candidate_modtime
    BEFORE UPDATE ON public.entity_alias_candidate
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

DROP TRIGGER IF EXISTS update_conversation_threads_modtime ON public.conversation_threads;
CREATE TRIGGER update_conversation_threads_modtime
    BEFORE UPDATE ON public.conversation_threads
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();
