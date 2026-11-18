#!/usr/bin/env python
"""Run scan jobs as they are asked for.

A separate process on purpose. The scan blocks for the whole of every work
-- the inference happens on threads the event loop cannot see past -- so
running it inside the API server means hours with no heartbeat.
Here the API can be restarted, or crash, and the scan keeps going; the
only thing between them is a table.

One worker, one job at a time, because there is one GPU. Being the one
is holding an advisory lock; a second process finds it held and leaves.

    python -m polaris.cli.scan_worker           # keep taking jobs
    python -m polaris.cli.scan_worker --once    # take at most one, then stop

While it waits it says so in `scan_workers`, because "this job is queued
and nothing is going to take it" is otherwise indistinguishable from "this
job is about to start". Waiting long enough with nothing queued, it lets
go of the models and keeps waiting.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from polaris.jobs import worker
from polaris.observation import building, checking, scanning
from polaris.shared import tuning
from polaris.shared.db import Store

POLL = 5.0

log = logging.getLogger(__name__)


async def idle_release(conn) -> float:
    """Seconds of idleness after which the models are let go."""
    name = "worker:idle_release_s"
    return (await tuning.params(conn, {name: 600.0}))[name]


async def main(args) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from polaris.models import tagger
    from polaris.observation.pipeline import Pipeline

    store = Store()
    await store.connect()
    who = worker.worker_id()

    lock = await worker.hold(store.dsn, who)
    async with store.acquire() as conn:
        if lock is None:
            for w in await worker.roster(conn):
                doing = (f"job {w['job_id']}" if w["job_id"]
                         else "waiting for one")
                log.info("%s is already here (%s).", w["id"], doing)
            return 0
        await worker.register(conn, who)
        idle_release_s = await idle_release(conn)

    log.info("worker %s waiting for jobs (models released after %.0fs idle)",
             who, idle_release_s)

    made: dict[tuple, object] = {}

    def release_pipelines() -> None:
        for p in made.values():
            p.release()
        made.clear()

    def factory(keep, versions, top_k):
        key = (keep, tuple(v["id"] for v in versions), top_k)
        if key not in made:
            release_pipelines()
            made[key] = Pipeline(versions, keep_tags=keep,
                                 raw_top_k=top_k or tagger.RAW_TOP_K)
        return made[key]

    bodies = {"scan": scanning.job_body(factory, on_note=log.info),
              "check": checking.job_body(factory, on_note=log.info),
              "build": building.job_body(release_pipelines, on_note=log.info)}

    idle_since = time.monotonic()
    try:
        while True:
            async with store.acquire() as conn:
                job = await worker.claim(conn, who)
            if job is not None:
                log.info("%s job %s: collection=%r path=%r", job["kind"],
                         job["id"], job["collection"], job["path"])
                async with store.acquire() as conn:
                    await worker.seen(conn, who, models_loaded=bool(made))
                    state = await worker.run_job(
                        conn, job, bodies[job["kind"]])
                log.info("job %s %s", job["id"], state)
                idle_since = time.monotonic()
                if args.once:
                    return 0
                continue

            if args.once:
                log.info("nothing queued")
                return 0

            await lock.execute("SELECT 1")
            async with store.acquire() as conn:
                if await worker.seen(conn, who, models_loaded=bool(made)):
                    log.info("stop requested; leaving")
                    return 0
                idle_release_s = await idle_release(conn)

            if made and time.monotonic() - idle_since > idle_release_s:
                release_pipelines()
                async with store.acquire() as conn:
                    await worker.seen(conn, who, models_loaded=False)
                log.info("idle %.0fs; released the models", idle_release_s)

            await asyncio.sleep(POLL)
    except KeyboardInterrupt:
        log.info("stopping; the job keeps its lease until it expires")
        return 130
    finally:
        release_pipelines()
        async with store.acquire() as conn:
            await worker.retire(conn, who)
        await lock.close()
        await store.close()


def run() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="take at most one job, then exit")
    return asyncio.run(main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(run())
