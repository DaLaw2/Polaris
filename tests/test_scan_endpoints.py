"""The scan-job endpoints, called directly: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_scan_endpoints.py"""
import asyncio
import json
import tempfile
from pathlib import Path

from _common import dsn, refused

DSN = dsn()

from polaris import app as polaris_app  # noqa: E402
from polaris import config, state  # noqa: E402
from polaris.jobs import api as jobs_api  # noqa: E402
from polaris.jobs import worker  # noqa: E402
from polaris.models import profiles  # noqa: E402
from polaris.observation import api as observation_api  # noqa: E402

COLL = "endpoint-test"
Scan = observation_api.ScanJobRequest


async def test_reindex(tmp: Path, pid: int, vid: int):
    """One work is redone as a forced scan job under the active profile."""
    (tmp / "a work").mkdir()
    await refused(observation_api.reindex_work(path=str(tmp / "a work")), 400)
    async with state.store.acquire() as conn:
        await conn.execute("INSERT INTO model_profile_members VALUES ($1, $2, 1)", pid, vid)
    out = await observation_api.reindex_work(path=str(tmp / "a work"))
    assert out["state"] == "queued" and out["job_id"], out
    async with state.store.acquire() as conn:
        row = await conn.fetchrow("SELECT path, kind, params, model_profile_id FROM scan_jobs "
                                  "WHERE id = $1", out["job_id"])
        await conn.execute("DELETE FROM scan_jobs WHERE id = $1", out["job_id"])
    params = json.loads(row["params"])
    assert (row["path"], row["kind"], row["model_profile_id"]) == (
        str(tmp / "a work"), "scan", pid), dict(row)
    assert params["force"] is True and {"id": vid, "backend": "endpoint-fake"} in params["versions"]
    with tempfile.TemporaryDirectory() as outside:
        await refused(observation_api.reindex_work(path=outside), 400)
    print("  ok  reindex refuses an empty profile and a path under no root, "
          "else queues a forced scan job")


