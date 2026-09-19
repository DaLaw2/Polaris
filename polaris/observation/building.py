"""Building a model version: fetch what its definition names, check the
model is what the definition says, convert it, and check the converted
model against float32 on pages the library already holds.

Each step is written to the job as it starts; the version itself only
says building, ready or failed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import numpy as np

from polaris.jobs import worker
from polaris.models import definition, profiles
from polaris.models.backends.generic import DefinedBackend

STEPS = ("download", "validate", "convert", "verify")
PROBE_PAGES = 32
MAX_FLIP = 0.04
DEFAULT_CUT = 0.35

PAGES_SQL = """
SELECT p.path, s.source_relpath FROM work_samples s
JOIN work_paths p ON p.work_id = s.work_id AND p.present
WHERE s.analyzed AND s.medium = 'image'
ORDER BY s.run_id DESC, s.id LIMIT $1
"""


class _Stopped(Exception):
    """The job was cancelled, or the worker was asked to leave."""


def _synthetic():
    from PIL import Image

    rng = np.random.default_rng(0)
    return Image.fromarray(rng.integers(0, 256, (300, 200, 3), dtype=np.uint8))


def _download(d: definition.Definition) -> list:
    """Every file the definition names. Returns its published thresholds."""
    for extra in d.source.extra:
        definition.fetch(d, extra)
    definition.fetch(d, d.source.model)
    definition.vocabulary(d)
    return definition.published_thresholds(d)


def _validate(d: definition.Definition):
    """A float32 session on the CPU that opens, resolves its input size and
    outputs, and scores one synthetic image as wide as the vocabulary.
    Returns it and the embedding width."""
    ref = DefinedBackend(d, half=False, provider="CPUExecutionProvider")
    ref.load()
    preds, embs = ref._predict([ref.prepare(_synthetic())])
    if preds.shape[1] != len(ref._names):
        raise ValueError(f"the score output is {preds.shape[1]} wide, the "
                         f"vocabulary has {len(ref._names)} tags")
    return ref, None if embs is None else int(embs.shape[1])


def _convert(d: definition.Definition, precision: str) -> DefinedBackend:
    """The session scans will run, converted and warmed up once, so a
    TensorRT engine is built now rather than on the first scan."""
    gpu = DefinedBackend(d, half=precision == "fp16")
    gpu.load()
    gpu._predict([gpu.prepare(_synthetic())])
    return gpu


def _pages(paths: list[Path]) -> list:
    from PIL import Image

    out = []
    for path in paths:
        try:
            img = Image.open(path)
            img.draft("RGB", (1024, 1024))
            out.append(img.convert("RGB"))
        except Exception:
            continue
        if len(out) == PROBE_PAGES:
            break
    return out


def _verify(ref, gpu, paths, published, precision) -> float | None:
    """No NaN or Inf; for FP16, the share of tags over threshold on one
    side only against float32 on the CPU. None when too few pages."""
    images = _pages(paths)
    if len(images) < PROBE_PAGES:
        return None
    arrays = [gpu.prepare(im) for im in images]
    got, embs = gpu._predict(arrays)
    if not (np.isfinite(got).all()
            and (embs is None or np.isfinite(embs).all())):
        raise ValueError("the model returns NaN or Inf")
    if precision == "fp32":
        return 0.0
    want, _ = ref._predict(arrays)
    cut = {(t.category, t.tag): t.threshold for t in published}
    idx = [i for _, ids in ref._idx for i in ids]
    thr = np.asarray([cut.get((c, ref._names[i]), DEFAULT_CUT)
                      for c, ids in ref._idx for i in ids], dtype=np.float32)
    a, b = got[:, idx] > thr, want[:, idx] > thr
    either = int((a | b).sum())
    drift = int((a ^ b).sum()) / either if either else 0.0
    if drift > MAX_FLIP:
        raise ValueError(f"FP16 moves {drift:.1%} of the tags over threshold "
                         f"against FP32 (at most {MAX_FLIP:.0%}); build FP32 "
                         f"instead")
    return drift


async def _blocking(conn, job_id: int, fn, *args):
    """`fn` on a thread, renewing the lease while it runs."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    stop = False
    while not task.done():
        await asyncio.wait({task}, timeout=worker.BEAT_EVERY)
        if not task.done():
            stop = await worker.beat(conn, job_id) or stop
    result = task.result()
    if stop:
        raise _Stopped
    return result


async def _build(conn, job_id: int, vid: int, d: definition.Definition,
                 precision: str, note) -> None:
    async def step(i, fn, *args):
        if await worker.beat(conn, job_id, done=i, total=len(STEPS),
                             current_path=STEPS[i]):
            raise _Stopped
        note(f"build {d.name} {precision}: {STEPS[i]}")
        return await _blocking(conn, job_id, fn, *args)

    published = await step(0, _download, d)
    ref, dim = await step(1, _validate, d)
    gpu = None
    try:
        gpu = await step(2, _convert, d, precision)
        paths = [Path(r["path"]) / r["source_relpath"]
                 for r in await conn.fetch(PAGES_SQL, PROBE_PAGES * 4)]
        drift = await step(3, _verify, ref, gpu, paths, published, precision)
    finally:
        ref.unload()
        if gpu is not None:
            gpu.unload()
    await profiles.sync_thresholds(conn, [gpu], {d.name: vid})
    await conn.execute(
        "UPDATE model_versions SET state = 'ready', embedding_dim = $2, "
        "drift = $3, error = NULL WHERE id = $1", vid, dim, drift)
    note(f"build {d.name} {precision}: ready"
         + ("" if drift is None else f", drift {drift:.2%}"))


def job_body(release: Callable[[], None],
             on_note: Callable[[str], None] | None = None):
    """The callable `worker.run_job` runs for a build. `release` lets go of
    the worker's resident models first."""
    def note(msg: str) -> None:
        if on_note is not None:
            on_note(msg)

    async def body(conn, job) -> None:
        vid = job["model_version_id"]
        row = await conn.fetchrow(
            "SELECT v.precision, d.source_text FROM model_versions v "
            "LEFT JOIN model_definitions d ON d.id = v.definition_id "
            "WHERE v.id = $1", vid)
        await conn.execute(
            "UPDATE model_versions SET state = 'building', error = NULL "
            "WHERE id = $1", vid)
        try:
            if row is None or row["source_text"] is None:
                raise ValueError(f"model version {vid} has no definition")
            release()
            await _build(conn, job["id"], vid,
                         definition.parse(row["source_text"]),
                         row["precision"], note)
        except Exception as e:
            error = ("stopped before it finished" if isinstance(e, _Stopped)
                     else f"{type(e).__name__}: {e}")
            await conn.execute(
                "UPDATE model_versions SET state = 'failed', error = $2 "
                "WHERE id = $1", vid, error)
            if isinstance(e, _Stopped):
                raise RuntimeError(error) from None
            raise

    return body
