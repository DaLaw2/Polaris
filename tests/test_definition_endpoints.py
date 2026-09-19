"""POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_definition_endpoints.py"""
import asyncio

from _common import dsn, refused

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.models import api  # noqa: E402

NAME, PROFILE, KEY = "defn-test", "defn-test-profile", "defn-test:1"
SRC = 'path = "/defn-test/nowhere"'
TEXT = f"""name = "{NAME}"
note = "a test"
source = {{ {SRC}, model = "m.onnx" }}
preprocess = [{{ op = "resize", size = 8, interpolation = "bilinear" }}, {{ op = "to_tensor" }}]
outputs = {{ scores = "#0" }}
vocabulary = {{file = "tags.csv", format = "csv", name = "name", categories = {{"*" = "general"}}}}
defaults = {{ roles = {{ general = 1 }} }}
"""


async def clean(conn) -> None:
    await conn.execute(f"""
DELETE FROM model_profiles WHERE name = '{PROFILE}';
DELETE FROM works WHERE content_key = '{KEY}';
DELETE FROM scan_jobs WHERE model_version_id IN
    (SELECT id FROM model_versions WHERE backend = '{NAME}');
DELETE FROM model_versions WHERE backend = '{NAME}';
DELETE FROM model_definitions WHERE name = '{NAME}';""")


async def finish(*pairs) -> None:
    """What a build job would leave behind, per (version, outcome)."""
    async with state.store.acquire() as conn:
        for version, outcome in pairs:
            await conn.execute(
                "UPDATE scan_jobs SET state = 'failed', finished_at = NOW() "
                "WHERE model_version_id = $1 AND state IN ('queued', 'running')",
                version)
            await conn.execute("UPDATE model_versions SET state = $2 WHERE id = $1",
                               version, outcome)


def upload(text=TEXT):
    return api.upload_model_definition(api.DefinitionUpload(text=text))


def build(defn, **kw):
    return api.build_model_version(defn["id"], api.BuildRequest(**kw))


