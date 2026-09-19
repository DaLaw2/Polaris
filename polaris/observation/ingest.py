"""Writing observations into the raw layer.

One rule, and everything else follows from it: **nothing here decides
anything.** No threshold is applied, no argmax is taken, no score is
multiplied by a frequency, no page is dropped without a row saying why.
What the disk said and what the models said goes in exactly as observed,
and every question about what it means is answered later, in SQL, by
someone who can change their mind.

Writing conclusions instead makes every threshold permanent: the numbers
they came from are gone, so changing one costs a full rescan.
"""

from __future__ import annotations

import json

from polaris.catalog import identity
from polaris.derivation import derive
from polaris.models import profiles

from .domain import WorkObservation

VALID_SKIP_REASONS = {"blank", "too_small", "decode_error", "duplicate_scene"}


async def record_observation(conn, run_id: int, obs: WorkObservation,
                             versions: dict[str, int] | None = None) -> int:
    """Write one work's observations. Returns its work id.

    `versions` maps each backend name to the model version its scores came
    from. Idempotent within a run. What older runs hold of the same versions
    is replaced; a run nothing refers to any more is deleted, and a work
    whose content changed keeps no older run at all. The work's vectors and
    derived rows are rebuilt before the transaction commits.

    A path repeating content that is still on disk somewhere else is a
    copy: it goes to `work_copies` and nothing observed there is written,
    because the work already holds those observations.
    """
    if not obs.content_key:
        raise ValueError(f"no content key for {obs.path}")
    async with conn.transaction():
        original = await identity.copy_of(conn, obs.content_key, obs.path)
        if original is not None:
            displaced = await identity.record_copy(conn, original, obs.path,
                                                   obs.collection)
            await derive.derive_works(conn, [original, *displaced])
            return original

        work_id, displaced, changed = await identity.upsert(
            conn, obs.content_key, obs.path)
        await identity.record_path(conn, work_id, obs.path, obs.collection)

        sample_ids = await _record_samples(conn, run_id, work_id, obs)
        by_backend = await _versions(conn, obs, versions)
        await _record_scores(conn, obs, sample_ids, by_backend)
        await _record_measurements(conn, run_id, work_id, obs, sample_ids)
        await _record_embeddings(conn, run_id, work_id, obs, sample_ids,
                                 by_backend)
        await _prune(conn, work_id, run_id, list(by_backend.values()), changed)
        await derive.materialize_vectors(conn, [work_id])
        await derive.derive_works(conn, [work_id, *displaced])
    return work_id


async def record_scores(conn, work_id: int, obs: WorkObservation,
                        versions: dict[str, int] | None = None) -> None:
    """Write scores for samples a run already recorded, leaving the samples
    and measurements as they are."""
    async with conn.transaction():
        run_id = await conn.fetchval(
            "SELECT MAX(run_id) FROM work_samples WHERE work_id = $1", work_id)
        if run_id is None:
            raise ValueError(f"work {work_id} has no samples to score")
        sample_ids = {r["ordinal"]: r["id"] for r in await conn.fetch(
            "SELECT id, ordinal FROM work_samples "
            "WHERE work_id = $1 AND run_id = $2", work_id, run_id)}
        by_backend = await _versions(conn, obs, versions)
        written = list(by_backend.values())
        await conn.execute(
            "DELETE FROM sample_scores ss USING work_samples s "
            "WHERE ss.sample_id = s.id AND s.work_id = $1 AND s.run_id = $2 "
            "AND ss.model_version_id = ANY($3::smallint[])",
            work_id, run_id, written)
        await _record_scores(conn, obs, sample_ids, by_backend)
        await _record_embeddings(conn, run_id, work_id, obs, sample_ids,
                                 by_backend)
        await _prune(conn, work_id, run_id, written, False)
        await derive.materialize_vectors(conn, [work_id], written)
        await derive.derive_works(conn, [work_id])


