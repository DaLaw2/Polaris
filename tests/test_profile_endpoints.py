"""A model profile is built, filled, switched to and removed from the page.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_profile_endpoints.py
"""
import asyncio

from _common import define, dsn, refused, run_derives

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.derivation import derive  # noqa: E402
from polaris.jobs import worker  # noqa: E402
from polaris.models import api  # noqa: E402
from polaris.observation import api as observation_api  # noqa: E402
from polaris.shared import tuning  # noqa: E402

NEW, COPY = "profile-test-new", "profile-test-copy"
KEY = "profile-test:1"
Role, Member, Edit = api.RoleRequest, api.MemberRequest, api.ProfileEdit


async def clean(conn) -> None:
    ids = [r["id"] for r in await conn.fetch(
        "SELECT id FROM model_profiles WHERE name LIKE 'profile-test-%'")]
    await conn.execute("DELETE FROM scan_jobs WHERE model_profile_id = ANY($1::smallint[])", ids)
    await conn.execute("DELETE FROM model_profiles WHERE id = ANY($1::smallint[])", ids)
    await conn.execute("DELETE FROM works WHERE content_key = $1", KEY)
    await conn.execute(
        "DELETE FROM model_profile_members m USING model_versions v "
        "WHERE v.id = m.model_version_id AND v.backend = 'animetimm' "
        "AND m.profile_id = (SELECT id FROM model_profiles WHERE name = 'default')")
    await conn.execute(
        "DELETE FROM model_versions WHERE backend = ANY($1::text[]) AND NOT EXISTS "
        "(SELECT 1 FROM model_profile_members m WHERE m.model_version_id = model_versions.id)",
        ["animetimm", "canary"])
    await conn.execute(
        "DELETE FROM model_definitions d WHERE name = ANY($1::text[]) AND NOT EXISTS "
        "(SELECT 1 FROM model_versions v WHERE v.definition_id = d.id)", ["animetimm", "canary"])


async def listed() -> dict:
    return {p["name"]: p for p in (await api.list_model_profiles())["profiles"]}


async def members(name) -> dict:
    return {m["backend"]: m for m in (await listed())[name]["members"]}


