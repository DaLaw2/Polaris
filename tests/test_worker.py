"""The claim loop and the roster: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_worker.py"""
import asyncio

from _common import dsn

DSN = dsn()

import asyncpg  # noqa: E402

from polaris.jobs import worker  # noqa: E402
from polaris.models import profiles  # noqa: E402


async def claims(a, b, p):
    j1, j2, j3 = [await worker.enqueue(a, collection=f"worker-test-{c}", model_profile_id=p)
                  for c in "abc"]
    g1, g2 = await asyncio.gather(worker.claim(a, "w1"), worker.claim(b, "w2"))
    assert {g1["id"], g2["id"]} == {j1, j2}, (g1["id"], g2["id"])
    assert (await worker.claim(a, "w1"))["id"] == j3 and await worker.claim(a, "w1") is None
    print("  ok  concurrent claims take distinct rows in order; a running job is not offered twice")

    assert await worker.claim(a, "w9", lease="30 seconds") is None
    await a.execute("UPDATE scan_jobs SET heartbeat_at = NOW() - INTERVAL '10 minutes' "
                    "WHERE id = $1", j1)
    again = await worker.claim(a, "w9", lease="2 minutes")
    assert again["id"] == j1 and again["worker_id"] == "w9"
    print("  ok  an expired lease is reclaimed, a live one is not")

    assert await worker.beat(a, j2, done=3, total=10, current_path="x") is False
    assert await worker.request_cancel(a, j2) is True
    assert await worker.beat(a, j2, done=4) is True
    row = await a.fetchrow("SELECT done, total, current_path FROM scan_jobs WHERE id = $1", j2)
    assert tuple(row) == (4, 10, "x")
    await a.execute("UPDATE scan_jobs SET heartbeat_at = NOW() - INTERVAL '1 hour' "
                    "WHERE id = $1", j2)
    re2 = await worker.claim(a, "w3")
    assert re2["id"] == j2 and re2["cancel_requested"] is True
    print("  ok  cancel is seen on the next beat, progress survives, reclaiming keeps it")

    j4 = await worker.enqueue(a, collection="worker-test-d", model_profile_id=p)
    assert await worker.request_cancel(a, j4) is True
    row = await a.fetchrow("SELECT state, finished_at FROM scan_jobs WHERE id = $1", j4)
    assert row["state"] == "cancelled" and row["finished_at"] and await worker.claim(a, "w4") is None
    print("  ok  cancelling a queued job ends it, and nothing claims it")

    d1 = await worker.enqueue_derive(a, p)
    assert await worker.enqueue_derive(a, p) == d1 and await worker.claim(a, "w6") is None
    got = await worker.claim(a, "api", kinds=("derive",))
    assert got["id"] == d1 and got["kind"] == "derive"
    await worker.release(a, d1, "done")
    print("  ok  a derive job is queued once and claimed only by a derive loop")

    async def boom(conn, job):
        raise RuntimeError("decoder exploded")

    assert await worker.run_job(a, re2, boom) == "failed"
    row = await a.fetchrow("SELECT state, error, finished_at FROM scan_jobs WHERE id = $1", j2)
    assert row["state"] == "failed" and row["finished_at"] and "decoder exploded" in row["error"]
    g3 = await a.fetchrow("SELECT * FROM scan_jobs WHERE id = $1", j3)
    assert await worker.run_job(a, g3, lambda conn, job: asyncio.sleep(0)) == "done"
    row = await a.fetchrow("SELECT state, finished_at, current_path FROM scan_jobs "
                           "WHERE id = $1", j3)
    assert row["state"] == "done" and row["finished_at"] and row["current_path"] is None
    try:
        await a.execute("UPDATE scan_jobs SET finished_at = NULL WHERE id = $1", j3)
        raise AssertionError("the finished_at CHECK did not fire")
    except asyncpg.exceptions.CheckViolationError:
        pass
    print("  ok  a body that raises fails the row, one that returns finishes it; "
          "a terminal state needs finished_at")


async def roster(conn, p):
    A, B = "worker-test:1", "worker-test:2"
    assert worker.LEASE == f"{worker.LEASE_S} seconds" and worker.BEAT_EVERY * 2 < worker.LEASE_S
    job = await worker.enqueue(conn, path="/nowhere", model_profile_id=p)
    assert await worker.roster(conn) == [], "a queued job invented a worker"
    await worker.register(conn, A)
    row = (await worker.roster(conn))[0]
    assert (row["stale"], row["job_id"], row["models_loaded"]) == (False, None, False)
    assert (await worker.claim(conn, A))["id"] == job
    await worker.seen(conn, A, models_loaded=True)
    row = (await worker.roster(conn))[0]
    assert row["job_id"] == job and row["models_loaded"] is True
    print("  ok  no worker is no row; a worker names its job and admits to the models")

    await conn.execute("UPDATE scan_workers SET heartbeat_at = NOW() - $1::text::interval "
                       "WHERE id = $2", f"{worker.LEASE_S * 2} seconds", A)
    assert (await worker.roster(conn))[0]["stale"] is True
    await worker.beat(conn, job, done=1)
    assert (await worker.roster(conn))[0]["stale"] is False, "a beat left the worker stale"
    await worker.register(conn, A)
    assert len(await worker.roster(conn)) == 1, "re-registering added a worker"
    print(f"  ok  past the {worker.LEASE_S}s lease reads stale; a beat or re-register revives it")

    assert await worker.request_stop(conn, A) == 1 and await worker.request_stop(conn, A) == 0
    assert await worker.seen(conn, A) is True
    assert await worker.beat(conn, job, done=1, total=10) is True, "a running job missed the stop"
    await worker.register(conn, A)
    assert await worker.beat(conn, job, done=2) is False
    await worker.request_cancel(conn, job)
    assert await worker.beat(conn, job, done=3) is True
    print("  ok  a stop reaches the worker idle and mid-job; cancelling a job is not a stop")

    await worker.register(conn, B)
    assert await worker.request_stop(conn, None) == 2
    assert all(r["stop_requested"] for r in await worker.roster(conn))
    await worker.retire(conn, A)
    await worker.retire(conn, B)
    assert await worker.roster(conn) == []
    print("  ok  stopping everything asks every worker; a worker that left leaves no row")


async def main():
    a, b = await asyncpg.connect(DSN), await asyncpg.connect(DSN)
    try:
        await a.execute("DELETE FROM scan_jobs; DELETE FROM scan_workers")
        p = await profiles.active_profile(a)
        await claims(a, b, p)
        await a.execute("DELETE FROM scan_jobs")
        await roster(a, p)
    finally:
        await a.execute("DELETE FROM scan_jobs; DELETE FROM scan_workers")
        await a.close()
        await b.close()
    print("all worker checks passed")


asyncio.run(main())