async def _prune(conn, work_id: int, run_id: int, versions: list[int],
                 changed: bool) -> None:
    """Drop what older runs hold of these versions, then every older run
    nothing refers to, or every older run when the content changed."""
    await conn.execute(
        "DELETE FROM sample_scores ss USING work_samples s "
        "WHERE ss.sample_id = s.id AND s.work_id = $1 AND s.run_id < $2 "
        "AND ss.model_version_id = ANY($3::smallint[])",
        work_id, run_id, versions)
    await conn.execute(
        "DELETE FROM work_embeddings WHERE work_id = $1 AND run_id < $2 "
        "AND model_version_id = ANY($3::smallint[])",
        work_id, run_id, versions)
    await conn.execute(
        """
        WITH stale AS (
            SELECT DISTINCT s.run_id FROM work_samples s
            WHERE s.work_id = $1 AND s.run_id < $2
              AND ($3 OR (
                  NOT EXISTS (SELECT 1 FROM work_samples x
                              JOIN sample_scores ss ON ss.sample_id = x.id
                              WHERE x.work_id = $1 AND x.run_id = s.run_id)
                  AND NOT EXISTS (SELECT 1 FROM work_embeddings e
                                  WHERE e.work_id = $1 AND e.run_id = s.run_id)))
        ), m AS (
            DELETE FROM measurements
            WHERE work_id = $1 AND run_id IN (SELECT run_id FROM stale))
        DELETE FROM work_samples
        WHERE work_id = $1 AND run_id IN (SELECT run_id FROM stale)
        """, work_id, run_id, changed)


async def _record_samples(
    conn, run_id: int, work_id: int, obs: WorkObservation
) -> dict[int, int]:
    """Insert every sample, kept or not. Returns ordinal → sample id."""
    await conn.execute(
        "DELETE FROM work_samples WHERE work_id = $1 AND run_id = $2",
        work_id, run_id)
    if not obs.samples:
        return {}

    for s in obs.samples:
        if s.skip_reason is not None and s.skip_reason not in VALID_SKIP_REASONS:
            raise ValueError(
                f"unknown skip_reason {s.skip_reason!r} on {obs.path}")

    rows = await conn.fetch(
        """
        INSERT INTO work_samples (
            run_id, work_id, medium, source_relpath, position, ordinal,
            skip_reason
        )
        SELECT $1, $2, m, r, p, o, sr
        FROM unnest($3::text[], $4::text[], $5::numeric[], $6::int[],
                    $7::text[]) AS t(m, r, p, o, sr)
        RETURNING id, ordinal
        """,
        run_id, work_id,
        [s.medium for s in obs.samples],
        [s.source_relpath for s in obs.samples],
        [s.position for s in obs.samples],
        [s.ordinal for s in obs.samples],
        [s.skip_reason for s in obs.samples],
    )
    return {r["ordinal"]: r["id"] for r in rows}


async def _versions(conn, obs: WorkObservation,
                    versions: dict[str, int] | None) -> dict[str, int]:
    """The model version of every backend that said anything about this
    work, resolving names the caller did not map."""
    names = {b for per in obs.scores for b in per}
    out = {n: v for n, v in (versions or {}).items() if n in names}
    for name in sorted(names - out.keys()):
        out[name] = await profiles.model_version(conn, name)
    return out


