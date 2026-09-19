"""HTTP endpoints for scan jobs, check jobs and reindexing."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from polaris import config, state
from polaris.catalog import trash
from polaris.catalog.api import _forget_check_roots, _readable, _size
from polaris.jobs import worker
from polaris.models.api import _profile_id
from polaris.observation import checking, scanning
from polaris.search import queries
from polaris.search.api import _work_to_item_resolved
from polaris.shared.errors import refuse

router = APIRouter()


def _under_a_collection(path: str) -> Path:
    """The path, or a refusal naming where this installation will look.

    A path is a filesystem path from whoever asked, and every endpoint
    that hands one to the disk needs the same answer.
    """
    target = Path(path)
    if config.collection_for(target) is None:
        roots = ", ".join(str(c.root) for c in config.collections()) or "none"
        raise refuse(400, "outside_collections",
                     f"{target} is under no configured root. Configured: "
                     f"{roots}", path=str(target))
    return target


class ScanJobRequest(BaseModel):
    """A rescan of what is registered under a collection or a folder, or
    `paths` to register as works and scan. `profile` defaults to the
    active model profile."""

    collection: str | None = None
    path: str | None = None
    paths: list[str] | None = None
    profile: str | None = None
    force: bool = False


def _registrable(target: Path) -> bool:
    """A folder of pages or a video file: the two things a work can be."""
    from polaris.observation.media.video import VIDEO_EXTENSIONS

    return target.is_dir() or (target.is_file()
                               and target.suffix.lower() in VIDEO_EXTENSIONS)


@router.post("/api/scan/jobs")
async def create_scan_job(req: ScanJobRequest):
    """Ask for a scan under a model profile. Returns the id to watch."""
    if req.paths is not None:
        if not req.paths:
            raise refuse(400, "paths_required",
                         "name at least one path to register")
        for path in req.paths:
            target = _under_a_collection(path)
            if not await asyncio.to_thread(_registrable, target):
                raise refuse(400, "not_registrable",
                             f"{target} is neither a folder nor a video file",
                             path=str(target))
    elif req.path:
        _under_a_collection(req.path)

    async with state.store.acquire() as conn:
        pid = await _profile_id(conn, req.profile)
        try:
            params = await scanning.freeze(conn, pid, force=req.force,
                                           paths=req.paths)
        except ValueError as e:
            raise refuse(400, "profile_has_no_members", str(e)) from None
        rescan = req.paths is None
        job_id = await worker.enqueue(
            conn, "scan", collection=req.collection if rescan else None,
            path=req.path if rescan else None, params=params,
            model_profile_id=pid)
    return {"job_id": job_id, "state": "queued"}


@router.post("/api/scan/jobs/{job_id}/retry")
async def retry_scan_job(job_id: int):
    """Queue a finished job again.

    Works every model already scored are skipped, so one that got partway
    through continues where it stopped. A scan or derive whose profile has
    since been deleted has nothing to run under.
    """
    import asyncpg

    async with state.store.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "UPDATE scan_jobs SET state = 'queued', "
                "    cancel_requested = FALSE, finished_at = NULL, "
                "    error = NULL, worker_id = NULL, heartbeat_at = NULL "
                "WHERE id = $1 AND state IN ('failed', 'cancelled') "
                "RETURNING id, run_id", job_id)
        except asyncpg.CheckViolationError:
            raise refuse(409, "job_profile_deleted",
                         "the model profile this job ran under is gone"
                         ) from None
    if row is None:
        raise refuse(409, "job_not_retryable",
                     "only a failed or cancelled job can be retried")
    return {"job_id": job_id, "state": "queued",
            "resuming": row["run_id"] is not None}


@router.get("/api/scan/jobs")
async def list_scan_jobs(limit: int = Query(20, ge=1, le=100),
                         kind: str | None = Query(None)):
    """The most recently asked-for jobs, of one kind or of every kind."""
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {worker.JOB_COLUMNS} "
            "FROM scan_jobs WHERE ($2::text IS NULL OR kind = $2) "
            "ORDER BY requested_at DESC LIMIT $1", limit, kind)
    return {"jobs": [worker.job_view(r) for r in rows]}


@router.get("/api/scan/jobs/{job_id}")
async def get_scan_job(job_id: int):
    """One scan's progress. Poll this."""
    async with state.store.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {worker.JOB_COLUMNS} "
            "FROM scan_jobs WHERE id = $1", job_id)
        if row is None:
            raise refuse(404, "job_not_found", "no such job")
        errors = await conn.fetchval(
            "SELECT COUNT(*) FROM scan_errors e JOIN scan_jobs j "
            "ON j.run_id = e.run_id WHERE j.id = $1", job_id)
    view = worker.job_view(row)
    view["errors"] = errors
    return view


