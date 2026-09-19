"""Changing a threshold is a query: moving the cutoff re-derives, no model runs.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_params_endpoints.py
"""
import asyncio

from _common import dsn, member, observed, refused, run_derives

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.derivation import api as derivation_api  # noqa: E402
from polaris.models import api as models_api  # noqa: E402
from polaris.models.backends.base import RawScore  # noqa: E402
from polaris.observation import ingest  # noqa: E402

LOUD, QUIET = "params-test-loud", "params-test-quiet"
BACKEND, COLL, OTHER = "params-test-fake", "params-test", "params-test-other"


def put(name, value, profile=None):
    return derivation_api.set_param(name, derivation_api.ParamRequest(value=value), profile)


async def clean(conn) -> None:
    await conn.execute("DELETE FROM scan_runs WHERE params->>'test' = 'params'")
    await conn.execute("DELETE FROM works WHERE content_key LIKE $1", COLL + ":%")
    await conn.execute("DELETE FROM scan_jobs WHERE kind = 'derive' AND state = 'queued'")
    await conn.execute(
        "DELETE FROM model_profile_members WHERE model_version_id IN "
        "(SELECT id FROM model_versions WHERE backend = $1)", BACKEND)
    await conn.execute("DELETE FROM model_versions WHERE backend = $1", BACKEND)
    await conn.execute("DELETE FROM collections WHERE name = $1", COLL)
    await conn.execute("DELETE FROM model_profiles WHERE name = $1", OTHER)


async def tags_of(work_id: int) -> set[str]:
    async with state.store.acquire() as conn:
        return {r["tag"] for r in await conn.fetch(
            "SELECT tag FROM work_tags WHERE work_id = $1", work_id)}


async def main() -> None:
    await state.store.connect()
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
            await conn.execute("INSERT INTO collections (name, root, searchable) "
                               "VALUES ($1, $2, FALSE)", COLL, "/" + COLL)
            was = float(await conn.fetchval(
                "SELECT value FROM model_profile_params WHERE profile_id = "
                "active_profile_id() AND name = 'score:general'"))

        listed = (await derivation_api.list_params())["params"]
        by_name = {p["name"]: p for p in listed}
        assert len(listed) > 20 and by_name["score:general"]["group"] == "score"
        assert by_name["score:general"]["profile"] and not by_name["sample:floor"]["profile"]
        assert by_name["worker:idle_release_s"]["value"] == 600.0
        effects = {n: by_name[n]["effect"] for n in (
            "score:general", "rating:score", "sample:floor", "check:near",
            "worker:idle_release_s")}
        assert effects == {"score:general": "rederive", "rating:score": "rederive",
                           "sample:floor": "next_scan", "check:near": "next_scan",
                           "worker:idle_release_s": "now"}, effects
        print(f"  ok  {len(listed)} numbers are listed, grouped, and say when a change is felt")

        other = (await models_api.create_model_profile(
            models_api.ProfileRequest(name=OTHER)))["id"]
        theirs = await put("score:general", 0.66, other)
        assert (theirs["profile_id"], theirs["value"], theirs["refresh"]) == (other, 0.66, "none")
        read = {p["name"]: p for p in (await derivation_api.list_params(other))["params"]}
        assert read["score:general"]["value"] == 0.66
        assert read["sample:floor"]["value"] == by_name["sample:floor"]["value"]
        mine = {p["name"]: p for p in (await derivation_api.list_params())["params"]}
        assert mine["score:general"]["value"] == was
        await refused(derivation_api.list_params(32000), 404, "profile_not_found")
        await models_api.delete_model_profile(other)
        await refused(put("score:invented", 0.5), 404, "param_not_found")
        print("  ok  another profile's numbers are its own; unknown profiles and names are refused")

        async with state.store.acquire() as conn:
            version = await member(conn, BACKEND, ["general"])
            run = await ingest.begin_run(conn, params={"test": "params"})
            work_id = await ingest.record_observation(conn, run, observed(
                "a work", {BACKEND: [RawScore("general", LOUD, 0.90),
                                     RawScore("general", QUIET, 0.20)]}, COLL),
                {BACKEND: version})
            await ingest.finish_run(conn, run)
        before = await tags_of(work_id)
        assert LOUD in before and QUIET not in before, before

        out = await put("score:general", 0.1)
        assert (out["value"], out["adjusted"], out["refresh"]) == (0.1, True, "queued"), out
        assert (await put("score:general", 0.1))["refresh"] == "queued"
        async with state.store.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM scan_jobs "
                                       "WHERE kind = 'derive' AND state = 'queued'") == 1
        assert await run_derives("params-test"), "setting a derived parameter queued nothing"
        assert QUIET in await tags_of(work_id), "lowering the cutoff surfaced nothing"
        print("  ok  two changes queue one derive job, and lowering the cutoff surfaces the quiet tag")

        sampled = await put("sample:floor", 9)
        assert sampled["refresh"] == "none" and sampled["profile"] is False
        await put("sample:floor", by_name["sample:floor"]["value"])
        back = await put("score:general", was)
        assert back["adjusted"] is False, back
        assert await run_derives("params-test")
        assert await tags_of(work_id) == before
        print(f"  ok  an observation parameter queues nothing; putting {was} back restores exactly")

        async with state.store.acquire() as conn:
            await clean(conn)
        print("all parameter endpoint checks passed")
    finally:
        await state.store.close()


asyncio.run(main())