async def _record_scores(
    conn, obs: WorkObservation, sample_ids: dict[int, int],
    versions: dict[str, int],
) -> None:
    """Every backend's unthresholded output, kept per model version.

    No merging: picking a winner by priority is right when producing an
    answer and wrong when recording evidence, because the disagreement is
    what gets discarded.
    """
    analyzed = obs.analyzed_samples
    if not analyzed or not obs.scores:
        return

    best: dict[tuple, float] = {}
    for sample, by_backend in zip(analyzed, obs.scores):
        sid = sample_ids.get(sample.ordinal)
        if sid is None:
            continue
        for backend, seen in by_backend.items():
            for rs in seen.scores:
                key = (sid, backend, rs.category, rs.tag)
                if rs.score > best.get(key, -1.0):
                    best[key] = rs.score

    if not best:
        return
    keys = sorted({(b, c, t) for _, b, c, t in best})
    cols = ([k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys])
    await conn.execute(
        """
        INSERT INTO model_tags (backend, category, name)
        SELECT k.b, k.c, k.n
        FROM unnest($1::text[], $2::text[], $3::text[]) AS k(b, c, n)
        WHERE NOT EXISTS (SELECT 1 FROM model_tags m
                          WHERE m.backend = k.b AND m.category = k.c
                            AND m.name = k.n)
        ON CONFLICT DO NOTHING
        """, *cols)
    tag_id = {(r["backend"], r["category"], r["name"]): r["id"]
              for r in await conn.fetch(
                  """
                  SELECT m.id, m.backend, m.category, m.name
                  FROM model_tags m
                  JOIN unnest($1::text[], $2::text[], $3::text[])
                       AS k(b, c, n)
                    ON m.backend = k.b AND m.category = k.c AND m.name = k.n
                  """, *cols)}

    grouped: dict[tuple[int, int], tuple[list, list]] = {}
    for (sid, backend, category, tag), score in best.items():
        ids, scores = grouped.setdefault((sid, versions[backend]), ([], []))
        ids.append(tag_id[(backend, category, tag)])
        scores.append(score)
    await conn.copy_records_to_table(
        "sample_scores",
        records=[(sid, vid, ids, scores)
                 for (sid, vid), (ids, scores) in grouped.items()],
        columns=["sample_id", "model_version_id", "tag_ids", "scores"],
    )


async def _record_measurements(
    conn, run_id: int, work_id: int, obs: WorkObservation,
    sample_ids: dict[int, int],
) -> None:
    await conn.execute(
        "DELETE FROM measurements WHERE work_id = $1 AND run_id = $2 "
        "AND sample_id IS NULL", work_id, run_id)
    if not obs.measurements:
        return
    await conn.executemany(
        "INSERT INTO measurements (run_id, work_id, sample_id, kind, value) "
        "VALUES ($1, $2, $3, $4, $5)",
        [
            (run_id, work_id,
             sample_ids.get(m.sample_ordinal) if m.sample_ordinal is not None
             else None,
             m.kind, json.dumps(m.value))
            for m in obs.measurements
        ],
    )


async def _record_embeddings(
    conn, run_id: int, work_id: int, obs: WorkObservation,
    sample_ids: dict[int, int], versions: dict[str, int],
) -> None:
    """One vector per analyzed sample per model version. The work's vector
    is their mean, taken by `derive.materialize_vectors`."""
    import numpy as np

    rows = []
    for sample, by_backend in zip(obs.analyzed_samples, obs.scores or ()):
        sid = sample_ids.get(sample.ordinal)
        if sid is None:
            continue
        for backend, seen in by_backend.items():
            if seen.embedding is not None:
                rows.append((run_id, work_id, sid, versions[backend],
                             np.asarray(seen.embedding, dtype=np.float32)))
    if rows:
        await conn.executemany(
            "INSERT INTO work_embeddings "
            "(run_id, work_id, sample_id, model_version_id, vec) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (work_id, model_version_id, run_id, sample_id) "
            "DO UPDATE SET vec = EXCLUDED.vec", rows)


async def record_error(
    conn, run_id: int, path: str, stage: str, message: str,
    work_id: int | None = None,
) -> None:
    """Note that a work could not be observed, and why."""
    await conn.execute(
        "INSERT INTO scan_errors (run_id, work_id, path, stage, message) "
        "VALUES ($1, $2, $3, $4, $5)",
        run_id, work_id, path, stage, message[:2000])


async def begin_run(conn, params: dict | None = None) -> int:
    """Open a scan run. Everything raw written afterwards cites its id."""
    return await conn.fetchval(
        "INSERT INTO scan_runs (params) VALUES ($1) RETURNING id",
        json.dumps(params or {}))


async def finish_run(conn, run_id: int) -> None:
    await conn.execute(
        "UPDATE scan_runs SET finished_at = NOW() WHERE id = $1", run_id)