@router.delete("/api/scan/jobs/{job_id}")
async def cancel_scan_job(job_id: int):
    """Ask a scan to stop. Whether it has yet is what GET says."""
    async with state.store.acquire() as conn:
        asked = await worker.request_cancel(conn, job_id)
    if not asked:
        raise refuse(409, "job_not_running", "job is not queued or running")
    return {"job_id": job_id, "cancel_requested": True}


@router.delete("/api/scan/jobs/{job_id}/record")
async def delete_scan_job(job_id: int):
    """Delete a finished job's row. Its run, samples and scores stay; a
    check's items go with it."""
    async with state.store.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM scan_jobs WHERE id = $1 "
            "AND state IN ('done', 'failed', 'cancelled') RETURNING kind",
            job_id)
        if row is None:
            exists = await conn.fetchval(
                "SELECT 1 FROM scan_jobs WHERE id = $1", job_id)
            if exists is None:
                raise refuse(404, "job_not_found", "no such job")
            raise refuse(409, "job_not_finished",
                         "only a done, failed or cancelled job can be deleted")
    if row["kind"] == "check":
        _forget_check_roots()
    return {"deleted": job_id}


@router.get("/api/scan/jobs/{job_id}/errors")
async def scan_job_errors(job_id: int, limit: int = Query(100, ge=1, le=1000)):
    """Which works failed in this scan, and where.

    `scan_errors` has recorded this per run, stage and path all along;
    nothing read it.
    """
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT e.path, e.stage, e.message, e.at "
            "FROM scan_errors e JOIN scan_jobs j ON j.run_id = e.run_id "
            "WHERE j.id = $1 ORDER BY e.id DESC LIMIT $2", job_id, limit)
    return {"errors": [dict(r) for r in rows]}


class CheckJobRequest(BaseModel):
    """Folders or video files outside the library, to compare with it."""

    paths: list[str]


@router.post("/api/check/jobs")
async def create_check_job(req: CheckJobRequest):
    """Ask what the library already has of these items. Returns the job."""
    if not req.paths:
        raise refuse(400, "paths_required", "name at least one item")
    for path in req.paths:
        target = Path(path)
        if not await asyncio.to_thread(_registrable, target):
            raise refuse(400, "not_registrable",
                         f"{target} is neither a folder nor a video file",
                         path=str(target))
        if config.collection_for(target) is not None:
            raise refuse(400, "inside_collections",
                         f"{target} is under a configured root; scan it "
                         f"instead", path=str(target))
    async with state.store.acquire() as conn:
        try:
            params = await checking.freeze(conn, req.paths)
        except ValueError as e:
            raise refuse(400, "no_embedding_model", str(e)) from None
        job_id = await worker.enqueue(conn, "check", params=params)
    _forget_check_roots()
    return {"job_id": job_id, "state": "queued", "paths": req.paths}