async def count(sql, *args):
    async with state.store.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def main() -> None:
    await state.store.connect()
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
        await define("animetimm", "canary")
        async with state.store.acquire() as conn:
            await conn.execute("INSERT INTO works (content_key) VALUES ($1)", KEY)
            await conn.execute(
                "INSERT INTO model_versions (backend, revision, precision, definition_id) "
                "SELECT name, spec #>> '{source,revision}', 'fp32', max(id) "
                "FROM model_definitions WHERE name = ANY($1::text[]) "
                "GROUP BY name, spec #>> '{source,revision}' ON CONFLICT DO NOTHING",
                ["animetimm", "canary"])
            base = await conn.fetchval("SELECT id FROM model_profiles WHERE active")

        models = (await api.list_models())["models"]
        assert {"animetimm", "canary"} <= {m["name"] for m in models}
        assert all("categories" in m and "note" in m for m in models)
        made = await api.create_model_profile(api.ProfileRequest(name=NEW))
        pid = made["id"]
        seeded = await count("SELECT COUNT(*) FROM model_profile_params "
                             "WHERE profile_id = $1", pid)
        fresh = (await listed())[NEW]
        assert seeded == len(tuning.PROFILE_PARAMS)
        assert fresh["members"] == [] and fresh["works"] == 0
        assert not fresh["active"] and not fresh["maintained"]
        await refused(api.create_model_profile(api.ProfileRequest(name=NEW)), 409)
        print(f"  ok  a new profile starts empty with {seeded} parameters; a name twice is refused")

        await api.put_model_profile_member(pid, "animetimm", Member(rank=0, roles=[
            Role(role="general", score_cutoff=0.4), Role(role="embedding")]))
        await api.put_model_profile_member(pid, "canary", Member(rank=1, roles=[
            Role(role="character", yields=True)]))
        got = await members(NEW)
        assert [r["role"] for r in got["animetimm"]["roles"]] == ["embedding", "general"]
        assert got["animetimm"]["roles"][1]["score_cutoff"] == 0.4
        assert got["canary"]["roles"][0]["yields"] and got["canary"]["state"] == "ready"
        await refused(api.put_model_profile_member(pid, "invented", Member()), 404)
        await refused(api.put_model_profile_member(
            pid, "animetimm", Member(roles=[Role(role="mood")])), 400)
        await api.put_model_profile_member(pid, "animetimm", Member(
            rank=2, roles=[Role(role="general")]))
        again = (await members(NEW))["animetimm"]
        assert again["rank"] == 2 and [r["role"] for r in again["roles"]] == ["general"]
        print("  ok  members keep roles, cutoff and yields; writing again replaces; unknowns refused")

        await api.put_model_profile_member(pid, "canary", Member(rank=1, roles=[
            Role(role="rating"), Role(role="embedding")]))
        taken = await refused(api.put_model_profile_member(pid, "animetimm", Member(
            rank=2, roles=[Role(role="general"), Role(role="embedding")])), 409, "role_taken")
        assert taken["backend"] == "canary", taken
        await refused(api.put_model_profile_member(
            pid, "animetimm", Member(roles=[Role(role="rating")])), 409)
        assert (await api.delete_model_profile_member(pid, "canary"))["deriving"] is None
        assert set(await members(NEW)) == {"animetimm"}
        assert await count("SELECT COUNT(*) FROM model_profile_roles r JOIN model_versions v "
                           "ON v.id = r.model_version_id WHERE r.profile_id = $1 "
                           "AND v.backend = 'canary'", pid) == 0
        await refused(api.delete_model_profile_member(pid, "canary"), 404)
        print("  ok  rating and embedding have one member each; removing a member takes its roles")

        async with state.store.acquire() as conn:
            await tuning.set_param(conn, "score:general", 0.77, pid)
        copied = await api.create_model_profile(api.ProfileRequest(name=COPY, copy_from=NEW))
        clone = (await listed())[COPY]["members"]
        assert [(m["backend"], [r["role"] for r in m["roles"]]) for m in clone] == [
            ("animetimm", ["general"])]
        assert float(await count("SELECT value FROM model_profile_params WHERE profile_id = $1 "
                                 "AND name = 'score:general'", copied["id"])) == 0.77
        renamed = await api.edit_model_profile(copied["id"], Edit(name="profile-test-renamed"))
        assert renamed["name"] == "profile-test-renamed"
        await refused(api.edit_model_profile(copied["id"], Edit(name=NEW)), 409)
        print("  ok  a copy carries members, roles and tuned parameters; renames avoid taken names")

        await refused(api.edit_model_profile(pid, Edit(active=True)), 409)
        await refused(api.edit_model_profile(pid, Edit(maintained=True, active=True)),
                      409, "profile_needs_embedding")
        both = Member(rank=2, roles=[Role(role="general"), Role(role="embedding")])
        assert (await api.put_model_profile_member(pid, "animetimm", both))["deriving"] is None
        out = await api.edit_model_profile(pid, Edit(maintained=True))
        assert out["deriving"] is not None, out
        assert await count("SELECT COUNT(*) FROM scan_jobs WHERE kind = 'derive' "
                           "AND state = 'queued' AND model_profile_id = $1", pid) == 1
        await refused(api.edit_model_profile(pid, Edit(active=True)), 409, "profile_not_derived")
        joined = await api.put_model_profile_member(pid, "animetimm", Member(rank=2, roles=[
            Role(role="general", score_cutoff=0.45), Role(role="embedding")]))
        assert joined["deriving"] == out["deriving"], joined
        print("  ok  activation needs one embedding model and a finished derive; edits join the queued one")

        assert pid in await run_derives("profile-test")
        await api.edit_model_profile(pid, Edit(active=True))
        assert await count("SELECT active_profile_id()") == pid
        assert await count("SELECT COUNT(*) FROM work_search") == await count(
            "SELECT COUNT(*) FROM works")
        for edit in (Edit(active=False), Edit(maintained=False)):
            await refused(api.edit_model_profile(pid, edit), 409)
        await refused(api.delete_model_profile(pid), 409)
        print("  ok  a derived profile goes live and cannot be deleted, switched off or unmaintained")

        async with state.store.acquire() as conn:
            for table, cols in (("model_profile_members", "model_version_id, 0"),
                                ("model_profile_roles", "model_version_id, role")):
                await conn.execute(
                    f"INSERT INTO {table} SELECT $1, {cols} FROM model_profile_roles "
                    "WHERE profile_id = $2 AND role = 'embedding' ON CONFLICT DO NOTHING",
                    base, pid)
            await derive.refresh_derived(conn, base)
        await api.edit_model_profile(base, Edit(active=True))
        await api.edit_model_profile(pid, Edit(maintained=False))
        derived = "SELECT COUNT(*) FROM derived.work_search WHERE profile_id = $1"
        assert await count(derived, pid) == 0
        async with state.store.acquire() as conn:
            await derive.refresh_derived(conn, pid)
        assert await count(derived, pid) == 0
        await refused(api.edit_model_profile(pid, Edit(maintained=True, active=True)), 409)
        print("  ok  unmaintaining drops what it derived, and a late derive batch writes nothing")

        async with state.store.acquire() as conn:
            waiting = await worker.enqueue(conn, "scan", params={}, model_profile_id=pid)
        held = await refused(api.delete_model_profile(pid), 409, "profile_has_jobs")
        assert held["jobs"] == 1, held
        async with state.store.acquire() as conn:
            await worker.request_cancel(conn, waiting)
        history = await count("SELECT COUNT(*) FROM scan_jobs WHERE model_profile_id = $1", pid)
        assert history >= 2, history
        assert (await api.delete_model_profile(pid))["removed"] is True
        assert await count(derived, pid) == 0
        assert await count("SELECT COUNT(*) FROM scan_jobs WHERE id = $1 "
                           "AND model_profile_id IS NULL", waiting) == 1
        await refused(observation_api.retry_scan_job(waiting), 409, "job_profile_deleted")
        async with state.store.acquire() as conn:
            await conn.execute("DELETE FROM scan_jobs WHERE id = $1", waiting)
        await refused(api.delete_model_profile(pid), 404)
        print("  ok  an unfinished job keeps a profile; deleted, its finished jobs stay unretryable")

        async with state.store.acquire() as conn:
            await clean(conn)
        print("all model profile endpoint checks passed")
    finally:
        await state.store.close()


asyncio.run(main())
