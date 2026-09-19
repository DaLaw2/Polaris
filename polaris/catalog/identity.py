"""Which work a path holds, and every place a work is.

A work is its content: a path repeating content already on disk
elsewhere is a copy, and a path whose content changed is the same
work changed in place.
"""

from __future__ import annotations

from pathlib import Path

from polaris.derivation import derive


async def upsert(conn, content_key: str, path: str
                 ) -> tuple[int, list[int], bool]:
    """Get or create the work's id. Returns it, the works whose path this
    took over, and whether an existing work's content changed.

    The content decides first: a path holding content a work already has
    is that work, wherever it used to be, and its old location is retired.
    Then the place: a path recorded as a work's, now holding content nobody
    has, is that work changed in place. Otherwise it is a new work.
    """
    await conn.execute("DELETE FROM work_copies WHERE path = $1", path)

    work_id = await conn.fetchval(
        "SELECT id FROM works WHERE content_key = $1", content_key)
    if work_id is not None:
        displaced = [r["work_id"] for r in await conn.fetch(
            "UPDATE work_paths SET present = FALSE WHERE present AND ("
            "(work_id = $1 AND path <> $2) OR (work_id <> $1 AND path = $2)) "
            "RETURNING work_id",
            work_id, path)]
        return work_id, displaced, False

    work_id = await conn.fetchval(
        "SELECT work_id FROM work_paths WHERE path = $1 AND present", path)
    if work_id is not None:
        await conn.execute(
            "UPDATE works SET content_key = $2 WHERE id = $1",
            work_id, content_key)
        return work_id, [], True

    return await conn.fetchval(
        "INSERT INTO works (content_key) VALUES ($1) RETURNING id",
        content_key), [], False


async def record_path(conn, work_id: int, path: str, collection: str) -> int:
    """Note that the work was seen here, now."""
    return await conn.fetchval(
        """
        INSERT INTO work_paths (work_id, path, collection)
        VALUES ($1, $2, $3)
        ON CONFLICT (work_id, path) DO UPDATE
            SET last_seen = NOW(), present = TRUE,
                collection = EXCLUDED.collection
        RETURNING id
        """,
        work_id, path, collection,
    )


async def copy_of(conn, content_key: str, path: str) -> int | None:
    """The work whose content this path repeats, while that work is still
    on disk at its own path.

    Checked against the disk rather than `present`: a path someone
    deleted stays present until a scan notices, and in that window a
    rename would read as a copy of itself.
    """
    row = await conn.fetchrow(
        "SELECT w.id, p.path FROM works w "
        "JOIN work_paths p ON p.work_id = w.id AND p.present "
        "WHERE w.content_key = $1", content_key)
    if row is None or row["path"] == path or not Path(row["path"]).exists():
        return None
    return row["id"]


async def record_copy(conn, work_id: int, path: str,
                      collection: str) -> list[int]:
    """Note that this path holds another path's content.

    If the path was itself some work's location, that work's content has
    left it, so it stops being that work's.
    """
    displaced = [r["work_id"] for r in await conn.fetch(
        "UPDATE work_paths SET present = FALSE WHERE path = $1 AND present "
        "RETURNING work_id", path)]
    await conn.execute(
        "INSERT INTO work_copies (work_id, path, collection) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (path) DO UPDATE SET work_id = EXCLUDED.work_id, "
        "collection = EXCLUDED.collection",
        work_id, path, collection)
    return displaced


async def discard_place(conn, work_id: int, path: str, heir: str) -> None:
    """Forget a place of a work whose file a person has just deleted.

    A copy's row goes. The work's own path is retired and `heir`, one of
    its copies still on disk, takes over now. Samples, scores and claims
    hang off the work, not the path, so nothing else moves.
    """
    async with conn.transaction():
        if await conn.fetchval(
                "DELETE FROM work_copies WHERE work_id = $1 AND path = $2 "
                "RETURNING id", work_id, path):
            return
        await conn.execute(
            "UPDATE work_paths SET present = FALSE "
            "WHERE work_id = $1 AND path = $2 AND present", work_id, path)
        collection = await conn.fetchval(
            "DELETE FROM work_copies WHERE work_id = $1 AND path = $2 "
            "RETURNING collection", work_id, heir)
        if collection is None:
            raise ValueError(f"{heir} is not a copy of work {work_id}")
        await conn.execute(
            "INSERT INTO work_paths (work_id, path, collection) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (work_id, path) DO UPDATE SET present = TRUE, "
            "last_seen = NOW(), collection = EXCLUDED.collection",
            work_id, heir, collection)
        await derive.derive_works(conn, [work_id])


async def mark_absent(conn, paths: list[str]) -> int:
    """Retire registered paths that are no longer on disk, and forget copies
    that are gone. Returns how many work paths were retired."""
    absent = await conn.fetch(
        "UPDATE work_paths SET present = FALSE "
        "WHERE present AND path = ANY($1::text[]) RETURNING work_id", paths)
    await conn.execute(
        "DELETE FROM work_copies WHERE path = ANY($1::text[])", paths)
    await derive.derive_works(conn, [r["work_id"] for r in absent])
    return len(absent)
