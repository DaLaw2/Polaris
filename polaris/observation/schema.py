"""Tables of what a scan saw, and of what a check compared."""

SQL = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    params      JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS scan_errors (
    id      BIGSERIAL PRIMARY KEY,
    run_id  BIGINT  NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    work_id INTEGER REFERENCES works(id) ON DELETE SET NULL,
    path    TEXT NOT NULL,
    stage   TEXT NOT NULL,
    message TEXT NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scan_errors_run ON scan_errors (run_id);
CREATE INDEX IF NOT EXISTS idx_scan_errors_work ON scan_errors (work_id);
CREATE TABLE IF NOT EXISTS work_samples (
    id             BIGSERIAL PRIMARY KEY,
    run_id         BIGINT  NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    work_id        INTEGER NOT NULL REFERENCES works(id)     ON DELETE CASCADE,
    medium         TEXT    NOT NULL CHECK (medium IN ('image', 'video')),
    source_relpath TEXT    NOT NULL,
    position       NUMERIC NOT NULL,
    ordinal        INTEGER NOT NULL,
    skip_reason    TEXT CONSTRAINT work_samples_skip_reason_check
                   CHECK (skip_reason IN ('blank', 'too_small', 'decode_error',
                                          'duplicate_scene')),
    analyzed       BOOLEAN GENERATED ALWAYS AS (skip_reason IS NULL) STORED,
    UNIQUE (run_id, work_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_samples_work ON work_samples (work_id, run_id);
CREATE INDEX IF NOT EXISTS idx_samples_run  ON work_samples (run_id);
CREATE INDEX IF NOT EXISTS idx_samples_live
    ON work_samples (work_id) WHERE analyzed;
CREATE TABLE IF NOT EXISTS sample_scores (
    sample_id        BIGINT    NOT NULL REFERENCES work_samples(id) ON DELETE CASCADE,
    model_version_id SMALLINT  NOT NULL REFERENCES model_versions(id),
    tag_ids          INTEGER[] NOT NULL,
    scores           REAL[]    NOT NULL,
    PRIMARY KEY (sample_id, model_version_id),
    CHECK (cardinality(tag_ids) = cardinality(scores)
           AND cardinality(tag_ids) > 0),
    CHECK (0 <= ALL (scores) AND 1 >= ALL (scores))
);
CREATE INDEX IF NOT EXISTS idx_sample_scores_version
    ON sample_scores (model_version_id);

CREATE TABLE IF NOT EXISTS measurements (
    id        BIGSERIAL PRIMARY KEY,
    run_id    BIGINT  NOT NULL REFERENCES scan_runs(id)    ON DELETE CASCADE,
    work_id   INTEGER NOT NULL REFERENCES works(id)        ON DELETE CASCADE,
    sample_id BIGINT           REFERENCES work_samples(id) ON DELETE CASCADE,
    kind      TEXT    NOT NULL,
    value     JSONB   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_measurements_work ON measurements (work_id, kind);
CREATE INDEX IF NOT EXISTS idx_measurements_sample
    ON measurements (sample_id, kind) WHERE sample_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_measurements_run ON measurements (run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_measurements_key
    ON measurements (run_id, work_id, sample_id, kind) NULLS NOT DISTINCT;

CREATE TABLE IF NOT EXISTS work_embeddings (
    run_id           BIGINT   NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    work_id          INTEGER  NOT NULL REFERENCES works(id)     ON DELETE CASCADE,
    sample_id        BIGINT   NOT NULL REFERENCES work_samples(id) ON DELETE CASCADE,
    model_version_id SMALLINT NOT NULL REFERENCES model_versions(id),
    vec              vector   NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_embeddings_key
    ON work_embeddings (work_id, model_version_id, run_id, sample_id);
CREATE INDEX IF NOT EXISTS idx_work_embeddings_run
    ON work_embeddings (run_id);
CREATE INDEX IF NOT EXISTS idx_work_embeddings_sample
    ON work_embeddings (sample_id);
"""


CHECK_SQL = """
CREATE TABLE IF NOT EXISTS check_items (
    id               BIGSERIAL PRIMARY KEY,
    job_id           BIGINT NOT NULL REFERENCES scan_jobs(id) ON DELETE CASCADE,
    path             TEXT NOT NULL,
    bytes            BIGINT,
    verdict          TEXT NOT NULL
                     CHECK (verdict IN ('same', 'near', 'new', 'error')),
    error            TEXT,
    embedding        vector,
    model_version_id SMALLINT REFERENCES model_versions(id),
    discarded_at     TIMESTAMPTZ,
    UNIQUE (job_id, path)
);

CREATE TABLE IF NOT EXISTS check_matches (
    item_id BIGINT NOT NULL REFERENCES check_items(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    score   REAL,
    PRIMARY KEY (item_id, work_id)
);
"""
