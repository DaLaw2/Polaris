"""HTTP endpoints for collections, copies and reading a work."""

import asyncio
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from polaris import config, state
from polaris.catalog import identity, trash
from polaris.shared.errors import refuse

router = APIRouter()


class CollectionRequest(BaseModel):
    """Where a root is and how it should be treated. `name` is the path."""

    root: str
    searchable: bool = True
    ordinal: int | None = None


class CollectionCreate(CollectionRequest):
    """A new collection, named in the body."""

    name: str


class CollectionEdit(BaseModel):
    """What can change about a collection besides where it is."""

    name: str | None = None
    searchable: bool | None = None
    ordinal: int | None = None


class RelocateRequest(BaseModel):
    """Where a collection's folder is now."""

    root: str


FOLD = sys.platform == "win32"


def _same(a: str, b: str) -> str:
    """SQL comparing two paths the way this filesystem does."""
    return f"lower({a}) = lower({b})" if FOLD else f"{a} = {b}"


def _prefix(root: str) -> str:
    """What every path strictly under `root` starts with."""
    return root.rstrip("\\/") + os.sep


def _under(column: str) -> str:
    """SQL: `column` is the root in $2 or lies under its prefix in $3."""
    return (f"({_same(column, '$2')} "
            f"OR {_same(f'left({column}, length($3))', '$3')})")


def _directory(path: str) -> Path:
    """The folder a person named, resolved, or a refusal."""
    root = Path(path).expanduser()
    if not root.is_dir():
        raise refuse(400, "not_a_directory", f"not a directory: {root}",
                     path=str(root))
    return root.resolve()


async def _root_of(conn, name: str) -> str:
    root = await conn.fetchval(
        "SELECT root FROM collections WHERE name = $1", name)
    if root is None:
        raise refuse(404, "collection_not_found", "no such collection",
                     name=name)
    return root


@router.get("/api/collections")
async def list_collections():
    """Configured collections, with how many works each holds.

    The UI needs the counts to decide whether to show the selector at
    all: an installation with one collection should not grow a control
    that can only be set one way. It needs `root` to show where a
    collection points, which is the whole of `collection list`.

    Counted over `work_paths` rather than `work_identity`: the identity
    view picks one path per work, so a work present in two collections is
    counted for one of them and missing from the other.
    """
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.name, c.root, c.searchable, c.ordinal, "
            "       (SELECT COUNT(DISTINCT p.work_id) FROM work_paths p "
            "        WHERE p.collection = c.name AND p.present) AS works "
            "FROM collections c ORDER BY c.ordinal, c.name")
    lead = config.searchable_collections() or []
    return [
        {
            "name": r["name"],
            "root": r["root"],
            "works": r["works"],
            "searchable": r["searchable"],
            "ordinal": r["ordinal"],
            "default": r["name"] in lead,
        }
        for r in rows
    ]


@router.post("/api/collections")
async def create_collection(req: CollectionCreate):
    """Register a new root. Never touches a collection that already exists."""
    name = req.name.strip()
    if not name:
        raise refuse(400, "name_required", "name the collection")
    root = _directory(req.root)
    async with state.store.acquire() as conn:
        ordinal = await conn.fetchval(
            "INSERT INTO collections (name, root, searchable, ordinal) "
            "VALUES ($1, $2, $3, COALESCE($4, "
            "        (SELECT MAX(ordinal) + 1 FROM collections), 0)) "
            "ON CONFLICT (name) DO NOTHING RETURNING ordinal",
            name, str(root), req.searchable, req.ordinal)
        if ordinal is None:
            raise refuse(409, "collection_exists",
                         f"a collection named {name!r} exists", name=name)
        await config.load_collections(conn)
    return {"name": name, "root": str(root), "ordinal": ordinal}


