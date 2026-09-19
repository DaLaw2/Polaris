"""A scan job end to end with fake backends: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_scan_job.py"""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from _common import dsn

DSN = dsn()

import asyncpg  # noqa: E402
from PIL import Image  # noqa: E402

from polaris.jobs import worker  # noqa: E402
from polaris.models import profiles  # noqa: E402
from polaris.models.backends.base import RawScore, SampleScores, views_of  # noqa: E402
from polaris.models.tagger import Tagger  # noqa: E402
from polaris.observation import scanning  # noqa: E402
from polaris.observation.pipeline import Pipeline  # noqa: E402

COLL = "scan-job-test"
COLLS = ("scanning-a", "scanning-b")
NAMES = ("fake", "fake2")
prepared: dict[str, int] = {}


class FakeBackend:
    """Scores every image the same, and counts what it was given."""

    def __init__(self, name):
        self.name = name

    def load(self):
        pass

    def unload(self):
        pass

    def prepare(self, image):
        prepared[self.name] = prepared.get(self.name, 0) + 1
        return views_of(image).rgb().size

    def score_prepared(self, batch, top_k=0, keep=frozenset()):
        return [SampleScores([RawScore("general", f"{self.name}_tag", 0.9)],
                             embedding=[1.0, 0.0, 0.0, 0.0]) for _ in batch]


def factory(keep, versions, top_k):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.keep_tags, pipeline.raw_top_k, pipeline._initialized = keep, 300, False
    pipeline.tagger = Tagger([
        SimpleNamespace(backend=FakeBackend(v["backend"]), max_images=0,
                        concurrent=False, category_priority={}) for v in versions])
    return pipeline


def page(path: Path, side: int = 400) -> None:
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path)


def work(base: Path, name: str, pages: int = 2) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(pages):
        page(d / f"{i:03}.png")
    return d


async def fresh(conn):
    await conn.execute("DELETE FROM scan_jobs; DELETE FROM work_claims; DELETE FROM works; "
                       "DELETE FROM scan_errors; DELETE FROM scan_runs")


def test_pure(tmp: Path):
    folder, video = work(tmp, "work", 1), tmp / "clip.mp4"
    video.write_bytes(b"x" * 10)
    key = scanning.content_key(folder)
    assert key.startswith("img:") and scanning.content_key(folder.rename(tmp / "moved")) == key
    assert scanning.content_key(video).startswith("vid:")
    assert scanning.content_key(tmp / "nothing") is None
    ok = scanning._storable(os.path.join("somewhere", "a folder", "\ud83d cover.png"))
    ok.encode("utf-8")
    assert "a folder" in ok, ok
    print("  ok  identity follows content, not place; an unstorable path survives as text")


async def test_placeholder_revision(conn) -> None:
    """The first load claims a revision-less member's row."""
    name = "fake-placeholder"
    await conn.execute("DELETE FROM model_versions WHERE backend = $1", name)
    empty = await profiles.model_version(conn, name)
    snapshot = os.path.join("models--x--y", "snapshots", "abc123", "m.onnx")
    assert await profiles.model_version(conn, name, snapshot) == empty
    assert await conn.fetchval(
        "SELECT revision FROM model_versions WHERE id = $1", empty) == "abc123"
    other = os.path.join("models--x--y", "snapshots", "def456", "m.onnx")
    assert await profiles.model_version(conn, name, other) != empty
    await conn.execute("DELETE FROM model_versions WHERE backend = $1", name)
    print("  ok  a revision-less member is claimed by the first load")


async def test_registered(conn, root: str):
    """A collection, a folder prefix, or every searchable collection."""
    a, b = os.path.join(root, "a"), os.path.join(root, "b")
    await conn.execute("INSERT INTO collections (name, root, searchable) VALUES "
                       "($1, $3, TRUE), ($2, $4, FALSE)", *COLLS, a, b)
    x = os.path.join(a, "x")
    paths = {x: (COLLS[0], True), os.path.join(x, "inner"): (COLLS[0], True),
             os.path.join(a, "xy"): (COLLS[0], True), os.path.join(a, "gone"): (COLLS[0], False),
             os.path.join(b, "z"): (COLLS[1], True)}
    for path, (coll, present) in paths.items():
        wid = await conn.fetchval(
            "INSERT INTO works (content_key) VALUES ($1) RETURNING id", f"scanning:{path}")
        await conn.execute("INSERT INTO work_paths (work_id, path, collection, present) "
                           "VALUES ($1, $2, $3, $4)", wid, path, coll, present)
    await conn.execute("INSERT INTO work_copies (work_id, path, collection) "
                       "SELECT work_id, $1, $2 FROM work_paths WHERE path = $3",
                       x + "-copy", COLLS[0], x)
    live = sorted(p for p, (c, on) in paths.items() if on and c == COLLS[0])
    assert await scanning.registered(conn, COLLS[0], None) == (live, [x + "-copy"])
    assert (await scanning.registered(conn, None, x))[0] == [x, os.path.join(x, "inner")]
    assert os.path.join(b, "z") not in (await scanning.registered(conn, None, None))[0]
    assert (await scanning.registered(conn, COLLS[1], None))[0] == [os.path.join(b, "z")]
    await conn.execute("DELETE FROM works WHERE content_key LIKE 'scanning:%'")
    await conn.execute("DELETE FROM collections WHERE name = ANY($1::text[])", list(COLLS))
    print("  ok  a rescan covers present paths by collection, folder or every searchable one")


