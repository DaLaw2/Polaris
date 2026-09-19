"""Checking items against the library: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_check.py"""
import asyncio
import queue
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from _common import dsn, png, refused

DSN = dsn()

import asyncpg  # noqa: E402

from polaris import config, state  # noqa: E402
from polaris.catalog import api as catalog_api  # noqa: E402
from polaris.catalog import trash  # noqa: E402
from polaris.jobs import worker  # noqa: E402
from polaris.models import profiles  # noqa: E402
from polaris.models.backends.base import RawScore, SampleScores  # noqa: E402
from polaris.observation import api as observation_api  # noqa: E402
from polaris.observation import checking, ingest  # noqa: E402
from polaris.observation.domain import Sample, WorkObservation  # noqa: E402
from polaris.observation.media.sampler import content_key  # noqa: E402

COLL = "check-test"
NEAR = [1.0, 0.0, 0.0, 0.0]


def work(folder: Path, pad: int = 0) -> Path:
    """A folder with one image; `pad` moves only its size."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "001.png").write_bytes(png() + b"\0" * pad)
    return folder


class FakeTagger:
    _configs = [SimpleNamespace(backend=SimpleNamespace(name="fake"))]

    def ensure_loaded(self):
        pass

    def prepare_one(self, image, index=0, only=None):
        return {}


class FakePipeline:
    """Observes without observing; the vector depends on the folder name."""

    def __init__(self, vectors: dict[str, list[float]]):
        self.tagger, self.vectors, self.seen = FakeTagger(), vectors, []

    def observe_batch(self, targets, n_samples=8, show_progress=True,
                      require_collection=True):
        assert require_collection is False, "a check refused its own item"
        for t in targets:
            self.seen.append(str(t))
            yield WorkObservation(path=str(t), name=t.name, collection="",
                                  samples=[Sample("image", "001.png", 0, 0)],
                                  embeddings={"fake": self.vectors[t.name]})

    def release(self):
        pass


def test_stream_work_without_a_collection(tmp: Path) -> None:
    """The one path into `_stream_work` no other test enters."""
    from polaris.observation.pipeline import Pipeline, WorkPlan, _WorkState

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.tagger, pipeline._initialized = FakeTagger(), True
    st = _WorkState(WorkPlan(work(tmp / "loose")))
    pipeline._stream_work(st, 8, queue.Queue(), threading.Semaphore(8),
                          threading.Event(), False)
    assert st.obs is not None and st.obs.error is None, st.obs and st.obs.error
    assert st.obs.content_key.startswith("img:"), st.obs.content_key
    print("  ok  a work under no collection streams when a check asks for it")


async def test_tiers(conn, tmp: Path) -> int:
    """Exact first and free; the rest go to the models once; nothing is admitted."""
    pid = await profiles.active_profile(conn)
    vid = await profiles.model_version(conn, "fake")
    await conn.execute("INSERT INTO model_profile_members (profile_id, model_version_id, "
                       "rank) VALUES ($1, $2, 1) ON CONFLICT DO NOTHING", pid, vid)
    await conn.execute("INSERT INTO model_profile_roles (profile_id, model_version_id, "
                       "role) VALUES ($1, $2, 'embedding') ON CONFLICT DO NOTHING", pid, vid)
    params = await checking.freeze(conn, ["x"])
    assert params["paths"] == ["x"] and params["near"] == 0.92, params
    assert params["version"] == {"id": vid, "backend": "fake"}, params
    print("  ok  a check freezes its items, the embedding model and check:near")

    held = work(tmp / "lib" / "held")
    obs = WorkObservation(path=str(held), name="held", collection=COLL,
                          content_key=content_key(held),
                          samples=[Sample("image", "001.png", 0, 0)],
                          scores=[{"fake": SampleScores([RawScore("general", "x", 0.9)],
                                                        embedding=NEAR)}])
    run = await ingest.begin_run(conn, {"test": "test_check"})
    held_id = await ingest.record_observation(conn, run, obs, {"fake": vid})
    items = [work(tmp / "inbox" / "dupe"), work(tmp / "inbox" / "close", pad=100),
             work(tmp / "inbox" / "fresh", pad=200)]
    pipeline = FakePipeline({"close": NEAR, "fresh": [0.0, 1.0, 0.0, 0.0]})
    counted = "SELECT (SELECT count(*) FROM works), (SELECT count(*) FROM work_paths), " \
              "(SELECT count(*) FROM work_copies)"
    before = tuple(await conn.fetchrow(counted))
    job_id = await worker.enqueue(conn, "check", params=await checking.freeze(
        conn, [str(p) for p in items]))
    job = await worker.claim(conn, "check-test")
    assert job["id"] == job_id and job["kind"] == "check"
    assert await worker.run_job(conn, job, checking.job_body(
        lambda keep, b, k: pipeline)) == "done"
    rows = {Path(r["path"]).name: r for r in await conn.fetch(
        "SELECT path, verdict, model_version_id FROM check_items WHERE job_id = $1", job_id)}
    assert {n: r["verdict"] for n, r in rows.items()} == {
        "dupe": "same", "close": "near", "fresh": "new"}, rows
    assert rows["close"]["model_version_id"] == vid and rows["dupe"]["model_version_id"] is None
    assert len(pipeline.seen) == 2 and not any(s.endswith("dupe") for s in pipeline.seen)
    assert tuple(await conn.fetchrow(counted)) == before
    print("  ok  same, near and new are told apart; the duplicate never reached "
          "the models; nothing entered the library")

    got = await observation_api.check_job_items(job_id, verdict=None, limit=50, offset=0)
    assert got["total"] == 3 and got["counts"] == {"same": 1, "near": 1, "new": 1}, got
    assert [Path(i["path"]).name for i in got["items"]] == ["dupe", "close", "fresh"]
    near = await observation_api.check_job_items(job_id, verdict="near", limit=50, offset=0)
    assert [Path(i["path"]).name for i in near["items"]] == ["close"], near
    assert [m["work"].folder_path for m in near["items"][0]["matches"]] == [
        str(tmp / "lib" / "held")], near
    assert (await observation_api.check_job_items(job_id, verdict="new", limit=50,
                                                  offset=0))["items"][0]["matches"] == []
    await refused(observation_api.check_job_items(job_id + 10_000, verdict=None,
                                                  limit=50, offset=0), 404, "check_not_found")
    print("  ok  the items read duplicates first, filter by verdict, and name the match")
    return held_id


async def test_discard_needs_a_live_library_copy(conn, tmp, held_id) -> None:
    """The library's own copy on disk is what makes this not the last one."""
    ids = {Path(r["path"]).name: r["id"] for r in await conn.fetch(
        "SELECT id, path FROM check_items")}
    await refused(observation_api.discard_check_item(ids["fresh"]), 404)
    held = Path(await conn.fetchval(
        "SELECT path FROM work_paths WHERE work_id = $1 AND present", held_id))
    shutil.rmtree(held)
    await refused(observation_api.discard_check_item(ids["dupe"]), 409)
    assert (tmp / "inbox" / "dupe").exists()
    work(held)
    await observation_api.discard_check_item(ids["dupe"])
    assert not (tmp / "inbox" / "dupe").exists()
    assert await conn.fetchval("SELECT discarded_at IS NOT NULL FROM check_items "
                               "WHERE id = $1", ids["dupe"])
    assert await conn.fetchval("SELECT count(*) FROM work_paths WHERE work_id = $1 "
                               "AND present", held_id) == 1
    print("  ok  discard: an unmatched item 404s, a missing library copy refuses, "
          "else only the checked copy goes")