async def main(tmp: Path) -> None:
    async with polaris_app.lifespan(polaris_app.app):
        assert state.engine is not None and state.entity_mgr is not None
    print("  ok  the app starts with a live search engine and vocabulary")
    await state.store.connect()
    try:
        async with state.store.acquire() as conn:
            await conn.execute("DELETE FROM scan_jobs")
            await conn.execute("INSERT INTO collections (name, root) VALUES ($1, $2) "
                               "ON CONFLICT (name) DO UPDATE SET root = EXCLUDED.root",
                               COLL, str(tmp))
            await config.load_collections(conn)
            pid = await profiles.active_profile(conn)
            await conn.execute("DELETE FROM model_profile_members WHERE profile_id = $1", pid)
            vid = await profiles.model_version(conn, "endpoint-fake")
        await test_reindex(tmp, pid, vid)

        (tmp / "note.txt").write_text("x")
        for req, status in ((Scan(collection=COLL, profile="no-such-profile"), 404),
                            (Scan(path="/nowhere"), 400),
                            (Scan(paths=[str(tmp / "note.txt")]), 400), (Scan(paths=[]), 400)):
            await refused(observation_api.create_scan_job(req), status)
        print("  ok  unknown profiles, outside paths and non-works are refused")

        made = await observation_api.create_scan_job(Scan(paths=[str(tmp / "a work")], force=True))
        got = await observation_api.get_scan_job(made["job_id"])
        assert got["paths"] == [str(tmp / "a work")] and got["force"] is True, got
        assert got["collection"] is None and got["model_profile_id"] == pid
        made = await observation_api.create_scan_job(Scan(collection=COLL))
        job_id = made["job_id"]
        one = await observation_api.get_scan_job(job_id)
        assert made["state"] == one["state"] == "queued" and one["done"] == 0
        assert one["total"] is None and one["paths"] is None
        assert one["errors"] == 0 and one["worker_stale"] is False
        print("  ok  registering freezes its paths; a queued rescan has no total yet")

        async with state.store.acquire() as conn:
            await conn.execute("DELETE FROM scan_jobs WHERE id <> $1", job_id)
            assert (await worker.claim(conn, "endpoint-test"))["id"] == job_id
            await worker.beat(conn, job_id, done=7, total=100, current_path="/somewhere/a work")
        one = await observation_api.get_scan_job(job_id)
        assert one["state"] == "running" and (one["done"], one["total"]) == (7, 100)
        assert one["current_path"].endswith("a work") and one["worker_id"] == "endpoint-test"
        assert one["worker_stale"] is False, "a fresh heartbeat read as stale"
        async with state.store.acquire() as conn:
            await conn.execute("UPDATE scan_jobs SET heartbeat_at = NOW() - "
                               "INTERVAL '10 minutes' WHERE id = $1", job_id)
        assert (await observation_api.get_scan_job(job_id))["worker_stale"] is True
        print("  ok  a running job reports its work; a lapsed lease reads stale")

        assert (await observation_api.cancel_scan_job(job_id))["cancel_requested"] is True
        after = await observation_api.get_scan_job(job_id)
        assert after["cancel_requested"] is True and after["state"] == "running", after
        listed = await observation_api.list_scan_jobs(limit=5, kind=None)
        assert [j["id"] for j in listed["jobs"]] == [job_id]
        print("  ok  cancelling asks; state stays what the worker last wrote; it is listed")

        async with state.store.acquire() as conn:
            run_id = await conn.fetchval(
                "INSERT INTO scan_runs (params) VALUES ('{}') RETURNING id")
            await conn.execute("UPDATE scan_jobs SET run_id = $2 WHERE id = $1", job_id, run_id)
            await conn.execute("INSERT INTO scan_errors (run_id, path, stage, message) "
                               "VALUES ($1, '/a/broken', 'sampling', 'no pages decoded')", run_id)
        errs = await observation_api.scan_job_errors(job_id, limit=100)
        assert [e["stage"] for e in errs["errors"]] == ["sampling"]
        assert (await observation_api.get_scan_job(job_id))["errors"] == 1
        print("  ok  a failed work is readable from the job")

        async with state.store.acquire() as conn:
            await conn.execute("UPDATE scan_jobs SET state='done', finished_at=NOW() "
                               "WHERE id = $1", job_id)
            live = await worker.enqueue(conn, "scan", params={}, model_profile_id=pid)
            check = await worker.enqueue(conn, "check", params={})
            await conn.execute("UPDATE scan_jobs SET state='failed', finished_at=NOW() "
                               "WHERE id = $1", check)
            await conn.execute("INSERT INTO check_items (job_id, path, verdict) "
                               "VALUES ($1, 'x', 'new')", check)
        for call, status, code in (
                (observation_api.cancel_scan_job(job_id), 409, "job_not_running"),
                (observation_api.get_scan_job(job_id + 10_000), 404, None),
                (observation_api.delete_scan_job(live), 409, "job_not_finished"),
                (observation_api.delete_scan_job(job_id + 10_000), 404, "job_not_found")):
            await refused(call, status, code)
        assert await observation_api.delete_scan_job(job_id) == {"deleted": job_id}
        await observation_api.delete_scan_job(check)
        async with state.store.acquire() as conn:
            left = await conn.fetchrow(
                "SELECT (SELECT COUNT(*) FROM scan_jobs WHERE id = ANY($1)), "
                "(SELECT COUNT(*) FROM scan_runs WHERE id = $2), "
                "(SELECT COUNT(*) FROM scan_errors WHERE run_id = $2), "
                "(SELECT COUNT(*) FROM check_items WHERE job_id = $3)",
                [job_id, check], run_id, check)
            assert tuple(left) == (0, 1, 1, 0), tuple(left)
            await conn.execute("DELETE FROM scan_jobs WHERE id = $1", live)
        print("  ok  finished jobs refuse cancel; deleting one keeps its run and errors, "
              "takes a check's items, and refuses a queued one")

        async with state.store.acquire() as conn:
            await worker.enqueue(conn, "scan", params={}, model_profile_id=pid)
            await worker.enqueue(conn, "check", params={})
            await worker.enqueue_derive(conn, pid)
            await worker.claim(conn, "endpoint-test", kinds=("derive",))
            await worker.enqueue_derive(conn, pid)
        status = await jobs_api.scan_worker_status()
        assert status["by_kind"] == {
            "scan": {"queued": 1, "running": 0}, "check": {"queued": 1, "running": 0},
            "build": {"queued": 0, "running": 0},
            "derive": {"queued": 1, "running": 1}}, status["by_kind"]
        assert status["queued"] == 2, status["queued"]
        print("  ok  the queue is counted per kind, derive included")
    finally:
        async with state.store.acquire() as conn:
            await conn.execute("DELETE FROM scan_jobs; DELETE FROM scan_runs")
            await conn.execute(
                "DELETE FROM model_profile_members m USING model_versions v "
                "WHERE v.id = m.model_version_id AND v.backend = 'endpoint-fake'")
            await conn.execute("DELETE FROM collections WHERE name = $1", COLL)
            await config.load_collections(conn)
        await state.store.close()
    print("all scan endpoint checks passed")


with tempfile.TemporaryDirectory() as t:
    asyncio.run(main(Path(t)))
