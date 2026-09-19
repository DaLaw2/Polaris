"""HTTP endpoints for the scan worker process and the GPU it uses."""

import asyncio
import subprocess
import sys
import time

from fastapi import APIRouter, Query

from polaris import config, state
from polaris.jobs import worker
from polaris.shared.errors import refuse

router = APIRouter()


@router.get("/api/gpu")
async def gpu():
    """What is on the card right now, if there is a card.

    The only thing in this application that puts anything there is the
    scan worker, so this is how the page answers "is something holding my
    graphics card" without anybody having to believe a claim about it.
    """
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"present": False}
    if out.returncode != 0 or not out.stdout.strip():
        return {"present": False}
    name, used, total = (s.strip() for s in out.stdout.splitlines()[0].split(","))
    return {"present": True, "name": name,
            "used_mb": int(used), "total_mb": int(total)}


WORKER_LOG = config.PROJECT_ROOT / "data" / "scan_worker.log"


@router.get("/api/scan/worker")
async def scan_worker_status():
    """Who is willing to take a job, and whether they are still talking.

    `stale` is the same judgement the claim statement makes when it takes
    a job away from a silent worker, so a reader is told what the queue
    already believes. A stale worker's `models_loaded` is its last word,
    not a fact about the card now, and the page says so rather than
    repeating it.

    `queued` counts what waits for a scan worker; `by_kind` counts every
    kind, derive included, queued and running.
    """
    async with state.store.acquire() as conn:
        rows = await worker.roster(conn)
        counts = await conn.fetch(
            "SELECT kind, state, COUNT(*) AS n FROM scan_jobs "
            "WHERE state IN ('queued', 'running') GROUP BY kind, state")
    by_kind = {k: {"queued": 0, "running": 0}
               for k in (*worker.SCANNER_KINDS, "derive")}
    for r in counts:
        by_kind[r["kind"]][r["state"]] = r["n"]
    queued = sum(by_kind[k]["queued"] for k in worker.SCANNER_KINDS)
    return {"workers": [dict(r) for r in rows], "queued": queued,
            "by_kind": by_kind, "lease_s": worker.LEASE_S}


WORKER_START_TIMEOUT_S = 30.0


@router.post("/api/scan/worker")
async def start_scan_worker(force: bool = Query(False)):
    """Start a worker process, detached, and wait until it says it is up.

    Detached on purpose: restarting the API must not take a scan with it,
    which is the whole reason the worker is a separate process. It gets no
    console window either, so its output goes to a file -- a worker that
    dies before it can register (no CUDA, an unreachable database, a
    missing model) would otherwise fail invisibly, and explaining that
    failure is the one thing a web-first page cannot do without.

    What comes back is the row the worker wrote, not the pid `Popen`
    returned: on Windows a virtualenv's python.exe is a stub that launches
    the real interpreter as another process, so that number names
    something that has already exited. The worker's own name for itself is
    the only one that is true, and waiting for it is also how this answers
    "did it start" rather than "was it launched".
    """
    async with state.store.acquire() as conn:
        before = {w["id"] for w in await worker.roster(conn)}
        live = [w for w in await worker.roster(conn) if not w["stale"]]
    if live and not force:
        raise refuse(409, "worker_running", f"{live[0]['id']} is already here",
                     worker=live[0]["id"])

    WORKER_LOG.parent.mkdir(parents=True, exist_ok=True)
    detach = ({"creationflags": subprocess.CREATE_NO_WINDOW
                                | subprocess.CREATE_NEW_PROCESS_GROUP}
              if sys.platform == "win32" else {"start_new_session": True})
    argv = ([sys.executable, "-u", "-m", "polaris.cli.scan_worker"]
            + (["--force"] if force else []))
    with open(WORKER_LOG, "ab") as log:
        subprocess.Popen(
            argv, cwd=str(config.PROJECT_ROOT), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, **detach)

    deadline = time.monotonic() + WORKER_START_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        async with state.store.acquire() as conn:
            new = [w for w in await worker.roster(conn)
                   if w["id"] not in before]
        if new:
            return {"worker": dict(new[0]), "log": str(WORKER_LOG)}

    raise refuse(504, "worker_start_timeout",
                 f"the worker did not register within "
                 f"{WORKER_START_TIMEOUT_S:.0f}s; see GET /api/scan/worker/log")


@router.delete("/api/scan/worker")
async def stop_scan_worker(id: str | None = Query(None),
                           forget: bool = Query(False)):
    """Ask a worker to finish and leave. No id asks all of them.

    Asks; does not kill. A worker in the middle of a work finishes writing
    it and stops at the next one, which is the same bargain `cancel` makes
    with a job.

    `forget` is the other case: a worker that is already gone and left its
    row behind cannot be asked anything, and the row is what the page shows
    as a process that exists. It only ever removes a row past its lease.
    """
    async with state.store.acquire() as conn:
        if forget:
            return {"forgotten": await worker.forget(conn, id)}
        asked = await worker.request_stop(conn, id)
    return {"asked": asked}


@router.get("/api/scan/worker/log")
async def scan_worker_log(lines: int = Query(50, ge=1, le=500)):
    """The tail of what the worker printed, since it has no console."""
    if not WORKER_LOG.exists():
        return {"lines": [], "path": str(WORKER_LOG)}
    with open(WORKER_LOG, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 8192))
        text = f.read().decode("utf-8", "replace")
    return {"lines": text.splitlines()[-lines:], "path": str(WORKER_LOG)}


@router.get("/api/overview")
async def overview():
    """Every count the overview shows, and the jobs queued or running, in
    one read cheap enough to poll."""
    async with state.store.acquire() as conn:
        counts = await conn.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM scan_jobs WHERE state = 'failed')
                  AS failed_jobs,
              (SELECT COUNT(*) FROM (
                   SELECT 1 FROM term_map WHERE NOT search_only
                   GROUP BY lower(term)
                   HAVING COUNT(DISTINCT concept_id) > 1) x) AS conflicts,
              (SELECT COUNT(*) FROM concepts
               WHERE kind = 'character' AND parent_id IS NULL)
                  AS characters_without_series,
              (SELECT COUNT(*) FROM work_search WHERE work_type IS NULL)
                  AS untyped_works,
              (SELECT COUNT(*) FROM collections c
               WHERE c.searchable AND NOT EXISTS (
                   SELECT 1 FROM work_paths p
                   WHERE p.collection = c.name AND p.present))
                  AS empty_collections
            """)
        unplaced = dict(await conn.fetch(
            """
            SELECT t.category, COUNT(*)
            FROM (SELECT DISTINCT category, tag FROM work_tags
                  WHERE category IN ('character', 'copyright')) t
            JOIN claim_fields f ON f.model_mappable
             AND f.field = CASE t.category WHEN 'copyright' THEN 'series'
                                           ELSE t.category END
            WHERE NOT EXISTS (SELECT 1 FROM term_map tm
                              WHERE lower(tm.term) = lower(t.tag))
            GROUP BY t.category
            """))
        jobs = await conn.fetch(
            f"SELECT {worker.JOB_COLUMNS} FROM scan_jobs "
            "WHERE state IN ('running', 'queued') "
            "ORDER BY state = 'queued', requested_at, id")
    out = dict(counts)
    out["unplaced_characters"] = unplaced.get("character", 0)
    out["unplaced_series"] = unplaced.get("copyright", 0)
    out["unplaced"] = out["unplaced_characters"] + out["unplaced_series"]
    out["jobs"] = [worker.job_view(r) for r in jobs]
    return out
