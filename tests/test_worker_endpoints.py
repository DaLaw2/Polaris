"""A started worker outlives its request: POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_worker_endpoints.py"""
import asyncio
import tempfile
import time
from pathlib import Path

from _common import dsn, refused

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.jobs import api as jobs_api  # noqa: E402
from polaris.jobs import worker  # noqa: E402


async def gone(timeout: float = 40.0) -> bool:
    """Wait for the roster to empty."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with state.store.acquire() as conn:
            if not await worker.roster(conn):
                return True
        await asyncio.sleep(0.5)
    return False


async def main(tmp: Path) -> None:
    await state.store.connect()
    started, log = None, jobs_api.WORKER_LOG
    jobs_api.WORKER_LOG = tmp / "scan_worker.log"
    try:
        async with state.store.acquire() as conn:
            await conn.execute("DELETE FROM scan_workers")
        out = await jobs_api.scan_worker_status()
        assert out["workers"] == [] and out["lease_s"] == worker.LEASE_S
        assert (await jobs_api.stop_scan_worker(id=None, forget=False))["asked"] == 0
        card = await jobs_api.gpu()
        assert not card["present"] or card["used_mb"] <= card["total_mb"], card
        print("  ok  no worker is an empty list, stopping nobody asks nobody, the card answers")

        started = (await jobs_api.start_scan_worker(force=False))["worker"]["id"]
        assert started.rsplit(":", 1)[1].isdigit(), started
        seen = await jobs_api.scan_worker_status()
        assert [(w["id"], w["job_id"]) for w in seen["workers"]] == [(started, None)]
        why = await refused(jobs_api.start_scan_worker(force=False), 409, "worker_running")
        assert started in why["message"]
        tail = await jobs_api.scan_worker_log(lines=50)
        assert tail["path"] == str(jobs_api.WORKER_LOG)
        assert any(started in line for line in tail["lines"]), tail["lines"][-5:]
        print(f"  ok  {started} starts, holds no job, refuses a second, and its log is readable")

        await asyncio.sleep(12)
        again = (await jobs_api.scan_worker_status())["workers"]
        assert again and again[0]["heartbeat_at"] > seen["workers"][0]["heartbeat_at"]
        assert again[0]["stale"] is False
        print("  ok  it outlives the request that started it, and keeps beating")

        assert (await jobs_api.stop_scan_worker(id=started, forget=False))["asked"] == 1
        assert (await jobs_api.stop_scan_worker(id=started, forget=False))["asked"] == 0
        assert await gone(), "the worker ignored a stop request"
        started = None
        print("  ok  it hears the stop, leaves, and takes its row with it")
        print("all worker endpoint checks passed")
    finally:
        if started:
            async with state.store.acquire() as conn:
                await worker.request_stop(conn, None)
            await gone()
        jobs_api.WORKER_LOG = log
        await state.store.close()


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    asyncio.run(main(Path(t)))
