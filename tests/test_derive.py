"""Derived rows follow every write, per model profile; rows only, no models.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_derive.py
"""
import asyncio
import json
import math

from _common import dsn, member, observed

DSN = dsn()

import asyncpg  # noqa: E402

from polaris import state  # noqa: E402
from polaris.derivation import derive  # noqa: E402
from polaris.models import profiles  # noqa: E402
from polaris.models.backends.base import RawScore  # noqa: E402
from polaris.observation import ingest  # noqa: E402
from polaris.observation.domain import Measurement  # noqa: E402
from polaris.vocabulary import terms  # noqa: E402
from polaris.vocabulary.service import EntityManager  # noqa: E402

COLL = "derive-test"
TAG = {"test": "test_derive"}
ROLES = ("general", "character", "copyright", "artist", "rating")


def seen(key, scores=None, **kw):
    return observed(key, scores, COLL, **kw)


async def clean(conn):
    ids = [r["id"] for r in await conn.fetch(
        "SELECT id FROM works WHERE content_key LIKE 'derive-test:%'")]
    await conn.execute("DELETE FROM work_claims WHERE work_id = ANY($1::int[])", ids)
    await conn.execute("DELETE FROM works WHERE id = ANY($1::int[])", ids)
    await conn.execute("DELETE FROM scan_runs WHERE params @> $1::jsonb", json.dumps(TAG))
    await conn.execute("DELETE FROM term_map WHERE concept_id IN "
                       "(SELECT id FROM concepts WHERE slug LIKE 'derive-test%')")
    await conn.execute("UPDATE concepts SET parent_id = NULL WHERE slug LIKE 'derive-test%'")
    await conn.execute("DELETE FROM concepts WHERE slug LIKE 'derive-test%'")
    for table in ("model_profile_members", "derived.tag_thresholds"):
        await conn.execute(
            f"DELETE FROM {table} WHERE model_version_id IN "
            "(SELECT id FROM model_versions WHERE backend IN ('fake', 'fake2'))")
    await conn.execute("DELETE FROM collections WHERE name = $1", COLL)


async def tags(conn, work_id):
    return {(r["category"], r["tag"]) for r in await conn.fetch(
        "SELECT category, tag FROM effective_tags WHERE work_id = $1", work_id)}


async def runs(conn, work_id):
    return {r["run_id"] for r in await conn.fetch(
        "SELECT DISTINCT run_id FROM work_samples WHERE work_id = $1", work_id)}


async def snapshot(conn, ids):
    return [[tuple(r) for r in await conn.fetch(sql, ids)] for sql in (
        "SELECT * FROM work_tags WHERE work_id = ANY($1::int[]) ORDER BY work_id, category, tag",
        "SELECT * FROM work_model_claims WHERE work_id = ANY($1::int[]) ORDER BY 1, 2, 3",
        "SELECT * FROM work_search WHERE id = ANY($1::int[]) ORDER BY id")]