@router.put("/api/collections/{name}")
async def put_collection(name: str, req: CollectionRequest):
    """Register a root, or restate an existing one's flags.

    An existing collection keeps its root: pointing it elsewhere is a
    relocation, which rewrites its paths, and a PUT that silently did
    half of that orphaned every work in it.
    """
    root = _directory(req.root)
    async with state.store.acquire() as conn:
        was = await conn.fetchval(
            "SELECT root FROM collections WHERE name = $1", name)
        if was is not None and (os.path.normcase(was)
                                != os.path.normcase(str(root))):
            raise refuse(
                409, "collection_root_change",
                f"{name} already points at {was}; relocate it instead",
                name=name, root=was)
        ordinal = req.ordinal
        if ordinal is None:
            ordinal = await conn.fetchval(
                "SELECT COALESCE((SELECT ordinal FROM collections "
                "                 WHERE name = $1), "
                "                (SELECT MAX(ordinal) + 1 FROM collections), "
                "                0)", name)
        await conn.execute(
            """
            INSERT INTO collections (name, root, searchable, ordinal)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (name) DO UPDATE SET
                searchable = EXCLUDED.searchable, ordinal = EXCLUDED.ordinal
            """,
            name, was or str(root), req.searchable, ordinal)
        await config.load_collections(conn)
    return {"name": name, "root": was or str(root), "ordinal": ordinal}


@router.patch("/api/collections/{name}")
async def edit_collection(name: str, req: CollectionEdit):
    """Rename a collection, flag it searchable or not, or move it in order.

    A rename is one UPDATE: the foreign keys cascade it to every path and
    copy. The name copied into derived rows and job history follows in the
    same transaction.
    """
    to = name if req.name is None else req.name.strip()
    if not to:
        raise refuse(400, "name_required", "name the collection")
    async with state.store.acquire() as conn:
        async with conn.transaction():
            await _root_of(conn, name)
            if to != name and await conn.fetchval(
                    "SELECT 1 FROM collections WHERE name = $1", to):
                raise refuse(409, "collection_exists",
                             f"a collection named {to!r} exists", name=to)
            row = await conn.fetchrow(
                "UPDATE collections SET name = $2, "
                "    searchable = COALESCE($3, searchable), "
                "    ordinal = COALESCE($4, ordinal) "
                "WHERE name = $1 RETURNING name, root, searchable, ordinal",
                name, to, req.searchable, req.ordinal)
            if to != name:
                for table in ("derived.work_search", "scan_jobs"):
                    await conn.execute(
                        f"UPDATE {table} SET collection = $2 "
                        f"WHERE collection = $1", name, to)
        await config.load_collections(conn)
    return dict(row)


async def _relocation(conn, name: str, path: str) -> tuple[str, Path]:
    """The collection's current root and the folder it is to point at."""
    return await _root_of(conn, name), await asyncio.to_thread(
        _directory, path)


@router.get("/api/collections/{name}/relocate")
async def check_relocation(name: str, root: str):
    """Before moving a collection: of up to 20 of its registered places,
    how many are where the new root says they would be."""
    async with state.store.acquire() as conn:
        old, new = await _relocation(conn, name, root)
        rows = await conn.fetch(
            f"""
            SELECT path FROM (
                SELECT path FROM work_paths
                WHERE collection = $1 AND present AND {_under('path')}
                UNION
                SELECT path FROM work_copies
                WHERE collection = $1 AND {_under('path')}) p
            ORDER BY random() LIMIT 20
            """, name, old, _prefix(old))
    rels = [r["path"][len(_prefix(old)):] for r in rows]
    found = await asyncio.to_thread(
        lambda: sum((new / rel).exists() for rel in rels))
    return {"name": name, "root": str(new), "found": found,
            "sampled": len(rels)}


