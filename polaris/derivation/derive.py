"""Recomputing what works are, from what was seen and what was said."""

from __future__ import annotations


async def materialize_vectors(conn, work_ids,
                              versions: list[int] | None = None) -> int:
    """Rebuild each work's vector per model version: the normalised mean of
    that version's newest run of page vectors."""
    ids = sorted({int(i) for i in work_ids if i is not None})
    if not ids:
        return 0
    await conn.execute(
        "DELETE FROM derived.work_vectors WHERE work_id = ANY($1::int[]) "
        "AND ($2::smallint[] IS NULL OR model_version_id = ANY($2::smallint[]))",
        ids, versions)
    return int((await conn.execute(
        """
        INSERT INTO derived.work_vectors (model_version_id, work_id, vec)
        SELECT e.model_version_id, e.work_id, l2_normalize(avg(e.vec))
        FROM work_embeddings e
        JOIN (SELECT work_id, model_version_id, MAX(run_id) AS run_id
              FROM work_embeddings
              WHERE work_id = ANY($1::int[])
                AND ($2::smallint[] IS NULL
                     OR model_version_id = ANY($2::smallint[]))
              GROUP BY work_id, model_version_id) l
          ON l.work_id = e.work_id AND l.model_version_id = e.model_version_id
         AND l.run_id = e.run_id
        GROUP BY e.model_version_id, e.work_id
        """, ids, versions)).split()[-1])


async def derive_works(conn, work_ids) -> None:
    """Recompute every derived row of these works for every maintained
    profile, inside the caller's transaction when there is one."""
    ids = sorted({int(i) for i in work_ids if i is not None})
    if ids:
        await conn.execute("SELECT derive_works($1::int[])", ids)


async def refresh_derived(conn, profile: int | None = None, batch: int = 200,
                          on_batch=None, work_ids=None) -> int:
    """Recompute the derived rows of every work, or of `work_ids`, one batch
    per transaction, for one profile or every maintained one.

    Returns how many works were recomputed.
    """
    ids = [r["id"] for r in await conn.fetch(
        "SELECT id FROM works WHERE $1::int[] IS NULL OR id = ANY($1::int[]) "
        "ORDER BY id", None if work_ids is None else list(work_ids))]
    if on_batch is not None:
        await on_batch(0, len(ids))
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        async with conn.transaction():
            if profile is None:
                await derive_works(conn, chunk)
            else:
                await conn.execute(
                    "SELECT derive_profile($1::smallint, $2::int[])",
                    profile, chunk)
        if on_batch is not None:
            await on_batch(min(i + batch, len(ids)), len(ids))
    return len(ids)


async def works_naming(conn, terms) -> list[int]:
    """Works any profile tagged with these strings, or a person claimed
    a concept spelled this way."""
    wanted = sorted({t.lower() for t in terms if t})
    if not wanted:
        return []
    rows = await conn.fetch(
        """
        SELECT DISTINCT work_id FROM derived.work_tags
        WHERE lower(tag) = ANY($1::text[])
        UNION
        SELECT c.work_id FROM work_claims c
        JOIN concepts co ON co.id = c.concept_id
        WHERE lower(co.slug) = ANY($1::text[])
           OR EXISTS (SELECT 1 FROM term_map tm
                      WHERE tm.concept_id = co.id
                        AND lower(tm.term) = ANY($1::text[]))
        """, wanted)
    return [r["work_id"] for r in rows]
