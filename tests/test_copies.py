"""Copies, absence and discard: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_copies.py"""
import asyncio
import shutil
import tempfile
from pathlib import Path

from _common import dsn, refused

DSN = dsn()

import asyncpg  # noqa: E402

from polaris import config, state  # noqa: E402
from polaris.catalog import api as catalog_api  # noqa: E402
from polaris.catalog import identity, trash  # noqa: E402
from polaris.observation import ingest  # noqa: E402
from polaris.observation.domain import Sample, WorkObservation  # noqa: E402
from polaris.search import api as search_api  # noqa: E402
from polaris.search.engine import SearchEngine  # noqa: E402

COLL, GONE = "copies-test", "absence-test"
ROOT = "/absence-test"
FOLDER, CLIP, COPY = (f"{ROOT}/{n}" for n in ("pages", "a clip.mp4", "a copy"))


def seen(path: Path, key: str) -> WorkObservation:
    path.mkdir(exist_ok=True)
    return WorkObservation(path=str(path), name=path.name, collection=COLL,
                           content_key=key, samples=[Sample("image", "001.png", 0, 0)],
                           scores=[{}])


async def where(conn, work_id: int) -> tuple[list[str], list[str]]:
    rows = await conn.fetch(
        "SELECT path, FALSE AS copy FROM work_paths WHERE work_id = $1 AND present "
        "UNION ALL SELECT path, TRUE FROM work_copies WHERE work_id = $1 "
        "ORDER BY path", work_id)
    return ([r["path"] for r in rows if not r["copy"]],
            [r["path"] for r in rows if r["copy"]])


async def cleanup(conn) -> None:
    await conn.execute(
        "DELETE FROM works WHERE id IN (SELECT work_id FROM work_paths WHERE "
        "collection = ANY($1::text[]) UNION SELECT work_id FROM work_copies "
        "WHERE collection = ANY($1::text[]))", [COLL, GONE])
    await conn.execute("DELETE FROM collections WHERE name = ANY($1::text[])", [COLL, GONE])
    await conn.execute("DELETE FROM scan_runs WHERE params->>'test' = 'test_copies'")


async def absence_fixture(conn) -> None:
    await cleanup(conn)
    await conn.execute("INSERT INTO collections (name, root) VALUES ($1, $2)", GONE, ROOT)
    run = await ingest.begin_run(conn, params={"test": "test_copies"})
    for path, medium in ((FOLDER, "image"), (CLIP, "video")):
        wid = await conn.fetchval("INSERT INTO works (content_key) VALUES ($1) "
                                  "RETURNING id", "absence:" + path)
        await conn.execute("INSERT INTO work_paths (work_id, path, collection, "
                           "present) VALUES ($1, $2, $3, TRUE)", wid, path, GONE)
        await conn.execute(
            "INSERT INTO work_samples (run_id, work_id, medium, source_relpath, "
            "position, ordinal) VALUES ($1, $2, $3, 'x', 0, 0)", run, wid, medium)
        if medium == "image":
            await conn.execute("INSERT INTO work_copies (work_id, path, collection) "
                               "VALUES ($1, $2, $3)", wid, COPY, GONE)


async def test_absence(conn) -> None:
    present = "SELECT present FROM work_paths WHERE path = $1"
    copies = "SELECT count(*) FROM work_copies WHERE collection = $1"
    for listed, n, retired, kept, left in ((FOLDER, 1, FOLDER, CLIP, 1),
                                           (CLIP, 1, CLIP, FOLDER, 1),
                                           (COPY, 0, None, FOLDER, 0)):
        await absence_fixture(conn)
        assert await identity.mark_absent(conn, [listed]) == n
        assert retired is None or not await conn.fetchval(present, retired)
        assert await conn.fetchval(present, kept), "an unlisted path retired"
        assert await conn.fetchval(copies, GONE) == left
    assert [await identity.mark_absent(conn, x) for x in ([FOLDER], [FOLDER], [])] == [1, 0, 0]
    print("  ok  only listed paths retire, video alike; a gone copy is forgotten")