@router.post("/api/collections/{name}/relocate")
async def relocate_collection(name: str, req: RelocateRequest):
    """Point a collection at its new folder, and every path in it too.

    One transaction: the root, the paths and copies under the old root,
    and the folder paths copied into derived rows. A path that would land
    on one already recorded refuses the whole move.
    """
    import asyncpg

    async with state.store.acquire() as conn:
        old, new = await _relocation(conn, name, req.root)
        moved = {}
        try:
            async with conn.transaction():
                for table, column in (("work_paths", "path"),
                                      ("work_copies", "path"),
                                      ("derived.work_search", "folder_path")):
                    tag = await conn.execute(
                        f"UPDATE {table} SET {column} = CASE "
                        f"WHEN {_same(column, '$2')} THEN $4 "
                        f"ELSE $5 || substr({column}, length($3) + 1) END "
                        f"WHERE collection = $1 AND {_under(column)}",
                        name, old, _prefix(old), str(new), _prefix(str(new)))
                    moved[table.split(".")[-1]] = int(tag.split()[-1])
                await conn.execute(
                    "UPDATE collections SET root = $2 WHERE name = $1",
                    name, str(new))
        except asyncpg.UniqueViolationError:
            raise refuse(409, "relocate_conflict",
                         f"a path under {new} is already recorded",
                         name=name, root=str(new)) from None
        await config.load_collections(conn)
    return {"name": name, "root": str(new), "moved": moved}


@router.delete("/api/collections/{name}")
async def delete_collection(name: str):
    """Forget a root. Refused while any path still names it.

    Guarded on `work_paths` and `work_copies`, not on `work_identity`:
    the identity view names one path per work, so a work with a copy in
    another collection counts zero there -- the guard would pass and the
    delete would leave rows naming a collection that no longer existed.
    Both tables have a foreign key, so the database would refuse anyway;
    this is the message that says why.
    """
    async with state.store.acquire() as conn:
        held = await conn.fetchval(
            "SELECT COUNT(DISTINCT work_id) FROM ("
            "  SELECT work_id FROM work_paths WHERE collection = $1"
            "  UNION ALL"
            "  SELECT work_id FROM work_copies WHERE collection = $1) h",
            name)
        if held:
            raise refuse(409, "collection_holds_works",
                         f"{name} still holds {held:,} works",
                         name=name, works=held)
        gone = await conn.execute(
            "DELETE FROM collections WHERE name = $1", name)
        if not gone.endswith("1"):
            raise refuse(404, "collection_not_found", "no such collection",
                         name=name)
        await config.load_collections(conn)
    return {"name": name, "removed": True}


def _size(path: Path) -> int | None:
    """Bytes on disk: the file, or every file under the folder. None if gone."""
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        pass
    return None


class DiscardRequest(BaseModel):
    """One place of a work, to send to the Recycle Bin."""

    path: str


@router.post("/api/copies/discard")
async def discard_copy(req: DiscardRequest):
    """Send one place of a work to the Recycle Bin, and record that it went.

    Refused unless another place of the same work is still on disk and is
    not this very file under another spelling: the last one is never
    deleted from here. Either may go -- when it is the work's own path, a
    copy takes over. The file goes first, and the database is written only
    once it has.
    """
    target = await _readable(req.path)
    async with state.store.acquire() as conn:
        work_id = await conn.fetchval(
            "SELECT work_id FROM work_copies WHERE path = $1 "
            "UNION ALL "
            "SELECT work_id FROM work_paths WHERE path = $1 AND present "
            "LIMIT 1", req.path)
        if work_id is None:
            raise refuse(404, "place_unknown",
                         "no work is recorded at this path", path=req.path)
        places = [r["path"] for r in await conn.fetch(
            "SELECT path FROM work_paths WHERE work_id = $1 AND present "
            "UNION ALL SELECT path FROM work_copies WHERE work_id = $1",
            work_id)]

        def others() -> list[str]:
            return [p for p in places
                    if p != req.path and Path(p).exists()
                    and not os.path.samefile(p, target)]

        kept = await asyncio.to_thread(others)
        if not kept:
            raise refuse(409, "last_copy",
                         "this is the last copy of the work on disk")
        try:
            await asyncio.to_thread(trash.recycle, target, _size(target) or 0)
        except trash.Refused as e:
            raise refuse(409, "recycle_refused", str(e))
        await identity.discard_place(conn, work_id, req.path, kept[0])
    return {"discarded": req.path, "kept": kept}


