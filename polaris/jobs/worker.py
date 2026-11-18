"""Claiming a scan job, holding it, and letting it go.

The queue is a table and the scheduler is one statement. `FOR UPDATE SKIP
LOCKED` is the whole of the mutual exclusion: two workers cannot take the
same row, and no lock is held across the hours the job itself runs -- the
lease is.

Nothing here knows what a scan is. `run_job` takes the work to do as a
callable so the claim loop can be tested without a GPU.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from datetime import datetime
from typing import Any, Awaitable, Callable

from polaris.derivation import derive

LEASE_S = 120
LEASE = f"{LEASE_S} seconds"
BEAT_EVERY = 30.0
SCANNER_KINDS = ("scan", "check", "build")
DERIVE_POLL_S = 2.0
STALL = "600 seconds"
LOCK_KEY = 0x706F6C61

log = logging.getLogger(__name__)

JOB_COLUMNS = (
    "id, collection, path, state, total, done, current_path, "
    "cancel_requested, worker_id, heartbeat_at, error, requested_at, "
    "finished_at, run_id, kind, model_profile_id, model_version_id, params")


def job_view(row) -> dict:
    """A job row as the page reads it: stale is a fact, not an inference."""
    beat = row["heartbeat_at"]
    stale = (
        row["state"] == "running" and beat is not None
        and (datetime.now(beat.tzinfo) - beat).total_seconds() > LEASE_S
    )
    params = row["params"]
    params = json.loads(params) if isinstance(params, str) else dict(params or {})
    return {
        "id": row["id"],
        "kind": row["kind"],
        "collection": row["collection"],
        "path": row["path"],
        "state": row["state"],
        "total": row["total"],
        "done": row["done"],
        "current_path": row["current_path"],
        "cancel_requested": row["cancel_requested"],
        "worker_id": row["worker_id"],
        "worker_stale": stale,
        "error": row["error"],
        "run_id": row["run_id"],
        "requested_at": row["requested_at"],
        "model_profile_id": row["model_profile_id"],
        "model_version_id": row["model_version_id"],
        "paths": params.get("paths"),
        "force": bool(params.get("force")),
        "finished_at": row["finished_at"],
    }


_CLAIM = """
UPDATE scan_jobs SET
    state = 'running',
    heartbeat_at = NOW(),
    progress_at = NOW(),
    worker_id = $1
WHERE id = (
    SELECT id FROM scan_jobs
    WHERE (state = 'queued'
       OR (state = 'running' AND heartbeat_at < NOW() - $2::text::interval))
      AND kind = ANY($3::text[])
    ORDER BY requested_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1)
RETURNING *
"""


def worker_id() -> str:
    """Who is holding the lease, for a person reading the table."""
    return f"{socket.gethostname()}:{os.getpid()}"


async def claim(conn, who: str | None = None, lease: str = LEASE,
                kinds=SCANNER_KINDS):
    """Take the oldest waiting job of these kinds, or one whose worker
    stopped talking."""
    return await conn.fetchrow(_CLAIM, who or worker_id(), lease, list(kinds))


async def beat(conn, job_id: int, done: int | None = None,
               current_path: str | None = None, total: int | None = None
               ) -> bool:
    """Renew the lease and report progress. True means stop.

    Progress and heartbeat are the same write, so saying where the scan is
    costs nothing beyond saying that it still lives.

    Two different requests mean stop, and both are read here: this job was
    cancelled, or the whole worker was asked to shut down. The second one
    otherwise would not be noticed until the job it is in the middle of
    finished, which for a full library is hours.

    The worker's own row is renewed in the same statement. `seen` is the
    only other writer of it and it runs between jobs, so a worker in the
    middle of a long scan would otherwise go quiet for as long as the scan
    took. A beat with nothing to report renews the lease but not
    `progress_at`, which is what the roster's `stalled` reads.
    """
    row = await conn.fetchrow("""
        WITH j AS (
            UPDATE scan_jobs SET
                heartbeat_at = NOW(),
                progress_at = CASE WHEN $2::int IS NULL AND $3::int IS NULL
                                        AND $4::text IS NULL
                                   THEN progress_at ELSE NOW() END,
                done = COALESCE($2, done),
                total = COALESCE($3, total),
                current_path = COALESCE($4, current_path)
            WHERE id = $1
            RETURNING worker_id, cancel_requested
        ), w AS (
            UPDATE scan_workers SET heartbeat_at = NOW(), models_loaded = TRUE
            WHERE id = (SELECT worker_id FROM j)
            RETURNING stop_requested
        )
        SELECT j.cancel_requested
               OR COALESCE((SELECT stop_requested FROM w), FALSE) AS stop
        FROM j""",
        job_id, done, total, current_path)
    return bool(row and row["stop"])


async def offload(conn, job_id: int, fn, *args):
    """`fn` on a thread, renewing the job's lease while it runs."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    while not task.done():
        await asyncio.wait({task}, timeout=BEAT_EVERY)
        if not task.done():
            await beat(conn, job_id)
    return task.result()