async def run(conn, pid, who, *, cancel=False, **freeze) -> tuple[str, dict]:
    prepared.clear()
    paths = freeze.pop("paths", None)
    params = await scanning.freeze(conn, pid, paths=paths, **freeze)
    job_id = await worker.enqueue(conn, "scan", collection=None if paths else COLL,
                                  params=params, model_profile_id=pid)
    job = await worker.claim(conn, who)
    assert job["id"] == job_id, (job["id"], job_id)
    if cancel:
        assert await worker.request_cancel(conn, job_id)
    state = await worker.run_job(conn, job, scanning.job_body(factory))
    return state, dict(await conn.fetchrow("SELECT * FROM scan_jobs WHERE id = $1", job_id))


async def samples(conn, name):
    return {r["id"]: r["run_id"] for r in await conn.fetch(
        "SELECT s.id, s.run_id FROM work_samples s JOIN work_paths p ON p.work_id = s.work_id "
        "WHERE p.path LIKE '%' || $1", os.sep + name)}


async def count(conn, sql, *args):
    return await conn.fetchval(f"SELECT count(*) FROM {sql}", *args)


async def test_jobs(conn, root: Path):
    await conn.execute("INSERT INTO collections (name, root) VALUES ($1, $2) "
                       "ON CONFLICT (name) DO UPDATE SET root = EXCLUDED.root", COLL, str(root))
    pid = await profiles.active_profile(conn)
    versions = {n: await profiles.model_version(conn, n) for n in NAMES}
    await conn.execute("DELETE FROM model_profile_members WHERE profile_id = $1", pid)
    for vid in versions.values():
        await conn.execute("INSERT INTO model_profile_members (profile_id, model_version_id, "
                           "rank) VALUES ($1, $2, 1)", pid, vid)
    one, two = work(root, "one"), work(root, "two")

    state, job = await run(conn, pid, "t1", paths=[str(one), str(two), str(root / "gone")])
    assert state == "done", (state, job["error"])
    assert (job["done"], job["total"]) == (3, 3) and job["run_id"] and job["current_path"] is None
    assert sorted(v["backend"] for v in scanning.as_dict(job["params"])["versions"]) == list(NAMES)
    assert await count(conn, "works") == 2 and prepared == {"fake": 4, "fake2": 4}, prepared
    assert [e["stage"] for e in await conn.fetch("SELECT stage FROM scan_errors")] == ["missing"]
    assert await count(conn, "derived.work_vectors") == 4
    print("  ok  registering scores every work with every member; a missing path is an error")

    state, job = await run(conn, pid, "t2")
    assert state == "done" and job["run_id"] is None and prepared == {}, (job, prepared)
    print("  ok  a rescan with nothing missing decodes nothing, opens no run")

    before, runs = await samples(conn, "one"), await count(conn, "scan_runs")
    for table in ("sample_scores", "work_embeddings", "derived.work_vectors"):
        await conn.execute(f"DELETE FROM {table} WHERE model_version_id = $1", versions["fake2"])
    state, job = await run(conn, pid, "t3")
    assert state == "done" and prepared == {"fake2": 4}, (job["error"], prepared)
    assert await samples(conn, "one") == before, "rescoring moved samples"
    assert await count(conn, "sample_scores WHERE model_version_id = $1", versions["fake2"]) == 4
    assert await count(conn, "scan_runs") == runs
    assert await count(conn, "derived.work_vectors WHERE model_version_id = $1",
                       versions["fake2"]) == 2
    print("  ok  a missing member rescores the recorded samples, and only that member runs")

    state, job = await run(conn, pid, "t4", force=True)
    assert state == "done" and prepared == {"fake": 4, "fake2": 4}, prepared
    assert set((await samples(conn, "one")).values()) == {job["run_id"]}
    print("  ok  force observes afresh in a new run and drops the old one")

    page(one / "002.png")
    state, job = await run(conn, pid, "t5")
    assert state == "done" and prepared == {"fake": 3, "fake2": 3}, (job["error"], prepared)
    assert await count(conn, "works") == 2 and len(await samples(conn, "one")) == 3
    print("  ok  changed content is observed afresh on the same work")

    state, job = await run(conn, pid, "t6", force=True, cancel=True)
    assert state == "cancelled" and prepared == {}, (state, prepared)
    print("  ok  a cancel read on the first beat observes nothing")

    shutil.rmtree(two)
    assert (await run(conn, pid, "t7"))[0] == "done"
    present = {Path(r["path"]).name: r["present"] for r in await conn.fetch(
        "SELECT path, present FROM work_paths")}
    assert present == {"one": True, "two": False}, present
    shutil.rmtree(one)
    assert (await run(conn, pid, "t8"))[0] == "done"
    assert await count(conn, "work_paths WHERE present") == 1
    print("  ok  a work gone from disk is retired, but not when nothing registered is there")

    for column, value, why in (("revision", "abc", "loaded revision"),
                               ("state", "building", "not ready")):
        await conn.execute(f"UPDATE model_versions SET {column} = $2 WHERE id = $1",
                           versions["fake"], value)
        state, job = await run(conn, pid, "t9")
        assert state == "failed" and why in job["error"], job["error"]
    await conn.execute("UPDATE model_versions SET revision = NULL, state = 'ready' "
                       "WHERE id = $1", versions["fake"])
    print("  ok  a version that is not ready, or loads at another revision, fails the job")
    await conn.execute("DELETE FROM model_profile_members WHERE profile_id = $1", pid)


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    from pgvector.asyncpg import register_vector

    await register_vector(conn)
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            test_pure(tmp)
            await fresh(conn)
            await test_placeholder_revision(conn)
            await test_registered(conn, str(tmp / "reg"))
            await test_jobs(conn, tmp / "lib")
        print("all scan-job checks passed")
    finally:
        await fresh(conn)
        await conn.execute("DELETE FROM collections WHERE name = ANY($1::text[])",
                           [COLL, *COLLS])
        await conn.close()


asyncio.run(main())
