"""Tables of what a work is and where it lives."""

WORKS_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS works (
    id SERIAL PRIMARY KEY,

    content_key TEXT NOT NULL UNIQUE,

    title TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


SQL = """
CREATE TABLE IF NOT EXISTS collections (
    name       TEXT PRIMARY KEY,
    root       TEXT NOT NULL,
    searchable BOOLEAN NOT NULL DEFAULT TRUE,
    ordinal    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_collections_order
    ON collections (ordinal, name);
CREATE TABLE IF NOT EXISTS work_paths (
    id         BIGSERIAL PRIMARY KEY,
    work_id    INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    collection TEXT NOT NULL
               CONSTRAINT work_paths_collection_fkey
               REFERENCES collections(name)
               ON UPDATE CASCADE ON DELETE RESTRICT,
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    present    BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (work_id, path)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_paths_live
    ON work_paths (path) WHERE present;
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_paths_one_live
    ON work_paths (work_id) WHERE present;
CREATE INDEX IF NOT EXISTS idx_work_paths_work ON work_paths (work_id);
CREATE INDEX IF NOT EXISTS idx_work_paths_path ON work_paths (path);

CREATE TABLE IF NOT EXISTS work_copies (
    id         BIGSERIAL PRIMARY KEY,
    work_id    INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    path       TEXT NOT NULL UNIQUE,
    collection TEXT NOT NULL
               CONSTRAINT work_copies_collection_fkey
               REFERENCES collections(name)
               ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_copies_work ON work_copies (work_id);
"""
