"""What the library already has of things a person points at outside it.

Two tiers and one question. An identical `content_key` is an answer with
no model in it, so it is settled first and the card never sees the item;
what is left is embedded once and compared with the work vectors of the
active profile's embedding model.

Nothing here writes a work: the moment a check wrote to `works` the answer
to "is this new" would be "no, you just added it".
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from polaris import config
from polaris.jobs import worker
from polaris.shared import tuning

from . import calibration, scanning

NEAR_TOP = 3

KNN = """
SELECT work_id AS id, 1 - (vec <=> $1) AS score
FROM derived.work_vectors WHERE model_version_id = $2
ORDER BY vec <=> $1 LIMIT $3
"""


def _bytes(path: Path) -> int | None:
    """Bytes on disk: the file, or every file under the folder."""
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return None


async def freeze(conn, paths: list[str]) -> dict:
    """What a check runs with: the items, the active profile's embedding
    model and its `check:near`."""
    row = await conn.fetchrow(
        "SELECT v.id, v.backend FROM model_profile_roles r "
        "JOIN model_versions v ON v.id = r.model_version_id "
        "WHERE r.profile_id = active_profile_id() AND r.role = 'embedding' "
        "ORDER BY v.id LIMIT 1")
    if row is None:
        raise ValueError("the active model profile has no embedding model")
    near = (await tuning.params(conn, {"check:near": 0.92}))["check:near"]
    return {"paths": list(paths), "version": dict(row), "near": near}


async def _record(conn, job_id: int, target: Path, verdict: str,
                  error: str | None = None,
                  embedding: list[float] | None = None,
                  version_id: int | None = None,
                  matches: list[tuple[int, float | None]] = ()) -> None:
    """One item's verdict and what it was reached against."""
    item_id = await conn.fetchval(
        "INSERT INTO check_items (job_id, path, bytes, verdict, error,"
        "                         embedding, model_version_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (job_id, path) DO UPDATE SET "
        "    bytes = EXCLUDED.bytes, verdict = EXCLUDED.verdict,"
        "    error = EXCLUDED.error, embedding = EXCLUDED.embedding,"
        "    model_version_id = EXCLUDED.model_version_id "
        "RETURNING id",
        job_id, scanning._storable(str(target)),
        await asyncio.to_thread(_bytes, target), verdict,
        scanning._storable(error) if error else None, embedding,
        version_id if embedding is not None else None)
    for work_id, score in matches:
        await conn.execute(
            "INSERT INTO check_matches (item_id, work_id, score) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            item_id, work_id, score)


def job_body(pipeline_factory: "scanning.PipelineFactory",
             on_note: Callable[[str], None] | None = None):
    """The callable `worker.run_job` runs for a check. Opens no run."""
    def note(msg: str) -> None:
        if on_note is not None:
            on_note(msg)

    async def body(conn, job) -> None:
        params = scanning.as_dict(job["params"])
        version, near = params["version"], params["near"]
        await config.load_collections(conn)
        await calibration.load_settings(conn)

        await conn.execute("DELETE FROM check_items WHERE job_id = $1",
                           job["id"])
        targets = [Path(p) for p in params["paths"]]
        note(f"checking {len(targets)} items")
        stop = await worker.beat(conn, job["id"], done=0, total=len(targets))

        done = 0
        pending: list[Path] = []
        for target in targets:
            if stop:
                return
            key = (await asyncio.to_thread(scanning.content_key, target)
                   if await asyncio.to_thread(target.exists) else None)
            owner = await conn.fetchval(
                "SELECT id FROM works WHERE content_key = $1", key) \
                if key else None
            if not await asyncio.to_thread(target.exists):
                await _record(conn, job["id"], target, "error",
                              error="not on disk")
                done += 1
            elif owner is not None:
                await _record(conn, job["id"], target, "same",
                              matches=[(owner, None)])
                done += 1
            else:
                pending.append(target)
            stop = await worker.beat(conn, job["id"], done=done,
                                     current_path=str(target))
        note(f"{done} settled without a model; {len(pending)} to look at")
        if stop or not pending:
            return

        keep = await scanning.curated_keep_tags(conn)
        pipeline, _ = await scanning.load_models(
            conn, {"versions": [version]}, pipeline_factory, keep)
        dims = await conn.fetchval(
            "SELECT vector_dims(vec) FROM derived.work_vectors "
            "WHERE model_version_id = $1 LIMIT 1", version["id"])
        observed = pipeline.observe_batch(
            pending, n_samples=None, show_progress=False,
            require_collection=False)

        for target, obs in zip(pending, observed):
            if stop:
                return
            vec = obs.embeddings.get(version["backend"])
            if obs.error:
                await _record(conn, job["id"], target, "error",
                              error=obs.error)
            elif vec is None or len(vec) != dims:
                await _record(
                    conn, job["id"], target, "error",
                    error=f"embedding: no comparable vector "
                          f"({0 if vec is None else len(vec)} dims, the "
                          f"library has {dims})")
            else:
                rows = await conn.fetch(KNN, vec, version["id"], NEAR_TOP)
                hits = [(r["id"], float(r["score"])) for r in rows
                        if float(r["score"]) >= near]
                await _record(conn, job["id"], target,
                              "near" if hits else "new", embedding=vec,
                              version_id=version["id"], matches=hits)
            done += 1
            stop = await worker.beat(conn, job["id"], done=done,
                                     current_path=str(target))

    return body