@router.get("/api/check/jobs/{job_id}/items")
async def check_job_items(job_id: int, verdict: str | None = Query(None),
                          limit: int = Query(50, ge=1, le=200),
                          offset: int = Query(0, ge=0)):
    """What a check concluded, duplicates first."""
    async with state.store.acquire() as conn:
        job = await conn.fetchrow(
            f"SELECT {worker.JOB_COLUMNS} "
            "FROM scan_jobs WHERE id = $1 AND kind = 'check'", job_id)
        if job is None:
            raise refuse(404, "check_not_found", "no such check")
        counts = {r["verdict"]: r["n"] for r in await conn.fetch(
            "SELECT verdict, COUNT(*) n FROM check_items WHERE job_id = $1 "
            "GROUP BY verdict", job_id)}
        rows = await conn.fetch(
            "SELECT id, path, bytes, verdict, error,"
            "       discarded_at IS NOT NULL AS discarded "
            "FROM check_items "
            "WHERE job_id = $1 AND ($2::text IS NULL OR verdict = $2) "
            "ORDER BY array_position("
            "    ARRAY['same','near','new','error'], verdict), path "
            "LIMIT $3 OFFSET $4", job_id, verdict, limit, offset)
        matches = await conn.fetch(
            "SELECT item_id, work_id, score FROM check_matches "
            "WHERE item_id = ANY($1::bigint[]) "
            "ORDER BY score DESC NULLS LAST", [r["id"] for r in rows])

        ids = sorted({m["work_id"] for m in matches})
        by_work = await queries.tags_by_work(conn, ids)
        found = await conn.fetch(
            "SELECT * FROM works_effective WHERE id = ANY($1::int[])", ids)

    works = {}
    for row in found:
        analysis = queries.row_to_work(row, by_work.get(row["id"], []))
        works[row["id"]] = await _work_to_item_resolved(analysis)

    hits: dict[int, list] = {}
    for m in matches:
        item = works.get(m["work_id"])
        if item is not None:
            hits.setdefault(m["item_id"], []).append(
                {"work": item,
                 "score": None if m["score"] is None else float(m["score"])})

    return {
        "job": worker.job_view(job),
        "total": sum(counts.values()),
        "counts": counts,
        "items": [{**dict(r), "matches": hits.get(r["id"], [])} for r in rows],
    }


@router.post("/api/check/items/{item_id}/discard")
async def discard_check_item(item_id: int):
    """Send one checked item to the Recycle Bin.

    Refused unless the library's own copy of what it matched is still on
    disk: that, and not a row, is what makes this not the last one. A
    verdict with no match is not deletable from here at all.
    """
    async with state.store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT i.path, i.discarded_at, p.path AS kept "
            "FROM check_items i "
            "JOIN check_matches m ON m.item_id = i.id "
            "JOIN work_paths p ON p.work_id = m.work_id AND p.present "
            "WHERE i.id = $1 ORDER BY m.score DESC NULLS LAST LIMIT 1",
            item_id)
        if row is None:
            raise refuse(404, "no_match",
                         "this item matched nothing the library still holds")
        if row["discarded_at"] is not None:
            raise refuse(409, "already_discarded", "already discarded")
        if not await asyncio.to_thread(Path(row["kept"]).exists):
            raise refuse(409, "library_copy_missing",
                         "the library's own copy is not on disk",
                         path=row["kept"])

        target = await _readable(row["path"])
        try:
            await asyncio.to_thread(trash.recycle, target, _size(target) or 0)
        except trash.Refused as e:
            raise refuse(409, "recycle_refused", str(e))
        await conn.execute(
            "UPDATE check_items SET discarded_at = NOW() WHERE id = $1",
            item_id)
    return {"discarded": row["path"], "kept": row["kept"]}


@router.delete("/api/check/jobs/{job_id}")
async def forget_check_job(job_id: int):
    """Drop a check and its results, and stop serving its root."""
    async with state.store.acquire() as conn:
        gone = await conn.fetchval(
            "DELETE FROM scan_jobs WHERE id = $1 AND kind = 'check' "
            "RETURNING id", job_id)
    if gone is None:
        raise refuse(404, "check_not_found", "no such check")
    _forget_check_roots()
    return {"forgotten": job_id}


@router.post("/api/reindex")
async def reindex_work(path: str = Query(..., description="Folder to redo")):
    """Observe one work again. Returns the job to watch, not a result.

    It used to run the models inline, which held the event loop for the
    whole of a work and kept two gigabytes on the card for the ninety-nine
    percent of the time nobody was reindexing anything. It is a scan of one
    folder, and scans are jobs.

    Every model runs again on freshly chosen samples.
    """
    _under_a_collection(path)
    async with state.store.acquire() as conn:
        pid = await _profile_id(conn, None)
        try:
            params = await scanning.freeze(conn, pid, force=True)
        except ValueError as e:
            raise refuse(400, "profile_has_no_members", str(e)) from None
        job_id = await worker.enqueue(conn, "scan", path=path, params=params,
                                      model_profile_id=pid)
    return {"job_id": job_id, "state": "queued", "path": path}
