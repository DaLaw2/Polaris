"""Tables of the job queue and the workers that take from it."""

SQL = """
CREATE TABLE IF NOT EXISTS scan_jobs (
    id               BIGSERIAL PRIMARY KEY,
    kind             TEXT NOT NULL DEFAULT 'scan'
                     CHECK (kind IN ('scan', 'check', 'derive', 'build')),
    collection       TEXT,
    path             TEXT,
    state            TEXT NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'running', 'done',
                                      'failed', 'cancelled')),
    total            INTEGER,
    done             INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ,
    heartbeat_at     TIMESTAMPTZ,
    worker_id        TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    current_path     TEXT,
    params           JSONB NOT NULL,
    run_id           BIGINT REFERENCES scan_runs(id) ON DELETE SET NULL,
    model_profile_id SMALLINT REFERENCES model_profiles(id),
    model_version_id SMALLINT REFERENCES model_versions(id),
    CONSTRAINT scan_jobs_finished_state
        CHECK ((state IN ('done', 'failed', 'cancelled'))
               = (finished_at IS NOT NULL)),
    CONSTRAINT scan_jobs_target CHECK (
        (kind NOT IN ('scan', 'derive') OR model_profile_id IS NOT NULL
         OR finished_at IS NOT NULL)
        AND (kind <> 'build' OR model_version_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_queue
    ON scan_jobs (requested_at) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS idx_scan_jobs_stale
    ON scan_jobs (heartbeat_at) WHERE state = 'running';
CREATE TABLE IF NOT EXISTS scan_workers (
    id             TEXT PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    models_loaded  BOOLEAN NOT NULL DEFAULT FALSE,
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE
);
"""
