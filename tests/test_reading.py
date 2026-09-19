"""Reading a work and the path guard: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_reading.py"""
import asyncio
import tempfile
from io import BytesIO
from pathlib import Path

from _common import dsn, png, refused

DSN = dsn()

from PIL import Image  # noqa: E402

from polaris import config, state  # noqa: E402
from polaris.catalog import api as catalog_api  # noqa: E402

COLL = "reading-test"


async def main(root: Path, outside: Path) -> None:
    work = root / "a work"
    (work / "extras").mkdir(parents=True)
    for i in range(12):
        (work / f"{i:03d}.png").write_bytes(png(400, 560))
    (work / "extras" / "z.png").write_bytes(png(400, 560))
    clip = root / "a clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512)
    (outside / "elsewhere").mkdir()

    await state.store.connect()
    try:
        async with state.store.acquire() as conn:
            await conn.execute("DELETE FROM collections WHERE name = $1", COLL)
            await conn.execute("INSERT INTO collections (name, root, searchable) "
                               "VALUES ($1, $2, FALSE)", COLL, str(root.resolve()))
            await config.load_collections(conn)

        info = await catalog_api.work_pages(str(work))
        assert info["kind"] == "images" and info["total"] == 13, info
        assert info["pages"][-1].endswith("z.png"), info["pages"][-1]
        assert await catalog_api.work_pages(str(clip)) == {
            "kind": "video", "total": 1, "pages": ["a clip.mp4"], "name": "a clip.mp4"}
        print("  ok  a folder is its pages, chapters included; a video is one thing")

        full = await catalog_api.work_page(str(work), i=0, w=None)
        assert full.media_type == "image/png", full.media_type
        assert full.headers["cache-control"] == "private, max-age=86400"
        small = await catalog_api.work_page(str(work), i=0, w=120)
        assert small.media_type == "image/jpeg"
        with Image.open(BytesIO(small.body)) as im:
            assert im.width <= 120 and im.height == 168, im.size
        await refused(catalog_api.work_page(str(work), i=99, w=None), 404)
        played = await catalog_api.work_page(str(clip), i=0, w=None)
        assert played.media_type == "video/mp4" and Path(played.path) == clip
        print("  ok  a page comes back cached for a day, a thumbnail scaled, past "
              "the end 404, a video as a file")

        away = str(outside / "elsewhere")
        for call in (catalog_api.work_pages(away),
                     catalog_api.work_page(away, i=0, w=None),
                     catalog_api.open_on_host(catalog_api.OpenRequest(path=away)),
                     *(catalog_api.cover(path=str(p), w=None) for p in (
                         outside / "elsewhere", root.parent,
                         root / ".." / "elsewhere", Path(root.anchor)))):
            await refused(call, 403)
        cover = await catalog_api.cover(path=str(work), w=None)
        assert cover.media_type == "image/png", cover
        print("  ok  pages, open and cover refuse a sibling, the parent, a path "
              "climbing out and the drive root; under a root gets through")

        top = await catalog_api.browse(None)
        assert top["dirs"] and top["parent"] is None, top
        here = await catalog_api.browse(str(root))
        names = {d["name"]: d for d in here["dirs"]}
        assert set(names) == {"a work"}, names
        assert (names["a work"]["images"], names["a work"]["subdirs"]) == (12, 1)
        assert here["parent"] == str(root.parent)
        assert here["files"] == [{"name": "a clip.mp4", "path": str(clip), "work": None}]
        print(f"  ok  browsing shows folders, videos and what is in them, from "
              f"{len(top['dirs'])} drives down")
    finally:
        async with state.store.acquire() as conn:
            await conn.execute("DELETE FROM collections WHERE name = $1", COLL)
        await state.store.close()


with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory() as away:
    asyncio.run(main(Path(inside), Path(away)))
print("all reading checks passed")