@router.get("/api/cover")
async def cover(path: str = Query(...),
                w: int | None = Query(None, ge=32, le=2048)):
    """The first page of a work, at `w` pixels wide if asked.

    A grid of results asks for the width it will draw at. The scans behind
    these covers run to several megabytes each, so a page of forty-eight
    of them at full size is a couple of hundred megabytes for thumbnails
    a fifth of a screen wide -- the size is the whole cost here, not the
    number of them.

    The path is a filesystem path from the caller, and what stands between
    it and every readable file on this machine is `_readable` -- the same
    guard the reader uses, rather than a second copy of it that has to be
    widened twice and can disagree once.
    """
    from fastapi.responses import FileResponse, Response

    from polaris.observation.media.sampler import collect_images

    folder = await _readable(path)

    if folder.is_file():
        still = await asyncio.to_thread(_video_still, folder, w or 480)
        if still is None:
            raise refuse(404, "no_frame",
                         "no frame could be read from this file")
        return Response(still, media_type="image/jpeg", headers=PAGE_CACHE)

    images = await asyncio.to_thread(collect_images, folder)
    if not images:
        return {"error": "no images"}

    kind = MEDIA_TYPES.get(images[0].suffix.lower(), "image/jpeg")
    if w and kind.startswith("image/"):
        data = await asyncio.to_thread(_thumbnail, images[0], w)
        return Response(data, media_type="image/jpeg", headers=PAGE_CACHE)
    return FileResponse(images[0], media_type=kind, headers=PAGE_CACHE)


MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".m4v": "video/x-m4v", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
}


PAGE_CACHE = {"Cache-Control": "private, max-age=86400"}


CHECK_ROOTS_TTL_S = 30.0


_check_roots: tuple[float, list[Path]] = (0.0, [])


def _forget_check_roots() -> None:
    """Read them again on the next request."""
    global _check_roots
    _check_roots = (0.0, [])


async def _check_roots_now() -> list[Path]:
    """The roots of the check jobs on record."""
    global _check_roots
    at, roots = _check_roots
    if roots and time.monotonic() - at < CHECK_ROOTS_TTL_S:
        return roots
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT jsonb_array_elements_text(params->'paths') AS path "
            "FROM scan_jobs WHERE kind = 'check'")
    found = []
    for r in rows:
        try:
            found.append(Path(r["path"]).resolve())
        except OSError:
            continue
    _check_roots = (time.monotonic(), found)
    return found


async def _readable(path: str) -> Path:
    """A path the caller named, once this installation admits to holding it."""
    target = Path(path)
    if config.collection_for(target) is None:
        here = target.resolve()
        if not any(here == root or root in here.parents
                   for root in await _check_roots_now()):
            raise refuse(403, "outside_collections",
                         "path is outside every configured collection root",
                         path=path)
    if not target.exists():
        raise refuse(404, "path_missing", f"{target} is not there",
                     path=path)
    return target


STILLS_DIR = config.MODELS_DIR / "stills"


def _video_still(file: Path, width: int) -> bytes | None:
    """A JPEG of the first frame of a video worth looking at, kept.

    Asks the sampler for three rather than one: the same blank-frame test
    a scan uses applies here, and a film that opens on black would
    otherwise have no cover at all. That costs 0.35s warm and near a
    second cold, which is per card in a grid -- so the answer is written
    down. A page of fifty is otherwise half a minute of decoding every
    time the browser cache expires.

    Keyed on the file's identity rather than its name, so a video edited
    in place is not served the previous one's frame.
    """
    import hashlib
    from io import BytesIO

    from PIL import Image

    from polaris.observation.media import video

    stat = file.stat()
    key = hashlib.sha1(
        f"{file.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{width}"
        .encode("utf-8", "replace")).hexdigest()
    cached = STILLS_DIR / f"{key}.jpg"
    try:
        return cached.read_bytes()
    except OSError:
        pass

    frames = video.sample_video_frames(file, n=3, deduplicate=False)
    if not frames:
        return None

    im = frames[0]
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)),
                       Image.LANCZOS)
    buf = BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=82, optimize=True)
    data = buf.getvalue()
    try:
        STILLS_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError:
        pass
    return data


