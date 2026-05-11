-- depends:

CREATE EXTENSION IF NOT EXISTS vector;

-- Practices: one row per provider practice (from NPI data)
CREATE TABLE IF NOT EXISTS practices (
    id SERIAL PRIMARY KEY,
    npi_number VARCHAR(10) UNIQUE NOT NULL,

    -- Practice identity
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    practice_type VARCHAR(20) NOT NULL DEFAULT 'healthcare',

    -- Address
    address_1 TEXT NOT NULL DEFAULT '',
    address_city VARCHAR(100) NOT NULL DEFAULT '',
    address_state VARCHAR(2) NOT NULL DEFAULT '',
    address_zip VARCHAR(10) NOT NULL DEFAULT '',

    -- Structured metadata (filterable)
    services TEXT[] NOT NULL DEFAULT '{}',
    specialties TEXT[] NOT NULL DEFAULT '{}',
    presence_types TEXT[] NOT NULL DEFAULT '{}',

    -- Professional roster
    professionals JSONB NOT NULL DEFAULT '[]',

    -- Embedding
    embedding vector(768),
    embedding_model VARCHAR(100),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for vector similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS practices_embedding_idx
    ON practices USING hnsw (embedding vector_cosine_ops);

-- Filter indexes
CREATE INDEX IF NOT EXISTS practices_zip_idx ON practices (address_zip);
CREATE INDEX IF NOT EXISTS practices_services_idx ON practices USING gin (services);
CREATE INDEX IF NOT EXISTS practices_specialties_idx ON practices USING gin (specialties);


-- Resource chunks: one row per chunk of a curated resource page
CREATE TABLE IF NOT EXISTS resource_chunks (
    id SERIAL PRIMARY KEY,
    content_hash VARCHAR(64) UNIQUE NOT NULL,

    -- Source metadata
    source_url TEXT NOT NULL,
    org_name TEXT NOT NULL DEFAULT '',
    page_title TEXT NOT NULL DEFAULT '',
    county_scope VARCHAR(50) NOT NULL DEFAULT 'national',
    fetch_date DATE,

    -- Content
    content TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    section_header TEXT NOT NULL DEFAULT '',

    -- Embedding
    embedding vector(768),
    embedding_model VARCHAR(100),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for vector similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS resource_chunks_embedding_idx
    ON resource_chunks USING hnsw (embedding vector_cosine_ops);

-- Lookup indexes
CREATE INDEX IF NOT EXISTS resource_chunks_source_url_idx ON resource_chunks (source_url);
CREATE INDEX IF NOT EXISTS resource_chunks_county_scope_idx ON resource_chunks (county_scope);
