-- [DDL] PostgreSQL Table

-- 0. Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. Access_Tokens
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

-- 2. Conversation Threads (persistent chat sessions)
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

-- 3. Conversation Messages (history entries per thread)
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

-- 4. User memory: follows
CREATE TABLE IF NOT EXISTS public.user_follow (
    follow_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_key        TEXT NOT NULL,
    query           TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    search_query    TEXT
);

-- CREATE TABLE IF NOT EXISTS leaves an existing table as it is, so the column is added here.
ALTER TABLE public.user_follow ADD COLUMN IF NOT EXISTS search_query TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_follow_query
    ON public.user_follow (user_key, LOWER(query));

-- 5. Agent request traces
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
        'running', 'answered', 'insufficient', 'direct', 'not_found', 'blocked', 'error', 'stopped'
    ))
);

-- CREATE TABLE IF NOT EXISTS leaves an existing table as it is, so the status check is (re)created here.
ALTER TABLE public.agent_runs DROP CONSTRAINT IF EXISTS chk_agent_runs_status;
ALTER TABLE public.agent_runs ADD CONSTRAINT chk_agent_runs_status CHECK (status IN (
    'running', 'answered', 'insufficient', 'direct', 'not_found', 'blocked', 'error', 'stopped'
));

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

-- 6. Performance Indexes
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

-- 7. Update Trigger Function
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_conversation_threads_modtime ON public.conversation_threads;
CREATE TRIGGER update_conversation_threads_modtime
    BEFORE UPDATE ON public.conversation_threads
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();
