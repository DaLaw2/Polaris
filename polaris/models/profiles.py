"""Model versions as loaded, and the profile that decides what they mean."""

from __future__ import annotations

ROLES = ("general", "character", "copyright", "artist", "rating", "embedding")


async def sync_thresholds(conn, backends,
                          versions: dict[str, int]) -> tuple[dict[str, int], bool]:
    """Copy what each backend publishes about its own tags into the database,
    under the model version it was loaded as.

    Returns how many rows each backend supplied, and whether any stored
    row changed.
    """
    written: dict[str, int] = {}
    changed = False
    for backend in backends:
        publish = getattr(backend, "published_thresholds", None)
        version = versions.get(backend.name)
        if publish is None or version is None:
            continue
        rows = publish()
        if not rows:
            continue
        cols = ([r.category for r in rows], [r.tag for r in rows],
                [r.threshold for r in rows], [r.f1 for r in rows])
        async with conn.transaction():
            differs = await conn.fetchval(
                """
                WITH new AS (
                    SELECT * FROM unnest($2::text[], $3::text[],
                                         $4::numeric[], $5::real[])
                        AS n(category, tag, threshold, f1)),
                old AS (
                    SELECT category, tag, threshold, f1
                    FROM derived.tag_thresholds WHERE model_version_id = $1)
                SELECT EXISTS (SELECT * FROM new EXCEPT SELECT * FROM old)
                    OR EXISTS (SELECT * FROM old EXCEPT SELECT * FROM new)
                """, version, *cols)
            if differs:
                await conn.execute(
                    "DELETE FROM derived.tag_thresholds WHERE model_version_id = $1",
                    version)
                await conn.execute(
                    "INSERT INTO derived.tag_thresholds "
                    "  (model_version_id, category, tag, threshold, f1, source) "
                    "SELECT $1, n.*, 'shipped:' || $6 "
                    "FROM unnest($2::text[], $3::text[], $4::numeric[], "
                    "            $5::real[]) AS n",
                    version, *cols, backend.name)
        changed = changed or differs
        written[backend.name] = len(rows)
    return written, changed


async def active_profile(conn) -> int:
    return await conn.fetchval("SELECT active_profile_id()")


async def embedding_version(conn) -> int | None:
    """The model version whose work vectors the active profile searches."""
    return await conn.fetchval(
        "SELECT model_version_id FROM model_profile_roles "
        "WHERE profile_id = active_profile_id() AND role = 'embedding' "
        "ORDER BY model_version_id LIMIT 1")


def snapshot_revision(model_path: str | None) -> str | None:
    """The Hugging Face cache snapshot a model file sits in; None for a
    path outside that cache."""
    from pathlib import Path

    parts = Path(model_path).parts if model_path else ()
    return (parts[parts.index("snapshots") + 1]
            if "snapshots" in parts[:-1] else None)


async def model_version(conn, backend: str,
                        model_path: str | None = None) -> int:
    """The id of a backend at the revision its weights were loaded from.

    The revision is the Hugging Face cache snapshot the file sits in; a
    path outside that cache records none. The scan loads every backend at
    FP16, so this only resolves FP16 rows.

    A row with no revision is a placeholder someone added before the
    weights were ever loaded, so the first load that resolves one claims
    it rather than leaving the profile pointing at a second row.
    """
    revision = snapshot_revision(model_path)
    if revision is not None:
        claimed = await conn.fetchval(
            "UPDATE model_versions SET revision = $2 WHERE id = ("
            "  SELECT id FROM model_versions"
            "  WHERE backend = $1 AND revision IS NULL AND precision = 'fp16'"
            "    AND NOT EXISTS (SELECT 1 FROM model_versions o"
            "                    WHERE o.backend = $1 AND o.revision = $2"
            "                      AND o.precision = 'fp16'))"
            " RETURNING id", backend, revision)
        if claimed is not None:
            return claimed
    await conn.execute(
        "INSERT INTO model_versions (backend, revision, precision, "
        "                            definition_id) "
        "SELECT $1, $2, 'fp16', (SELECT max(id) FROM model_definitions "
        "                        WHERE name = $1 AND retracted_at IS NULL) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM model_versions WHERE backend = $1 "
        "  AND revision IS NOT DISTINCT FROM $2 AND precision = 'fp16') "
        "ON CONFLICT DO NOTHING", backend, revision)
    return await conn.fetchval(
        "SELECT id FROM model_versions WHERE backend = $1 "
        "AND revision IS NOT DISTINCT FROM $2 AND precision = 'fp16'",
        backend, revision)