def _thumbnail(file: Path, width: int) -> bytes:
    """A small JPEG of one page, for the grid that jumps between them.

    A 200-page work is 400 MB of scans. Sending those to fill a strip of
    120px thumbnails is the difference between a grid that opens and one
    that hangs the tab, so the resize happens here.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(file) as im:
        im.draft("RGB", (width * 2, width * 2))
        im = im.convert("RGB")
        im.thumbnail((width, width * 6), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=78)
        return buf.getvalue()


@router.get("/api/work/pages")
async def work_pages(path: str = Query(...)):
    """What a work is made of: pages to turn, or one file to play.

    `is_dir()` is how a registered path is told apart as an image work or a
    video one, so it is how this does too -- a second definition would
    disagree with the scanner about what a work is.
    """
    from polaris.observation.media.sampler import collect_images
    from polaris.observation.media.video import VIDEO_EXTENSIONS

    target = await _readable(path)
    if target.is_file():
        kind = "video" if target.suffix.lower() in VIDEO_EXTENSIONS else "image"
        return {"kind": kind, "total": 1, "pages": [target.name],
                "name": target.name}

    images = await asyncio.to_thread(collect_images, target)
    return {"kind": "images", "total": len(images), "name": target.name,
            "pages": [str(p.relative_to(target)) for p in images]}


@router.get("/api/work/page")
async def work_page(path: str = Query(...), i: int = Query(0, ge=0),
                    w: int | None = Query(None, ge=32, le=2048)):
    """One page of a work, or the file itself when the work is one file.

    `FileResponse` answers Range requests, which is the whole of what a
    video needs to be scrubbed rather than only played from the start.
    `w` asks for a thumbnail instead, and is ignored for video.
    """
    from fastapi.responses import FileResponse, Response

    from polaris.observation.media.sampler import collect_images

    target = await _readable(path)
    if target.is_file():
        file = target
    else:
        images = await asyncio.to_thread(collect_images, target)
        if not images:
            raise refuse(404, "no_images", "no images in this work")
        if i >= len(images):
            raise refuse(404, "page_out_of_range",
                         f"page {i} of {len(images)}", total=len(images))
        file = images[i]

    kind = MEDIA_TYPES.get(file.suffix.lower())
    if w and kind and kind.startswith("image/"):
        data = await asyncio.to_thread(_thumbnail, file, w)
        return Response(data, media_type="image/jpeg", headers=PAGE_CACHE)
    return FileResponse(file, media_type=kind, headers=PAGE_CACHE)


def _entry(d) -> dict:
    """One directory, and enough about it to choose it without opening it."""
    from polaris.observation.media.sampler import IMAGE_EXTENSIONS
    from polaris.observation.media.video import VIDEO_EXTENSIONS

    images = subdirs = 0
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    subdirs += 1
                else:
                    ext = Path(e.name).suffix.lower()
                    if ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS:
                        images += 1
    except OSError:
        pass
    return {"name": Path(d).name or str(d), "path": str(d),
            "images": images, "subdirs": subdirs}


@router.get("/api/browse")
async def browse(path: str | None = Query(None)):
    """The directories under `path`, so a root can be chosen and not typed.

    Not limited to configured roots, on purpose: this is how the first
    root gets configured, and an installation with none would otherwise
    have nowhere to begin. It answers with directory names, how much each one holds, and
    the video files directly inside -- never a file's contents.

    It does show the shape of this machine's filesystem to anything that
    can reach the API. That is the same bargain `/api/open` makes, and it
    has the same answer: this binds to localhost.
    """
    import string

    def listing():
        if not path:
            roots = ([Path(f"{c}:\\") for c in string.ascii_uppercase
                      if Path(f"{c}:\\").exists()]
                     if sys.platform == "win32" else [Path("/")])
            return {"path": None, "parent": None, "files": [],
                    "dirs": [{"name": str(r), "path": str(r),
                              "images": 0, "subdirs": 0} for r in roots]}

        here = Path(path)
        if not here.is_dir():
            raise refuse(404, "not_a_directory",
                         f"{here} is not a directory", path=str(here))
        try:
            kids = sorted((e.path for e in os.scandir(here)
                           if e.is_dir() and not e.name.startswith(".")),
                          key=str.lower)
        except OSError as e:
            raise refuse(403, "unreadable_directory", str(e),
                         path=str(here)) from e
        from polaris.observation.media.video import VIDEO_EXTENSIONS

        videos = sorted((e.path for e in os.scandir(here)
                         if e.is_file()
                         and Path(e.name).suffix.lower() in VIDEO_EXTENSIONS),
                        key=str.lower)
        parent = str(here.parent) if here.parent != here else None
        return {"path": str(here), "parent": parent,
                "dirs": [_entry(k) for k in kids],
                "files": [{"name": Path(v).name, "path": v} for v in videos]}

    out = await asyncio.to_thread(listing)
    await _mark_registered(out)
    return out


async def _mark_registered(out: dict) -> None:
    """Name the work each entry already is, and count the places each
    folder holds further down."""
    rows = []
    if out["path"]:
        prefix = _prefix(out["path"])
        under = _same("left(path, length($1))", "$1")
        async with state.store.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT path, work_id, collection, FALSE AS copy "
                f"FROM work_paths WHERE present AND {under} "
                f"UNION ALL SELECT path, work_id, collection, TRUE "
                f"FROM work_copies WHERE {under}", prefix)
    places, holds = {}, Counter()
    for r in rows:
        head, _, tail = os.path.normcase(
            r["path"][len(prefix):]).partition(os.sep)
        if tail:
            holds[head] += 1
        else:
            places[head] = {"id": r["work_id"], "collection": r["collection"],
                            "copy": r["copy"]}
    for d in out["dirs"]:
        d["work"] = places.get(os.path.normcase(d["name"]))
        d["holds"] = holds[os.path.normcase(d["name"])]
    for f in out["files"]:
        f["work"] = places.get(os.path.normcase(f["name"]))


@router.get("/api/browse/unregistered")
async def browse_unregistered(path: str):
    """The folders and video files directly under `path` that are no work
    yet and hold none: what "select every unregistered one" selects."""
    out = await browse(path)
    return {"path": out["path"], "items": [
        *({"name": d["name"], "path": d["path"], "kind": "dir"}
          for d in out["dirs"] if d["work"] is None and not d["holds"]),
        *({"name": f["name"], "path": f["path"], "kind": "video"}
          for f in out["files"] if f["work"] is None)]}


class OpenRequest(BaseModel):
    """What to hand to the desktop. A work, so a folder or a video file."""

    path: str
    reveal: bool = False


@router.post("/api/open")
async def open_on_host(req: OpenRequest):
    """Open a work in whatever this desktop opens it with.

    The one endpoint that hands a path to the operating system. It refuses
    anything `collection_for` does not claim, which keeps it to the roots
    this installation was told about -- but an installation that binds the
    API to anything other than localhost should not be running it at all.

    `reveal` shows it selected in its folder instead: for a video, the
    difference between finding the file and playing it.
    """
    target = await _readable(req.path)
    if req.reveal:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    elif sys.platform == "win32":
        os.startfile(target)
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(target)])
    return {"opened": str(target)}