async def release(conn, job_id: int, state: str, error: str | None = None,
                  run_id: int | None = None) -> None:
    """Write the job's last word. Every exit runs through here."""
    await conn.execute("""
        UPDATE scan_jobs SET
            state = $2,
            error = $3,
            run_id = COALESCE($4, run_id),
            current_path = NULL,
            finished_at = CASE WHEN $2 IN ('done', 'failed', 'cancelled')
                               THEN NOW() END
        WHERE id = $1""", job_id, state, error, run_id)


async def request_cancel(conn, job_id: int) -> bool:
    """Ask a job to stop. Whether it has is a separate question.

    Except when nothing has started it: a queued job has no worker to
    notice the request, so it ends here rather than waiting to be picked
    up in order to be abandoned.
    """
    row = await conn.fetchrow("""
        UPDATE scan_jobs SET
            cancel_requested = TRUE,
            state = CASE WHEN state = 'queued' THEN 'cancelled' ELSE state END,
            finished_at = CASE WHEN state = 'queued' THEN NOW()
                               ELSE finished_at END
        WHERE id = $1 AND state IN ('queued', 'running')
        RETURNING id""", job_id)
    return row is not None


async def enqueue(conn, kind: str = "scan", *, collection: str | None = None,
                  path: str | None = None, params: dict | None = None,
                  model_profile_id: int | None = None,
                  model_version_id: int | None = None) -> int:
    """Ask for a job. Returns the id to watch."""
    return await conn.fetchval("""
        INSERT INTO scan_jobs (kind, collection, path, params,
                               model_profile_id, model_version_id)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6) RETURNING id""",
        kind, collection, path, json.dumps(params or {}),
        model_profile_id, model_version_id)


async def enqueue_derive(conn, profile_id: int, works=None) -> int:
    """Queue a re-derive of a profile, of every work or only `works`,
    unless a full one is already waiting."""
    waiting = await conn.fetchval(
        "SELECT id FROM scan_jobs WHERE kind = 'derive' AND state = 'queued' "
        "AND model_profile_id = $1 AND NOT params ? 'works' LIMIT 1",
        profile_id)
    params = None if works is None else {"works": sorted({int(w) for w in works})}
    return waiting or await enqueue(conn, "derive", params=params,
                                    model_profile_id=profile_id)


async def derive_body(conn, job) -> None:
    """Re-derive the job's works, every one unless its params name some,
    for the job's profile, beating per batch."""
    class Stop(Exception):
        pass

    params = job["params"]
    if isinstance(params, str):
        params = json.loads(params)
    works = (params or {}).get("works")

    async def on_batch(done: int, total: int) -> None:
        if await beat(conn, job["id"], done=done, total=total):
            raise Stop
    try:
        await derive.refresh_derived(conn, job["model_profile_id"],
                                     on_batch=on_batch, work_ids=works)
    except Stop:
        pass
    finally:
        await drop_unmaintained(conn, job["model_profile_id"])


async def drop_unmaintained(conn, profile_id: int) -> None:
    """Delete a profile's derived rows if it is no longer maintained."""
    for table in ("work_tags", "work_model_claims", "work_search"):
        await conn.execute(
            f"DELETE FROM derived.{table} WHERE profile_id = $1 AND NOT "
            f"(SELECT maintained FROM model_profiles WHERE id = $1)",
            profile_id)


async def serve_derive(store, who: str | None = None) -> None:
    """The API process's loop: take derive jobs one at a time, forever."""
    who = who or f"{worker_id()}:derive"
    while True:
        try:
            async with store.acquire() as conn:
                job = await claim(conn, who, kinds=("derive",))
                if job is not None:
                    await run_job(conn, job, derive_body)
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("derive loop")
        await asyncio.sleep(DERIVE_POLL_S)