async def test_identity(conn, run, tmp, k) -> None:
    a, b, c, d, e, f, g, h, i = (tmp / n for n in "abcdefghi")
    w = await ingest.record_observation(conn, run, seen(a, k + "1"))
    later = await ingest.begin_run(conn, params={"test": "test_copies"})
    assert await ingest.record_observation(conn, later, seen(b, k + "1")) == w
    assert await where(conn, w) == ([str(a)], [str(b)])
    assert await conn.fetchval(
        "SELECT count(*) FROM work_samples WHERE run_id = $1", later) == 0
    a.rmdir()
    assert await ingest.record_observation(conn, run, seen(b, k + "1")) == w
    assert await where(conn, w) == ([str(b)], [])
    print("  ok  a second path is a copy, writes nothing, takes over later")

    w = await ingest.record_observation(conn, run, seen(c, k + "2"))
    c.rmdir()
    assert await ingest.record_observation(conn, run, seen(d, k + "2")) == w
    assert await where(conn, w) == ([str(d)], [])
    assert await ingest.record_observation(conn, run, seen(d, k + "3")) == w
    assert await conn.fetchval("SELECT content_key FROM works WHERE id = $1", w) == k + "3"
    print("  ok  a rename is the same work; new content at its path is it edited")

    we = await ingest.record_observation(conn, run, seen(e, k + "4"))
    wf = await ingest.record_observation(conn, run, seen(f, k + "5"))
    assert await ingest.record_observation(conn, run, seen(f, k + "4")) == we
    assert await where(conn, we) == ([str(e)], [str(f)])
    assert await where(conn, wf) == ([], [])
    w = await ingest.record_observation(conn, run, seen(g, k + "6"))
    for p in (h, i):
        await ingest.record_observation(conn, run, seen(p, k + "6"))
    assert await identity.mark_absent(conn, [str(i)]) == 0
    assert await where(conn, w) == ([str(g)], [str(h)])
    print("  ok  another work's content makes a copy; a copy gone is dropped")

    try:
        await ingest.record_observation(conn, run, seen(tmp / "j", None))
        raise AssertionError("a work was recorded with no content key")
    except ValueError:
        pass
    w = await conn.fetchval("SELECT id FROM works WHERE content_key = $1", k + "1")
    for sql, args, err in (
            ("INSERT INTO works (content_key) VALUES (NULL)", (),
             asyncpg.NotNullViolationError),
            ("INSERT INTO works (content_key) VALUES ($1)", (k + "1",),
             asyncpg.UniqueViolationError),
            ("INSERT INTO work_paths (work_id, path, collection, present) "
             "VALUES ($1, $2, $3, TRUE)", (w, str(tmp / "k"), COLL),
             asyncpg.UniqueViolationError)):
        try:
            await conn.execute(sql, *args)
            raise AssertionError(f"accepted: {sql}")
        except err:
            pass
    print("  ok  no key, a shared key, and a second live path are all refused")


async def test_discard_and_search(conn, run, tmp, k) -> None:
    m, n, o = tmp / "m", tmp / "n", tmp / "o"
    w = await ingest.record_observation(conn, run, seen(m, k + "8"))
    await ingest.record_observation(conn, run, seen(n, k + "8"))
    await conn.execute("INSERT INTO work_copies (work_id, path, collection) "
                       "VALUES ($1, $2, $3)", w, str(tmp / "M"), COLL)
    await catalog_api.discard_copy(catalog_api.DiscardRequest(path=str(n)))
    assert not n.exists() and await where(conn, w) == ([str(m)], [str(tmp / "M")])
    await refused(catalog_api.discard_copy(catalog_api.DiscardRequest(path=str(m))), 409)
    assert m.exists()
    await ingest.record_observation(conn, run, seen(o, k + "8"))
    await catalog_api.discard_copy(catalog_api.DiscardRequest(path=str(m)))
    assert not m.exists() and (await where(conn, w))[0] == [str(o)]
    print("  ok  a copy goes; the last one is refused; a copy takes over the work")

    held = {r["path"] for r in await conn.fetch(
        "SELECT p.path FROM work_paths p WHERE p.present AND p.collection = $1 "
        "AND EXISTS (SELECT 1 FROM work_copies c WHERE c.work_id = p.work_id)",
        COLL)}
    payload = await SearchEngine(state.store).search_faceted(
        copies=True, collection=COLL, limit=200)
    found = {r.work.folder_path for r in payload["results"]}
    assert held and found == held and payload["total"] == len(held), (found, held)
    counts = {f["value"]: f["count"] for f in payload["facets"]["collection"]}
    assert counts.get(COLL) == len(held), counts
    places = await search_api._places(sorted(held))
    assert set(places) == held and all(
        row[0]["path"] == path and not row[0]["copy"] and any(p["copy"] for p in row[1:])
        for path, row in places.items())
    print(f"  ok  copies=True finds the {len(held)} works with copies, with places")


def test_trash_refuses(tmp) -> None:
    (f := tmp / "big").write_bytes(b"0" * 100)
    real = trash._limit
    for limit in ((False, 10), (True, 1 << 40)):
        trash._limit = lambda path, limit=limit: limit
        try:
            trash.recycle(f, 100)
            raise AssertionError(f"recycled under {limit}")
        except trash.Refused:
            assert f.exists()
        finally:
            trash._limit = real
    print("  ok  too big for the bin, or a bin switched off: refused, kept")


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    await state.store.connect()
    real = trash.recycle
    try:
        await test_absence(conn)
        with tempfile.TemporaryDirectory() as t:
            tmp, k = Path(t), Path(t).name + ":"
            test_trash_refuses(tmp)
            trash.recycle = lambda path, size: shutil.rmtree(path)
            await cleanup(conn)
            await conn.execute("INSERT INTO collections (name, root) VALUES ($1, $2)",
                               COLL, str(tmp))
            await config.load_collections(conn)
            run = await ingest.begin_run(conn, params={"test": "test_copies"})
            await test_identity(conn, run, tmp, k)
            await test_discard_and_search(conn, run, tmp, k)
    finally:
        trash.recycle = real
        await cleanup(conn)
        await state.store.close()
        await conn.close()
    print("all copy checks passed")


asyncio.run(main())
