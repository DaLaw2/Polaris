"""One scan job: observe registered works for a model profile.

A job either registers the paths a person picked or rescans the works
already registered under a collection or a folder. Each work runs only the
profile's models that have no scores for it, on the samples it already
has; a new work, changed content or `force` observes it afresh.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from polaris import config
from polaris.catalog import identity
from polaris.jobs import worker
from polaris.models import profiles

from . import calibration, ingest
from .domain import Sample


@dataclass
class ScanProgress:
    """One work, finished."""

    run_id: int | None
    done: int
    total: int
    name: str
    path: str
    status: str
    rate: float
    eta_s: float


@dataclass
class ScanSummary:
    """What the job did, for whoever asked for it."""

    run_id: int | None
    written: int = 0
    rescored: int = 0
    skipped: int = 0
    errors: int = 0
    absent: int = 0
    elapsed_s: float = 0.0
    cancelled: bool = False
    error_stages: dict[str, int] = field(default_factory=dict)


OnProgress = Callable[[ScanProgress], Awaitable[None]]
ShouldCancel = Callable[[], Awaitable[bool]]
PipelineFactory = Callable[
    [frozenset[str], "list[dict]", "int | None"], "object"]


@dataclass
class _Target:
    path: Path
    plan: object = None
    work_id: int | None = None


def as_dict(value) -> dict:
    """jsonb comes back as text unless a codec says otherwise."""
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def content_key(target: Path) -> str | None:
    """A work's identity: a folder's file names and sizes, a file's size
    and extension."""
    from polaris.observation.media.sampler import content_key as folder_key

    from .pipeline import _file_content_key

    return folder_key(target) if target.is_dir() else _file_content_key(target)


async def freeze(conn, profile_id: int, *, force: bool = False,
                 paths: list[str] | None = None) -> dict:
    """What a scan job runs with, fixed when it is asked for: the profile's
    member versions, whether to observe afresh, and the paths to register."""
    rows = await conn.fetch(
        "SELECT v.id, v.backend FROM model_profile_members m "
        "JOIN model_versions v ON v.id = m.model_version_id "
        "WHERE m.profile_id = $1 ORDER BY v.backend, m.rank", profile_id)
    if not rows:
        raise ValueError(f"model profile {profile_id} has no members")
    out = {"versions": [dict(r) for r in rows], "force": force}
    if paths is not None:
        out["paths"] = list(paths)
    return out


async def registered(conn, collection: str | None,
                     path: str | None) -> tuple[list[str], list[str]]:
    """Work paths and copy paths registered under a collection, a folder,
    or every searchable collection when neither is named."""
    exact = str(Path(path)) if path else None
    prefix = exact.rstrip("\\/") + os.sep if exact else None
    out = []
    for table in ("work_paths", "work_copies"):
        present = "AND t.present" if table == "work_paths" else ""
        out.append([r["path"] for r in await conn.fetch(
            f"""
            SELECT t.path FROM {table} t
            JOIN collections c ON c.name = t.collection
            WHERE ($1::text IS NULL OR t.collection = $1)
              AND ($2::text IS NULL OR t.path = $2
                   OR left(t.path, length($3)) = $3)
              AND ($1::text IS NOT NULL OR $2::text IS NOT NULL OR c.searchable)
              {present}
            ORDER BY t.path
            """, collection, exact, prefix)])
    return out[0], out[1]


def _storable(text: str) -> str:
    """The same text, minus unpaired surrogates PostgreSQL will not accept."""
    return text.encode("utf-8", "replace").decode("utf-8")


async def _note_error(conn, run_id, path, stage, message) -> None:
    """Write to `scan_errors`, and never fail the scan because of it."""
    try:
        await ingest.record_error(conn, run_id, _storable(path), stage,
                                  _storable(message))
    except Exception:
        pass


async def curated_keep_tags(conn) -> frozenset[str]:
    """Model tags someone mapped, kept regardless of how low they scored."""
    return frozenset(r["term"] for r in await conn.fetch(
        "SELECT DISTINCT term FROM term_map WHERE NOT search_only"))


async def _plan(conn, targets: list[Path], loaded: dict[str, int],
                force: bool) -> list[_Target]:
    """Decide per work: observe afresh, score with the missing models on
    the samples it has, or leave it alone (no plan)."""
    from .pipeline import WorkPlan

    rows = {r["path"]: r for r in await conn.fetch(
        """
        SELECT p.path, w.id, w.content_key,
               ARRAY(SELECT DISTINCT ss.model_version_id
                     FROM work_samples s
                     JOIN sample_scores ss ON ss.sample_id = s.id
                     WHERE s.work_id = w.id) AS scored
        FROM work_paths p JOIN works w ON w.id = p.work_id
        WHERE p.present AND p.path = ANY($1::text[])
        """, [str(t) for t in targets])}

    out = []
    for target in targets:
        row = rows.get(str(target))
        key = await asyncio.to_thread(content_key, target)
        if force or row is None or key is None or key != row["content_key"]:
            out.append(_Target(target, WorkPlan(target)))
            continue
        missing = frozenset(n for n, v in loaded.items()
                            if v not in row["scored"])
        if not missing:
            out.append(_Target(target))
            continue
        samples = [Sample(r["medium"], r["source_relpath"],
                          float(r["position"]), r["ordinal"])
                   for r in await conn.fetch(
                       "SELECT medium, source_relpath, position, ordinal "
                       "FROM work_samples WHERE work_id = $1 AND analyzed "
                       "AND run_id = (SELECT MAX(run_id) FROM work_samples "
                       "              WHERE work_id = $1) "
                       "ORDER BY ordinal", row["id"])]
        if samples:
            out.append(_Target(target, WorkPlan(target, missing, samples),
                               row["id"]))
        else:
            out.append(_Target(target, WorkPlan(target)))
    return out


async def load_models(conn, params: dict, pipeline_factory: PipelineFactory,
                      keep: frozenset[str]):
    """Build the pipeline for the job's frozen versions, each from its
    stored definition, and check each loads at its version's revision.
    One version runs per model, the first frozen. Returns the pipeline and
    backend → version."""
    chosen: dict[str, int] = {}
    for v in params["versions"]:
        chosen.setdefault(v["backend"], v["id"])
    rows = [dict(r) for r in await conn.fetch(
        "SELECT v.id, v.backend, v.revision, v.precision, v.state, "
        "       d.source_text "
        "FROM model_versions v "
        "LEFT JOIN model_definitions d ON d.id = v.definition_id "
        "WHERE v.id = ANY($1::smallint[]) ORDER BY v.backend",
        list(chosen.values()))]
    gone = set(chosen.values()) - {r["id"] for r in rows}
    if gone:
        raise ValueError(f"model versions {sorted(gone)} no longer exist")
    for r in rows:
        if r["state"] != "ready":
            raise ValueError(f"{r['backend']} version {r['id']} is "
                             f"{r['state']}, not ready")
    pipeline = pipeline_factory(keep, rows, None)
    pipeline.tagger.ensure_loaded()
    by_name = {r["backend"]: r for r in rows}
    loaded = {}
    for cfg in pipeline.tagger._configs:
        row = by_name[cfg.backend.name]
        got = profiles.snapshot_revision(
            getattr(cfg.backend, "model_path", None))
        if got != row["revision"]:
            raise ValueError(
                f"{row['backend']} loaded revision {got}, but version "
                f"{row['id']} is revision {row['revision']}")
        loaded[row["backend"]] = row["id"]
    return pipeline, loaded


async def execute(
    conn,
    params: dict,
    targets: list[Path],
    pipeline_factory: PipelineFactory,
    *,
    job_id: int | None = None,
    run_id: int | None = None,
    retire_missing: bool = False,
    on_progress: OnProgress | None = None,
    on_note: Callable[[str], None] | None = None,
    should_cancel: ShouldCancel | None = None,
    chunk: int = 64,
) -> ScanSummary:
    """Observe `targets` under the job's frozen params.

    A run is opened only when something is observed afresh or fails. A
    target missing from disk is retired when `retire_missing`, an error
    otherwise.
    """
    def note(msg: str) -> None:
        if on_note is not None:
            on_note(msg)

    await calibration.load_settings(conn)
    keep = await curated_keep_tags(conn)
    pipeline, loaded = await load_models(conn, params, pipeline_factory, keep)
    published, moved = await profiles.sync_thresholds(
        conn, [c.backend for c in pipeline.tagger._configs], loaded)
    for name, n in published.items():
        note(f"{name} publishes {n} per-tag thresholds")
    if moved:
        for r in await conn.fetch(
                "SELECT DISTINCT p.id FROM model_profiles p "
                "JOIN model_profile_members m ON m.profile_id = p.id "
                "WHERE p.maintained AND m.model_version_id = ANY($1::smallint[])",
                list(loaded.values())):
            await worker.enqueue_derive(conn, r["id"])
        note("per-tag thresholds changed; queued a re-derive")

    out = ScanSummary(run_id=run_id)
    opened = False

    async def run() -> int:
        nonlocal opened
        if out.run_id is None:
            out.run_id = await ingest.begin_run(
                conn, calibration.observation_params(
                    keep_tags=sorted(keep), model_versions=loaded,
                    max_images={c.backend.name: c.max_images
                                for c in pipeline.tagger._configs}))
            if job_id is not None:
                await conn.execute(
                    "UPDATE scan_jobs SET run_id = $2 WHERE id = $1",
                    job_id, out.run_id)
            note(f"scan run {out.run_id}")
        opened = True
        return out.run_id

    force = bool(params.get("force"))
    total = len(targets)
    t0 = time.perf_counter()
    missing: list[str] = []

    async def report(path: Path, status: str) -> None:
        done = out.written + out.rescored + out.skipped + out.errors
        elapsed = time.perf_counter() - t0
        rate = done / elapsed if elapsed > 0 else 0.0
        if on_progress is not None:
            await on_progress(ScanProgress(
                run_id=out.run_id, done=done, total=total, name=path.name,
                path=str(path), status=status, rate=rate,
                eta_s=(total - done) / rate if rate > 0 else 0.0))

    for i in range(0, total, chunk):
        if should_cancel is not None and await should_cancel():
            out.cancelled = True
            break
        here = []
        for target in targets[i:i + chunk]:
            if await asyncio.to_thread(target.exists):
                here.append(target)
            elif retire_missing:
                missing.append(str(target))
                out.skipped += 1
                await report(target, "not on disk")
            else:
                out.errors += 1
                await _note_error(conn, await run(), str(target), "missing",
                                  "not on disk")
                await report(target, "not on disk")

        planned = await _plan(conn, here, loaded, force)
        for t in planned:
            if t.plan is None:
                out.skipped += 1
                await report(t.path, "every model has scored it")
        todo = [t for t in planned if t.plan is not None]
        observed = pipeline.observe_batch([t.plan for t in todo],
                                          n_samples=None, show_progress=False)
        for t, obs in zip(todo, observed):
            if obs.error:
                out.errors += 1
                await _note_error(conn, await run(), obs.path,
                                  obs.error.partition(": ")[0], obs.error)
                status = obs.error
            else:
                try:
                    if t.work_id is None:
                        await ingest.record_observation(conn, await run(),
                                                        obs, loaded)
                        out.written += 1
                    else:
                        await ingest.record_scores(conn, t.work_id, obs,
                                                   loaded)
                        out.rescored += 1
                    status = (f"{len(obs.analyzed_samples)} samples, "
                              f"{'all models' if t.plan.only is None else ', '.join(sorted(t.plan.only))}")
                except Exception as e:
                    out.errors += 1
                    await _note_error(conn, await run(), obs.path, "write",
                                      str(e))
                    status = f"write failed: {e}"
            await report(t.path, status)
            if should_cancel is not None and await should_cancel():
                out.cancelled = True
                break
        if out.cancelled:
            break

    out.elapsed_s = time.perf_counter() - t0
    if not out.cancelled:
        if missing and len(missing) == total:
            note("nothing registered here is on disk; leaving it present")
        elif missing:
            out.absent = await identity.mark_absent(conn, missing)
        if opened:
            await ingest.finish_run(conn, out.run_id)
    if opened:
        out.error_stages = {r["stage"]: r["n"] for r in await conn.fetch(
            "SELECT stage, COUNT(*) n FROM scan_errors WHERE run_id = $1 "
            "GROUP BY stage ORDER BY 2 DESC", out.run_id)}
    return out


def job_body(pipeline_factory: PipelineFactory,
             on_note: Callable[[str], None] | None = None):
    """The callable `worker.run_job` runs: one scan, reporting into its row.

    Progress, heartbeat and the cancel check are one write per work.
    """
    async def body(conn, job) -> int | None:
        await config.load_collections(conn)
        params = as_dict(job["params"])
        register = "paths" in params
        if register:
            targets = [Path(p) for p in params["paths"]]
        else:
            paths, copies = await registered(conn, job["collection"],
                                             job["path"])
            targets = [Path(p) for p in paths]
            gone = [c for c in copies
                    if not await asyncio.to_thread(Path(c).exists)]
            if gone:
                await identity.mark_absent(conn, gone)
        stop = await worker.beat(conn, job["id"], done=0, total=len(targets))

        async def progress(p: ScanProgress) -> None:
            nonlocal stop
            stop = await worker.beat(conn, job["id"], done=p.done,
                                     total=p.total, current_path=p.path)

        async def cancelled() -> bool:
            return stop

        out = await execute(conn, params, targets, pipeline_factory,
                            job_id=job["id"], run_id=job["run_id"],
                            retire_missing=not register,
                            on_progress=progress, on_note=on_note,
                            should_cancel=cancelled)
        return out.run_id

    return body