async def test_endpoints_and_guard(conn, tmp: Path) -> None:
    """Creating and forgetting a check, and which items preview may read."""
    (tmp / "note.txt").write_text("x")
    for paths, code in (([], "paths_required"), ([str(tmp / "note.txt")], "not_registrable"),
                        ([str(tmp / "lib" / "held")], "inside_collections")):
        await refused(observation_api.create_check_job(
            observation_api.CheckJobRequest(paths=paths)), 400, code)
    made = await observation_api.create_check_job(observation_api.CheckJobRequest(
        paths=[str(work(tmp / "inbox" / "later"))]))
    assert made["state"] == "queued" and await conn.fetchval(
        "SELECT kind FROM scan_jobs WHERE id = $1", made["job_id"]) == "check"
    assert await observation_api.forget_check_job(made["job_id"]) == {
        "forgotten": made["job_id"]}
    await refused(observation_api.forget_check_job(made["job_id"]), 404, "check_not_found")
    print("  ok  a check job refuses empty, non-work and in-library items, and is forgotten once")

    inside, outside = tmp / "inbox" / "close", work(tmp / "elsewhere")
    catalog_api._forget_check_roots()
    assert await catalog_api._readable(str(inside)) == inside
    await refused(catalog_api._readable(str(outside)), 403)
    await conn.execute("DELETE FROM scan_jobs WHERE kind = 'check'")
    catalog_api._forget_check_roots()
    await refused(catalog_api._readable(str(inside)), 403)
    print("  ok  a checked item is readable only while its check is on record")


async def cleanup(conn) -> None:
    await conn.execute("DELETE FROM scan_jobs WHERE kind = 'check'")
    await conn.execute("DELETE FROM works WHERE id IN (SELECT work_id FROM work_paths "
                       "WHERE collection = $1)", COLL)
    await conn.execute("DELETE FROM scan_runs WHERE params->>'test' = 'test_check'")
    await conn.execute("DELETE FROM model_profile_members WHERE model_version_id IN "
                       "(SELECT id FROM model_versions WHERE backend = 'fake')")
    await conn.execute("DELETE FROM collections WHERE name = $1", COLL)


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    from pgvector.asyncpg import register_vector

    await register_vector(conn)
    await state.store.connect()
    real, trash.recycle = trash.recycle, lambda path, size: shutil.rmtree(path)
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            await cleanup(conn)
            await conn.execute("INSERT INTO collections (name, root) VALUES ($1, $2)",
                               COLL, str(tmp / "lib"))
            await config.load_collections(conn)
            test_stream_work_without_a_collection(tmp)
            held_id = await test_tiers(conn, tmp)
            await test_discard_needs_a_live_library_copy(conn, tmp, held_id)
            await test_endpoints_and_guard(conn, tmp)
    finally:
        trash.recycle = real
        await cleanup(conn)
        await state.store.close()
        await conn.close()
    print("all check tests passed")


asyncio.run(main())