async def main() -> None:
    await state.store.connect()
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
        up = await upload()
        assert up["created"] and up["name"] == NAME, up
        assert await upload() == {**up, "created": False}
        for text, field in ((TEXT.replace(SRC, SRC + ', repo = "a/b"'), "source:"),
                            (TEXT.replace("size = 8", "size = -1"), "preprocess[0].size")):
            bad = await refused(upload(text), 422, "definition_invalid")
            assert bad["message"].startswith(field), bad
        mine = {d["id"]: d for d in (await api.list_model_definitions())["definitions"]}[up["id"]]
        assert (mine["versions"], mine["retracted_at"], mine["source"]["path"], mine["note"]
                ) == ([], None, "/defn-test/nowhere", "a test"), mine
        gone = await api.delete_model_definition(up["id"])
        assert gone["removed"] and not gone["retracted"], gone
        await refused(api.delete_model_definition(up["id"]), 404, "definition_not_found")
        up = await upload()
        print("  ok  upload parses, stores once, names a bad field; unbuilt ones delete")
        unpinned = await upload(TEXT.replace(SRC, 'repo = "a/b"'))
        await refused(build(unpinned), 409, "definition_unpinned")
        await api.delete_model_definition(unpinned["id"])
        b32 = await build(up)
        v32 = b32["model_version_id"]
        async with state.store.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT j.kind, j.state AS job, v.backend, v.revision, v.precision, "
                "v.state, v.definition_id FROM scan_jobs j JOIN model_versions v "
                "ON v.id = j.model_version_id WHERE j.id = $1", b32["job_id"])
        assert dict(row) == {"kind": "build", "job": "queued", "backend": NAME,
                             "revision": None, "precision": "fp32", "state": "building",
                             "definition_id": up["id"]}, row
        why = await refused(build(up), 409, "version_exists")
        assert why["state"] == "building", why
        v16 = (await build(up, precision="fp16"))["model_version_id"]
        assert v16 != v32
        await finish((v32, "failed"))
        retry = await build(up)
        assert retry["model_version_id"] == v32 and retry["job_id"] != b32["job_id"]
        await finish((v32, "ready"))
        why = await refused(build(up), 409, "version_exists")
        assert why["state"] == "ready", why
        print("  ok  a build queues its version once per key; failed rebuilds in place")
        pid = (await api.create_model_profile(api.ProfileRequest(name=PROFILE)))["id"]
        await finish((v32, "building"))
        for name, req, status, code in (
                (NAME, api.MemberRequest(), 409, "no_ready_version"),
                ("defn-test-none", api.MemberRequest(), 404, "model_not_found"),
                (NAME, api.MemberRequest(version_id=v16), 409, "version_not_ready")):
            await refused(api.put_model_profile_member(pid, name, req), status, code)
        await finish((v32, "ready"), (v16, "ready"))
        general = [api.RoleRequest(role="general")]
        for req in (api.MemberRequest(roles=general), api.MemberRequest(rank=3, roles=general)):
            assert (await api.put_model_profile_member(pid, NAME, req))["model_version_id"] == v16
        await api.delete_model_profile_member(pid, NAME)
        put = await api.put_model_profile_member(pid, NAME, api.MemberRequest(version_id=v32))
        assert put["model_version_id"] == v32, put
        mine = {m["name"]: m for m in (await api.list_models())["models"]}[NAME]
        assert mine["categories"] == {"general": 1} and mine["note"] == "a test"
        assert {(v["precision"], v["state"]) for v in mine["versions"]} == {
            ("fp32", "ready"), ("fp16", "ready")}, mine["versions"]
        print("  ok  a member takes the named, held, else newest ready version")
        out = await api.delete_model_definition(up["id"])
        assert out["retracted"] and not out["removed"], out
        await refused(build(up), 409, "definition_retracted")
        assert any(m["name"] == NAME and m["retracted"]
                   for m in (await api.list_models())["models"])
        print("  ok  a definition with versions is only retracted, and cannot be built")
        held = await refused(api.delete_model_version(v32), 409, "version_in_use")
        assert held["profiles"] == [PROFILE], held
        await api.delete_model_profile_member(pid, NAME)
        async with state.store.acquire() as conn:
            waiting = await conn.fetchval(
                "INSERT INTO scan_jobs (kind, params, model_version_id) "
                "VALUES ('build', '{}', $1) RETURNING id", v32)
        await refused(api.delete_model_version(v32), 409, "version_building")
        async with state.store.acquire() as conn:
            await conn.execute("UPDATE scan_jobs SET state = 'cancelled', "
                               "finished_at = NOW() WHERE id = $1", waiting)
            work, runs = await conn.fetchrow(
                "WITH w AS (INSERT INTO works (content_key) VALUES ($1) RETURNING id), "
                "r AS (INSERT INTO scan_runs SELECT FROM generate_series(1, 2) RETURNING id), "
                "s AS (INSERT INTO work_samples (run_id, work_id, medium, source_relpath, "
                "      position, ordinal) SELECT r.id, w.id, 'image', '0.png', 0, 0 "
                "      FROM r, w RETURNING id), "
                "c AS (INSERT INTO sample_scores (sample_id, model_version_id, tag_ids, "
                "      scores) SELECT id, $2, '{1}', '{0.5}' FROM s), "
                "m AS (INSERT INTO measurements (run_id, work_id, kind, value) "
                "      SELECT min(r.id), min(w.id), 'x', '1' FROM r, w), "
                "t AS (INSERT INTO derived.tag_thresholds (model_version_id, category, "
                "      tag, threshold, source) VALUES ($2, 'general', 't', 0.5, 'test')) "
                "SELECT (SELECT id FROM w), (SELECT array_agg(id ORDER BY id) FROM r)",
                KEY, v32)
        out = await api.delete_model_version(v32)
        assert out["removed"] and out["scores"] == 2 and out["works"] == 1, out
        async with state.store.acquire() as conn:
            assert [r["run_id"] for r in await conn.fetch(
                "SELECT run_id FROM work_samples WHERE work_id = $1", work)] == [runs[1]]
            assert not await conn.fetchval(
                "SELECT count(*) FROM measurements WHERE work_id = $1", work)
            for table in ("model_versions WHERE id", "scan_jobs WHERE model_version_id",
                          "derived.tag_thresholds WHERE model_version_id"):
                assert await conn.fetchval(
                    f"SELECT count(*) FROM {table} = $1", v32) == 0, table
            await conn.execute("DELETE FROM works WHERE id = $1", work)
            await conn.execute("DELETE FROM scan_runs WHERE id = ANY($1::bigint[])", runs)
        await refused(api.delete_model_version(v32), 404, "version_not_found")
        print("  ok  deleting a version is refused while held or building, then "
              "takes its scores, thresholds, jobs and the run nothing cites")
        print("all definition endpoint checks passed")
    finally:
        async with state.store.acquire() as conn:
            await clean(conn)
        await state.store.close()


asyncio.run(main())
