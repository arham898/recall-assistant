CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id            SERIAL PRIMARY KEY,
    campaign_id   TEXT NOT NULL REFERENCES recalls(campaign_id) ON DELETE CASCADE,
    chunker       TEXT NOT NULL,
    field_name    TEXT NOT NULL,
    body          TEXT NOT NULL,
    embedding     vector(384),
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
    UNIQUE (campaign_id, chunker, field_name)
);

CREATE INDEX IF NOT EXISTS idx_chunks_campaign ON chunks (campaign_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chunker  ON chunks (chunker);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv      ON chunks USING GIN (tsv);

-- Partial HNSW indexes: one per chunker so dense queries that filter by
-- chunker search a smaller graph with no wasted candidates.
-- m=16: bi-directional links per node (pgvector default).  Good up to
--   low millions of rows; our dataset is ~30k chunks.
-- ef_construction=64: builder candidate list size (pgvector default).
--   Gives near-perfect recall for our dataset size.
-- vector_cosine_ops: BGE-small produces normalised vectors; cosine is the
--   recommended similarity metric.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_record
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE chunker = 'record';

CREATE INDEX IF NOT EXISTS idx_chunks_embedding_field
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE chunker = 'field';
