"""POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_build_job.py"""
import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from _common import dsn

DSN = dsn()
os.environ["POLARIS_ONNX_PROVIDER"] = "CPUExecutionProvider"

import numpy as np  # noqa: E402
import onnx.parser  # noqa: E402
from PIL import Image  # noqa: E402

from polaris import state  # noqa: E402
from polaris.jobs import worker  # noqa: E402
from polaris.models import api  # noqa: E402
from polaris.models.backends.base import TagThreshold  # noqa: E402
from polaris.observation import building  # noqa: E402

NAME = COLL = "build-test"
TAG = '{"test": "build-test"}'


def model(folder: Path, tags: int) -> str:
    """An 8x8 image to 5 scores and a 3-wide embedding, `tags` names; its definition."""
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    m = onnx.parser.parse_model("""<ir_version: 10, opset_import: ["" : 17]>
tiny (float[N,3,8,8] x) => (float[N,5] logits, float[N,3] emb)
{ f = Flatten(x)  logits = MatMul(f, ws)  emb = MatMul(f, we) }""")
    m.graph.initializer.extend([
        onnx.numpy_helper.from_array(rng.normal(size=(192, 5)).astype(np.float32) / 50, "ws"),
        onnx.numpy_helper.from_array(rng.normal(size=(192, 3)).astype(np.float32), "we")])
    onnx.save(m, str(folder / "m.onnx"))
    (folder / "tags.csv").write_text("name\n" + "".join(f"t{i}\n" for i in range(tags)))
    return f"""name = "{NAME}"
source = {{ path = "{folder.as_posix()}", model = "m.onnx" }}
preprocess = [{{op = "resize", size = "model", interpolation = "bilinear"}}, {{op = "to_tensor"}}]
outputs = {{ scores = "logits", activation = "sigmoid", embedding = "emb" }}
vocabulary = {{file = "tags.csv", format = "csv", name = "name", categories = {{"*" = "general"}}}}
thresholds = {{ kind = "constant", value = {{ general = 0.5 }} }}
defaults = {{ roles = {{ general = 1 }} }}
"""


async def clean(conn) -> None:
    await conn.execute(f"""
DELETE FROM scan_jobs WHERE model_version_id IN
    (SELECT id FROM model_versions WHERE backend = '{NAME}');
DELETE FROM model_versions WHERE backend = '{NAME}';
DELETE FROM model_definitions WHERE name = '{NAME}';
DELETE FROM works WHERE content_key LIKE '{NAME}:%';
DELETE FROM scan_runs WHERE params @> '{TAG}'; DELETE FROM collections WHERE name = '{COLL}';""")


async def build(text: str) -> tuple[int, str, dict]:
    """Upload a definition, build it, run the job; the version as it ends."""
    up = await api.upload_model_definition(api.DefinitionUpload(text=text))
    b = await api.build_model_version(up["id"], api.BuildRequest(precision="fp32"))
    async with state.store.acquire() as conn:
        job = await worker.claim(conn, "build-test")
        assert job["id"] == b["job_id"], (job["id"], b)
        outcome = await worker.run_job(conn, job, building.job_body(lambda: None))
        row = dict(await conn.fetchrow(
            "SELECT v.state, v.drift, v.embedding_dim, v.error, j.error AS job_error, "
            "(SELECT count(*) FROM derived.tag_thresholds t "
            " WHERE t.model_version_id = v.id) AS thresholds "
            "FROM model_versions v, scan_jobs j WHERE v.id = $1 AND j.id = $2",
            b["model_version_id"], job["id"]))
    return b["model_version_id"], outcome, row


async def pages(conn, folder: Path, n: int) -> None:
    """A scan's worth of recorded page samples, `n` of them on disk."""
    folder.mkdir(parents=True)
    for i, a in enumerate(np.random.default_rng(2).integers(0, 256, (n, 64, 48, 3), np.uint8)):
        Image.fromarray(a).save(folder / f"{i:03}.png")
    await conn.execute(
        "WITH c AS (INSERT INTO collections (name, root) VALUES ($1, $2)), "
        "w AS (INSERT INTO works (content_key) VALUES ('build-test:1') RETURNING id), "
        "p AS (INSERT INTO work_paths (work_id, path, collection) "
        "      SELECT id, $3, $1 FROM w), "
        "r AS (INSERT INTO scan_runs (params) VALUES ($4::jsonb) RETURNING id) "
        "INSERT INTO work_samples (run_id, work_id, medium, source_relpath, "
        "position, ordinal) SELECT r.id, w.id, 'image', lpad(i::text, 3, '0') || "
        "'.png', i, i FROM r, w, generate_series(0, $5) i",
        COLL, str(folder.parent), str(folder), TAG, n + 1)


def check_flips(paths) -> None:
    """Over 4% of the tags over threshold flipping fails; so does a NaN."""
    def session(preds):
        return SimpleNamespace(_names=[f"t{i}" for i in range(5)],
                               _idx=[("general", np.arange(5))], prepare=lambda image: 0,
                               _predict=lambda arrays: (preds, None))
    published = [TagThreshold("general", f"t{i}", 0.5) for i in range(5)]
    want = np.full((building.PROBE_PAGES, 5), 0.6, dtype=np.float32)
    got = want.copy()
    got[0, 0] = 0.4
    def verify(precision):
        return building._verify(session(want), session(got), paths, published, precision)
    assert verify("fp16") == 1 / want.size
    for rows, cell, value, precision, msg in (
            (slice(0, 10), 1, 0.4, "fp16", "build FP32"), (0, 2, np.nan, "fp32", "NaN")):
        got[rows, cell] = value
        try:
            verify(precision)
            raise AssertionError(f"passed: {msg}")
        except ValueError as e:
            assert msg in str(e), e
    print("  ok  FP16 passes under 4% flipped, fails over it; NaN fails")


async def main() -> None:
    await state.store.connect()
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            good = model(tmp / "good", tags=5)
            vid, outcome, row = await build(good)
            assert outcome == "done" and (row["state"], row["drift"], row["error"],
                row["embedding_dim"], row["thresholds"]) == ("ready", None, None, 3, 5), row
            print("  ok  with no readable pages the version is ready, unverified")
            await api.delete_model_version(vid)
            async with state.store.acquire() as conn:
                await pages(conn, tmp / "lib" / "work", building.PROBE_PAGES)
            _, outcome, row = await build(good)
            assert outcome == "done" and row["state"] == "ready" and row["drift"] == 0.0, row
            print("  ok  recorded pages verify an FP32 build; missing files skipped")
            check_flips(sorted((tmp / "lib" / "work").glob("*.png")))
            async with state.store.acquire() as conn:
                await clean(conn)
            _, outcome, row = await build(model(tmp / "narrow", tags=4))
            assert outcome == "failed" and row["state"] == "failed", row
            assert "5 wide" in row["error"] and "4 tags" in row["error"], row
            assert "5 wide" in row["job_error"], row
            print("  ok  a score output wider than the vocabulary fails at validate")
        print("all build-job checks passed")
    finally:
        async with state.store.acquire() as conn:
            await clean(conn)
        await state.store.close()


asyncio.run(main())