async def main():
    await state.store.connect()
    mgr = EntityManager(state.store)
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
            await conn.execute("INSERT INTO collections (name, root, searchable) "
                               "VALUES ($1, '/derive-test', FALSE)", COLL)
            fake = await member(conn, "fake", ROLES)
            series = "SELECT series FROM work_search WHERE id = $1"
            current = "SELECT run_id FROM work_search WHERE id = $1"

            r1 = await ingest.begin_run(conn, TAG)
            a = await ingest.record_observation(conn, r1, seen("a", [RawScore("general", "tag-x", 0.9)]))
            assert await tags(conn, a) == {("general", "tag-x")}
            r2 = await ingest.begin_run(conn, TAG)
            await ingest.record_observation(conn, r2, seen("a", [RawScore("general", "tag-y", 0.9)]))
            assert await tags(conn, a) == {("general", "tag-y")} and await runs(conn, a) == {r2}
            print("  ok  a rescan replaces the tags and deletes the earlier run")

            r3 = await ingest.begin_run(conn, TAG)
            await ingest.record_observation(conn, r3, seen("a"))
            assert await tags(conn, a) == {("general", "tag-y")}
            assert await runs(conn, a) == {r2, r3} and await conn.fetchval(current, a) == r2
            r4 = await ingest.begin_run(conn, TAG)
            await ingest.record_observation(conn, r4, seen(
                "a", samples=0, measurements=[Measurement("page_census", {"files": 3}, None)]))
            assert await conn.fetchval(current, a) == r4 and await runs(conn, a) == {r2}
            assert await tags(conn, a) == {("general", "tag-y")}
            assert await conn.fetchval("SELECT total_images FROM work_search WHERE id = $1", a) == 3
            print("  ok  an unscored run keeps the scored one; runs nothing cites are deleted")

            b = await ingest.record_observation(conn, r4, seen("b", [
                RawScore("general", "foo", 0.9), RawScore("character", "foo", 0.8),
                RawScore("general", "foo", 0.95)]))
            assert await conn.fetchval("SELECT count(*) FROM model_tags "
                                       "WHERE backend = 'fake' AND name = 'foo'") == 2
            top = await conn.fetchval("SELECT avg_score FROM work_tags WHERE work_id = $1 "
                                      "AND category = 'general' AND tag = 'foo'", b)
            assert math.isclose(top, 0.95, rel_tol=1e-6), top
            assert await tags(conn, b) == {("general", "foo"), ("character", "foo")}
            print("  ok  one name in two categories stays two tags; a repeat keeps the stronger")

            try:
                await ingest.record_observation(conn, r4, seen("c", [RawScore("general", "logit", 1.5)]))
                raise AssertionError("a score above 1 was stored")
            except asyncpg.CheckViolationError:
                pass
            assert not await conn.fetchval("SELECT count(*) FROM works "
                                           "WHERE content_key = 'derive-test:c'")
            print("  ok  a score outside 0..1 is refused and nothing of the work is written")

        removal = await mgr.add_work_override(work_id=b, action="remove",
                                              tag_category="general", original_tag="foo")
        await mgr.add_work_override(work_id=b, action="add", tag_category="general", new_tag="bar")
        async with state.store.acquire() as conn:
            assert await tags(conn, b) == {("character", "foo"), ("general", "bar")}
            assert await conn.fetchval("SELECT confidence FROM effective_tags "
                                       "WHERE work_id = $1 AND tag = 'bar'", b) == 1.0
            assert await conn.fetchval("SELECT freq_ratio FROM work_tags WHERE work_id = $1 "
                                       "AND category = 'general' AND tag = 'foo'", b) == 1.0
        await mgr.delete_work_override(removal)
        async with state.store.acquire() as conn:
            assert ("general", "foo") in await tags(conn, b)
            print("  ok  a correction shows at once and leaves the model's numbers alone")

            cid = await terms.concept(conn, "character", "derive-test-char")
            await terms.map_term(conn, "foo", cid)
            await derive.derive_works(conn, await derive.works_naming(conn, ["foo"]))
            assert await conn.fetchval("SELECT count(*) FROM work_model_claims WHERE work_id = $1 "
                                       "AND field = 'character' AND concept_id = $2", b, cid) == 1
            aid = await terms.concept(conn, "artist", "derive-test-artist")
            await terms.map_term(conn, "foo", aid)
            await derive.derive_works(conn, [b])
            assert not await conn.fetchval("SELECT count(*) FROM work_model_claims "
                                           "WHERE work_id = $1 AND field = 'artist'", b)
            print("  ok  a mapped term re-derives its works; a general tag names no artist")

        negation = await mgr.add_work_override(work_id=b, action="remove",
                                               tag_category="character",
                                               original_tag="derive-test-char")
        async with state.store.acquire() as conn:
            assert ("character", "foo") not in await tags(conn, b)
            assert not await conn.fetchval("SELECT count(*) FROM work_claims_multi "
                                           "WHERE work_id = $1 AND field = 'character'", b)
        await mgr.delete_work_override(negation)
        other = await mgr.add_work_override(work_id=b, action="add", tag_category="character",
                                            new_tag="derive-test-other")
        async with state.store.acquire() as conn:
            assert ("character", "foo") in await tags(conn, b)
            oid = await conn.fetchval("SELECT id FROM concepts WHERE kind = 'character' "
                                      "AND slug = 'derive-test-other'")
            assert {r["concept_id"] for r in await conn.fetch(
                "SELECT concept_id FROM work_claims_multi WHERE work_id = $1 "
                "AND field = 'character'", b)} == {cid, oid}
            print("  ok  a negated character leaves tags and claims; a person's joins the model's")

            s1 = await terms.concept(conn, "series", "derive-test-series")
            s2 = await terms.concept(conn, "series", "derive-test-series2")
            for concept, parent, want in ((cid, s1, "derive-test-series"),
                                          (oid, s2, "derive-test-series2")):
                await conn.execute("UPDATE concepts SET parent_id = $2 WHERE id = $1",
                                   concept, parent)
                await derive.derive_works(conn, [b])
                assert await conn.fetchval(series, b) == want
        no_s2 = await mgr.add_work_override(work_id=b, action="remove", tag_category="copyright",
                                            original_tag="derive-test-series2")
        async with state.store.acquire() as conn:
            assert await conn.fetchval(series, b) == "derive-test-series"
        await mgr.delete_work_override(no_s2)
        await mgr.delete_work_override(other)
        async with state.store.acquire() as conn:
            assert await conn.fetchval(series, b) == "derive-test-series"
            print("  ok  a series follows the characters, a person's first; a negated one falls back")

            fake2 = await member(conn, "fake2", ["general"], yields=True)
            await conn.execute(
                "INSERT INTO derived.tag_thresholds (model_version_id, category, tag, "
                "threshold, source) VALUES ($1, 'general', 'shared', 0.99, 'test'), "
                "($1, 'character', 'only2', 0.99, 'test')", fake)
            r5 = await ingest.begin_run(conn, TAG)
            d = await ingest.record_observation(conn, r5, seen("d", {
                "fake": [RawScore("general", "mid", 0.9)],
                "fake2": [RawScore("general", "shared", 0.9),
                          RawScore("general", "only2", 0.9)]}), {"fake": fake, "fake2": fake2})
            assert await tags(conn, d) == {("general", "mid"), ("general", "only2")}
            await conn.execute("UPDATE model_profile_roles SET yields = FALSE "
                               "WHERE model_version_id = $1", fake2)
            await derive.derive_works(conn, [d])
            assert ("general", "shared") in await tags(conn, d)
            print("  ok  a yielding member gives way only where another publishes in the category")

            for cutoff, present in ((0.95, False), (None, True)):
                await conn.execute("UPDATE model_profile_roles SET score_cutoff = $2 "
                                   "WHERE model_version_id = $1 AND role = 'general'",
                                   fake, cutoff)
                await derive.derive_works(conn, [d])
                assert (("general", "mid") in await tags(conn, d)) == present
            print("  ok  a role's score cutoff outranks the profile's")

            before = await snapshot(conn, [a, b, d])
            await derive.refresh_derived(conn)
            assert await snapshot(conn, [a, b, d]) == before
            print("  ok  deriving one work agrees with deriving them all")

            tr = conn.transaction()
            await tr.start()
            await conn.execute("UPDATE model_profiles SET active = FALSE")
            try:
                await conn.fetchval("SELECT active_profile_id()")
                raise AssertionError("no active profile went unnoticed")
            except asyncpg.RaiseError as e:
                assert "no model profile is active" in str(e)
            await tr.rollback()
            assert await profiles.active_profile(conn) is not None
            print("  ok  with no active profile the views refuse instead of guessing")

            await clean(conn)
        print("all derive checks passed")
    finally:
        await state.store.close()


asyncio.run(main())
