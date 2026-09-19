"""Shared by the tests: the scratch-database guard and a few fixtures."""
import asyncio
import os
import struct
import zlib


def dsn() -> str:
    """POLARIS_TEST_DSN with its schema booted; skip when unset, refuse a
    database whose name does not contain `test`. Call before importing polaris."""
    url = os.environ.get("POLARIS_TEST_DSN")
    if not url:
        print("POLARIS_TEST_DSN is not set; skipped.")
        raise SystemExit(0)
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name:
        raise SystemExit(f"refusing {name!r}: the scratch database's name "
                         "must contain 'test'")
    os.environ["POLARIS_DSN"] = url
    from polaris import config
    from polaris.shared.db import Store

    assert config.DATABASE_DSN == url, "polaris was imported before dsn()"

    async def boot():
        store = Store(url)
        await store.connect()
        await store.close()

    asyncio.run(boot())
    return url


async def refused(call, status: int, code: str | None = None):
    """Await a request that must be turned down; return its detail."""
    from fastapi import HTTPException

    try:
        await call
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        assert code is None or e.detail["code"] == code, e.detail
        return e.detail
    raise AssertionError(f"expected {status} {code or ''}")


async def member(conn, backend: str, roles=(), yields: bool = False) -> int:
    """Put `backend`'s version in the active profile with these roles."""
    from polaris.models import profiles

    version = await profiles.model_version(conn, backend)
    await conn.execute(
        "INSERT INTO model_profile_members (profile_id, model_version_id, rank) "
        "VALUES (active_profile_id(), $1, 1) ON CONFLICT DO NOTHING", version)
    for role in roles:
        await conn.execute(
            "INSERT INTO model_profile_roles "
            "(profile_id, model_version_id, role, yields) "
            "VALUES (active_profile_id(), $1, $2, $3) ON CONFLICT DO NOTHING",
            version, role, yields)
    return version


async def define(*names: str) -> None:
    """Upload a small definition under each name."""
    from polaris.models import api

    for name in names:
        await api.upload_model_definition(api.DefinitionUpload(text=f"""name = "{name}"
source = {{ path = "/{name}", model = "m.onnx" }}
preprocess = [{{ op = "resize", size = 8, interpolation = "bilinear" }}, {{ op = "to_tensor" }}]
outputs = {{ scores = "#0" }}
vocabulary = {{file = "t.csv", format = "csv", name = "name", categories = {{"*" = "general"}}}}
defaults = {{ roles = {{ general = 1 }} }}
"""))


def observed(key: str, scores, coll: str, samples: int = 1, measurements=()):
    """Work `key` at /coll/key; `scores` maps a backend to RawScores, and a
    bare list is the `fake` backend's."""
    from polaris.models.backends.base import SampleScores
    from polaris.observation.domain import Sample, WorkObservation

    obs = WorkObservation(path=f"/{coll}/{key}", name=key, collection=coll,
                          content_key=f"{coll}:{key}")
    obs.samples = [Sample("image", f"{i:03}.png", i, i) for i in range(samples)]
    if isinstance(scores, list):
        scores = {"fake": scores}
    per = {b: SampleScores(scores=list(s)) for b, s in (scores or {}).items()}
    obs.scores = [dict(per) for _ in range(samples)]
    obs.measurements = list(measurements)
    return obs


def png(w: int = 8, h: int = 1) -> bytes:
    """A real PNG that PIL can open."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes([(y * 7) % 256] * 3 * w) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


async def run_derives(who: str = "test") -> list[int]:
    """Run every queued derive job as the API's loop would; the profiles run."""
    from polaris import state
    from polaris.jobs import worker

    ran = []
    async with state.store.acquire() as conn:
        while job := await worker.claim(conn, who, kinds=("derive",)):
            assert await worker.run_job(conn, job, worker.derive_body) == "done"
            ran.append(job["model_profile_id"])
    return ran