_LIVE = f"""
SELECT a.application_name AS id, a.backend_start
FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory' AND l.granted
  AND l.classid = 0 AND l.objid = {LOCK_KEY} AND l.objsubid = 1
  AND l.database = (SELECT oid FROM pg_database
                    WHERE datname = current_database())
"""


async def hold(dsn: str, who: str):
    """The connection that makes this process the worker, or None when
    another process already is.

    An advisory lock lives as long as its connection, so a worker that
    dies however it dies lets go, and its `application_name` is how the
    roster names it. A connection of its own, since a pooled one is reset
    -- advisory locks included -- every time it goes back to the pool.
    """
    import asyncpg

    conn = await asyncpg.connect(
        dsn, server_settings={"application_name": who})
    if await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
        return conn
    await conn.close()
    return None


async def register(conn, who: str) -> None:
    """Say that this process is here and willing.

    Called holding the lock, so any other row is a worker that died
    without leaving, and goes. `stop_requested` is cleared: a stop applies
    to the process that was asked, not to whatever starts next.
    """
    await conn.execute("DELETE FROM scan_workers WHERE id <> $1", who)
    await conn.execute("""
        INSERT INTO scan_workers (id) VALUES ($1)
        ON CONFLICT (id) DO UPDATE SET
            started_at = NOW(), heartbeat_at = NOW(),
            stop_requested = FALSE, models_loaded = FALSE""", who)


async def seen(conn, who: str, models_loaded: bool = False) -> bool:
    """Still here, and holding this much. True means stop.

    The idle counterpart of `beat`: that one is written while a job runs,
    this one while nothing does, and between them a worker is never
    silent for longer than its poll interval.
    """
    row = await conn.fetchrow("""
        UPDATE scan_workers SET heartbeat_at = NOW(), models_loaded = $2
        WHERE id = $1 RETURNING stop_requested""", who, models_loaded)
    return bool(row and row["stop_requested"])


async def retire(conn, who: str) -> None:
    """Leave. A row that is gone is a worker that is gone."""
    await conn.execute("DELETE FROM scan_workers WHERE id = $1", who)


async def request_stop(conn, who: str | None = None) -> int:
    """Ask the worker to finish and leave. None asks whichever is there."""
    rows = await conn.fetch(
        "UPDATE scan_workers SET stop_requested = TRUE "
        "WHERE ($1::text IS NULL OR id = $1) AND NOT stop_requested "
        f"AND id IN (SELECT id FROM ({_LIVE}) l) "
        "RETURNING id", who)
    return len(rows)


async def roster(conn, stall: str = STALL) -> list:
    """The worker holding the lock, if any, and what it is doing.

    Alive is the lock and nothing else. `stalled` is a running job that
    has renewed its lease but reported no progress for `stall`.
    """
    return list(await conn.fetch(f"""
        SELECT l.id, substring(l.id from '[0-9]+$')::int AS pid,
               l.backend_start AS started_at,
               COALESCE(w.models_loaded, FALSE) AS models_loaded,
               COALESCE(w.stop_requested, FALSE) AS stop_requested,
               j.id AS job_id,
               COALESCE(j.progress_at < NOW() - $1::text::interval, FALSE)
                   AS stalled
        FROM ({_LIVE}) l
        LEFT JOIN scan_workers w ON w.id = l.id
        LEFT JOIN scan_jobs j ON j.worker_id = l.id AND j.state = 'running'
        ORDER BY l.backend_start""", stall))


JobBody = Callable[[Any, Any], Awaitable[int | None]]


async def run_job(conn, job, body: JobBody) -> str:
    """Run one claimed job, and make sure the row says how it ended.

    A job whose worker dies mid-way keeps its lease only until it expires,
    so the state this writes is the only one a reader can trust.
    """
    try:
        run_id = await body(conn, job)
    except asyncio.CancelledError:
        await release(conn, job["id"], "cancelled", "worker stopped")
        raise
    except Exception as e:
        log.exception("%s job %s failed", job["kind"], job["id"])
        await release(conn, job["id"], "failed", f"{type(e).__name__}: {e}")
        return "failed"

    cancelled = await conn.fetchval(
        "SELECT cancel_requested FROM scan_jobs WHERE id = $1", job["id"])
    state = "cancelled" if cancelled else "done"
    await release(conn, job["id"], state, run_id=run_id)
    return state
