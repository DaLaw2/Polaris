"""Collection endpoints: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_collection_endpoints.py"""
import asyncio
import sys
import tempfile
from pathlib import Path

from _common import dsn, refused

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.catalog import api as catalog_api  # noqa: E402

A, B, C = "endpoint-test-a", "endpoint-test-b", "endpoint-test-c"
KEYS = ["collection-endpoint-test:1", "collection-endpoint-test:2",
        "collection-endpoint-test:3"]


async def clean(conn) -> None:
    await conn.execute("DELETE FROM works WHERE content_key = ANY($1::text[])", KEYS)
    await conn.execute("DELETE FROM collections WHERE name = ANY($1::text[])", [A, B, C])


async def named(name):
    return next((r for r in await catalog_api.list_collections() if r["name"] == name),
                None)


async def q(sql: str, *args):
    async with state.store.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def work(key: str, table: str, path: Path, coll: str) -> int:
    wid = await q("INSERT INTO works (content_key) VALUES ($1) RETURNING id", key)
    await q(f"INSERT INTO {table} (work_id, path, collection) VALUES ($1, $2, $3)",
            wid, str(path), coll)
    return wid


async def main(one: str, two: str) -> None:
    root = str(Path(one).resolve())
    made = await catalog_api.put_collection(A, catalog_api.CollectionRequest(root=one))
    row = await named(A)
    assert made["root"] == row["root"] == root and row["works"] == 0, row
    assert row["searchable"] is True and "note" not in row, row
    await catalog_api.put_collection(A, catalog_api.CollectionRequest(
        root=one, searchable=False))
    row = await named(A)
    assert row["searchable"] is False and row["ordinal"] == made["ordinal"], row
    for name, req, status, code in (
            (A, catalog_api.CollectionRequest(root=two), 409, "collection_root_change"),
            (A, catalog_api.CollectionRequest(root=str(Path(two) / "nope")), 400,
             "not_a_directory")):
        await refused(catalog_api.put_collection(name, req), status, code)
    assert (await named(A))["root"] == root
    created = await catalog_api.create_collection(catalog_api.CollectionCreate(name=B, root=one))
    assert created["ordinal"] > made["ordinal"], created
    await refused(catalog_api.create_collection(catalog_api.CollectionCreate(
        name=B, root=two)), 409, "collection_exists")
    assert (await named(B))["root"] == root
    print("  ok  a PUT restates flags in place but never moves a root; bad roots "
          "and a taken name are refused")

    wid = await work(KEYS[0], "work_paths", Path(one) / "a work", A)
    await q("INSERT INTO work_copies (work_id, path, collection) VALUES ($1, $2, $3)",
            wid, str(Path(two) / "a work"), B)
    await q("INSERT INTO derived.work_search (profile_id, id, folder_path, "
            "collection) VALUES (active_profile_id(), $1, $2, $3)",
            wid, str(Path(one) / "a work"), A)
    assert ((await named(A))["works"], (await named(B))["works"]) == (1, 0)
    for name in (A, B):
        await refused(catalog_api.delete_collection(name), 409, "collection_holds_works")
    print("  ok  a work counts where it lives, and holds both collections from deletion")

    await refused(catalog_api.edit_collection(A, catalog_api.CollectionEdit(name=B)),
                  409, "collection_exists")
    out = await catalog_api.edit_collection(A, catalog_api.CollectionEdit(
        name=C, ordinal=-5, searchable=True))
    assert (out["name"], out["ordinal"], out["searchable"]) == (C, -5, True), out
    assert await q("SELECT collection FROM work_paths WHERE work_id = $1", wid) == C
    assert await q("SELECT collection FROM derived.work_search WHERE id = $1", wid) == C
    assert await named(A) is None
    print("  ok  a rename carries every path and derived row; a taken name is refused")

    ours = Path(one)
    for d in ("group/b work", "a work", "new one"):
        (ours / d).mkdir(parents=True)
    (ours / "a clip.mp4").write_bytes(b"")
    (ours / "a copy.mp4").write_bytes(b"")
    inner = await work(KEYS[1], "work_paths", ours / "group" / "b work", C)
    await q("INSERT INTO work_copies (work_id, path, collection) VALUES ($1, $2, $3)",
            inner, str(ours / "a copy.mp4"), C)
    here = await catalog_api.browse(one)
    dirs = {d["name"]: d for d in here["dirs"]}
    files = {f["name"]: f for f in here["files"]}
    assert dirs["a work"]["work"] == {"id": wid, "collection": C, "copy": False}
    assert dirs["group"]["work"] is None and dirs["group"]["holds"] == 1
    assert dirs["new one"]["work"] is None and not dirs["new one"]["holds"]
    assert files["a copy.mp4"]["work"]["copy"] is True and files["a clip.mp4"]["work"] is None
    loose = await catalog_api.browse_unregistered(one)
    assert sorted((i["name"], i["kind"]) for i in loose["items"]) == [
        ("a clip.mp4", "video"), ("new one", "dir")], loose
    if sys.platform == "win32":
        upper = await catalog_api.browse(one.upper())
        assert {d["name"]: d for d in upper["dirs"]}["a work"]["work"]
    print("  ok  browsing names each entry's work and counts those below; the "
          "unregistered listing skips both")

    fresh = Path(two) / "moved"
    for d in ("a work", "group/b work"):
        (fresh / d).mkdir(parents=True)
    assert await catalog_api.check_relocation(C, str(fresh)) == {
        "name": C, "root": str(fresh.resolve()), "found": 2, "sampled": 3}
    blocker = await work(KEYS[2], "work_copies", fresh.resolve() / "a copy.mp4", B)
    await refused(catalog_api.relocate_collection(
        C, catalog_api.RelocateRequest(root=str(fresh))), 409, "relocate_conflict")
    assert await q("SELECT path FROM work_paths WHERE work_id = $1", wid) \
        == str(ours / "a work")
    assert (await named(C))["root"] == root
    await q("DELETE FROM works WHERE id = $1", blocker)
    print("  ok  a move that would land on a recorded path changes nothing")

    if sys.platform == "win32":
        await q("UPDATE work_paths SET path = $2 WHERE work_id = $1",
                wid, str(ours / "a work").upper())
    moved = await catalog_api.relocate_collection(C, catalog_api.RelocateRequest(root=str(fresh)))
    assert moved["moved"] == {"work_paths": 2, "work_copies": 1, "work_search": 1}, moved
    new = fresh.resolve()
    async with state.store.acquire() as conn:
        paths = {r["path"] for r in await conn.fetch(
            "SELECT path FROM work_paths WHERE collection = $1 "
            "UNION ALL SELECT path FROM work_copies WHERE collection = $1", C)}
    first = "A WORK" if sys.platform == "win32" else "a work"
    assert paths == {str(new / first), str(new / "group" / "b work"),
                     str(new / "a copy.mp4")}, paths
    assert await q("SELECT folder_path FROM derived.work_search WHERE id = $1",
                   wid) == str(new / "a work")
    assert await q("SELECT path FROM work_copies WHERE collection = $1", B) \
        == str(Path(two) / "a work")
    assert (await named(C))["root"] == str(new)
    print("  ok  a move rewrites root, paths, copies (any case) and the derived "
          "folder, and nothing of another collection")

    empty = await catalog_api.create_collection(catalog_api.CollectionCreate(name=A, root=one))
    gone = await catalog_api.relocate_collection(A, catalog_api.RelocateRequest(root=two))
    assert gone["moved"] == {"work_paths": 0, "work_copies": 0, "work_search": 0}, gone
    assert (await named(A))["root"] == str(Path(two).resolve()) != empty["root"]
    await refused(catalog_api.check_relocation("no-such", two), 404)
    await q("DELETE FROM works WHERE content_key = ANY($1::text[])", KEYS)
    for name in (A, B, C):
        assert (await catalog_api.delete_collection(name))["removed"] is True
        assert await named(name) is None
    await refused(catalog_api.delete_collection(A), 404, "collection_not_found")
    print("  ok  an empty collection just points elsewhere; unheld ones go and stay gone")


async def run() -> None:
    await state.store.connect()
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            await main(one, two)
    finally:
        async with state.store.acquire() as conn:
            await clean(conn)
        await state.store.close()
    print("all collection endpoint checks passed")


asyncio.run(run())
