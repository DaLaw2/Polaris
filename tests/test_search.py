"""Search: tags, facets, similar, hybrid, one work and stats; rows only.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_search.py
"""
import asyncio
import inspect

from _common import dsn, member, observed

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.models.backends.base import RawScore  # noqa: E402
from polaris.observation import ingest  # noqa: E402
from polaris.search import api  # noqa: E402
from polaris.search.engine import SearchEngine  # noqa: E402
from polaris.vocabulary.service import EntityManager  # noqa: E402

COLL = "st-test"
TAG = {"test": "search"}


def call(fn, **kw):
    """Call an endpoint directly, its Query defaults filled in."""
    return fn(**{n: getattr(p.default, "default", p.default)
                 for n, p in inspect.signature(fn).parameters.items()
                 if n not in kw}, **kw)


async def clean(conn):
    await conn.execute("DELETE FROM works WHERE content_key LIKE 'st-test:%'")
    await conn.execute("DELETE FROM scan_runs WHERE params @> $1::jsonb", '{"test": "search"}')
    await conn.execute(
        "DELETE FROM model_profile_members WHERE model_version_id IN "
        "(SELECT id FROM model_versions WHERE backend = 'fake')")
    await conn.execute("DELETE FROM collections WHERE name = $1", COLL)


def names(results):
    return [r.work.folder_name for r in results]


async def main():
    await state.store.connect()
    state.entity_mgr = EntityManager(state.store)
    await state.entity_mgr.init_schema()
    state.engine = SearchEngine(state.store, vocabulary=state.entity_mgr)
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
            before = (await api.stats())["total_works"]
            await conn.execute("INSERT INTO collections (name, root, searchable) "
                               "VALUES ($1, '/st-test', TRUE)", COLL)
            fake = await member(conn, "fake", ["general", "character", "embedding"])
            run = await ingest.begin_run(conn, TAG)
            for key, scores, vec in (
                    ("a", [RawScore("general", "st-red", 0.9),
                           RawScore("character", "st-hero", 0.9)], [1, 0, 0, 0]),
                    ("b", [RawScore("general", "st-red", 0.9),
                           RawScore("general", "st-blue", 0.9)], [0.9, 0.1, 0, 0]),
                    ("c", [RawScore("general", "st-blue", 0.9)], [0, 1, 0, 0])):
                obs = observed(key, scores, COLL)
                obs.scores[0]["fake"].embedding = [float(x) for x in vec]
                await ingest.record_observation(conn, run, obs, {"fake": fake})
            await ingest.finish_run(conn, run)
        a = f"/{COLL}/a"

        for q, mode, want in (("st-red", "AND", {"a", "b"}),
                              ("st-red st-blue", "AND", {"b"}),
                              ("st-red st-blue", "OR", {"a", "b", "c"})):
            got = await api.search(q=q, mode=mode, limit=50, collection=COLL)
            assert set(names(got)) == want, (q, mode, names(got))
        print("  ok  tag search ANDs and ORs")

        out = await call(api.search_faceted, q="st-red", exclude="st-blue",
                         collection=COLL)
        assert names(out["results"]) == ["a"] and out["total"] == 1, out
        out = await call(api.search_faceted, character="st-hero", collection=COLL)
        assert names(out["results"]) == ["a"], out
        out = await call(api.search_faceted, collection=COLL)
        general = {f["value"]: f["count"] for f in out["facets"]["general"]}
        assert general == {"st-red": 2, "st-blue": 2}, general
        assert out["facets"]["collection"] == [{"value": COLL, "count": 3}]
        assert {f["value"] for f in out["facets"]["character"]} == {"st-hero"}
        print("  ok  faceted search filters, excludes and counts facets")

        near = await call(api.similar, path=a, collection=COLL)
        assert names(near) == ["b", "c"] and near[0].score > near[1].score, near
        assert await call(api.similar, path="/nowhere", collection=COLL) == []
        mixed = await call(api.hybrid, q="st-blue", similar_to=a, collection=COLL)
        assert names(mixed) == ["b", "c"], names(mixed)
        print("  ok  similar ranks by vector without the reference; hybrid filters then ranks")

        work = await api.get_work(path=a)
        assert work.folder_name == "a" and work.collection == COLL, work
        assert set(work.general_tags) == {"st-red"} and set(work.character_tags) == {"st-hero"}
        assert await api.get_work(path="/nowhere") is None
        assert (await api.stats())["total_works"] == before + 3
        print("  ok  one work reads whole, a missing one is null, stats count it")

        async with state.store.acquire() as conn:
            await clean(conn)
        print("all search checks passed")
    finally:
        await state.store.close()


asyncio.run(main())
