"""Works page and model tools: claims, list, explain, coverage, overview.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_works_endpoints.py
"""
import asyncio
import json

from _common import dsn, member, observed, refused

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.derivation import derive  # noqa: E402
from polaris.jobs import api as jobs_api  # noqa: E402
from polaris.jobs import worker  # noqa: E402
from polaris.models import api as models_api  # noqa: E402
from polaris.models import profiles  # noqa: E402
from polaris.models.backends.base import RawScore  # noqa: E402
from polaris.observation import api as observation_api  # noqa: E402
from polaris.observation import ingest  # noqa: E402
from polaris.search import queries  # noqa: E402
from polaris.vocabulary import api  # noqa: E402
from polaris.vocabulary import terms  # noqa: E402
from polaris.vocabulary.service import EntityManager  # noqa: E402

COLL, OTHER, EMPTY = "wt-test", "wt-other", "wt-empty"
TAG = {"test": "works_endpoints"}
A_PATH = f"/{COLL}/wt-a"


async def clean(conn):
    ids = [r["id"] for r in await conn.fetch("SELECT id FROM works WHERE content_key LIKE 'wt-%'")]
    await conn.execute("DELETE FROM work_claims WHERE work_id = ANY($1::int[])", ids)
    await conn.execute("DELETE FROM works WHERE id = ANY($1::int[])", ids)
    await conn.execute("DELETE FROM scan_runs WHERE params @> $1::jsonb", json.dumps(TAG))
    await conn.execute("DELETE FROM scan_jobs WHERE kind = 'derive'")
    wt = "(SELECT id FROM concepts WHERE slug LIKE 'wt-%')"
    await conn.execute(f"DELETE FROM term_map WHERE concept_id IN {wt}")
    await conn.execute("UPDATE concepts SET parent_id = NULL WHERE slug LIKE 'wt-%'")
    await conn.execute(f"DELETE FROM derived.work_model_claims WHERE concept_id IN {wt}")
    await conn.execute("DELETE FROM concepts WHERE slug LIKE 'wt-%'")
    for table in ("derived.tag_thresholds", "model_profile_members"):
        await conn.execute(f"DELETE FROM {table} WHERE model_version_id IN (SELECT id "
                           "FROM model_versions WHERE backend IN ('fake', 'fake2'))")
    await conn.execute("DELETE FROM collections WHERE name LIKE 'wt-%'")


def field(claims, name):
    return next(f for f in claims["fields"] if f["field"] == name)


def slugs(values):
    return sorted(v["slug"] for v in values)


def claim(work, **kw):
    return api.add_work_claim(work, api.ClaimRequest(**kw))


async def check_claims(a, b):
    got = await api.work_claims(a)
    assert got["title"] == "wt-a" and got["paths"][0]["collection"] == COLL, got
    ch = field(got, "character")
    assert ch["multi"] and ch["model_mappable"] and slugs(ch["model"]) == ["wt-char"]
    assert slugs(field(got, "series")["result"]) == ["wt-series"]
    tag = field(got, "tag")
    assert not tag["model_mappable"] and slugs(tag["model"]) == ["wt-tag1", "wt-tag_x"]
    assert not field(got, "rating")["multi"] and len(field(got, "rating")["result"]) == 1
    await refused(claim(a, field="artist", value="wt-nobody"), 409, "concept_unknown")
    await claim(a, field="artist", value="wt-nobody", create=True)
    got = await claim(a, field="artist", value="wt-artist2", create=True)
    assert slugs(field(got, "artist")["added"]) == slugs(field(got, "artist")["result"]) == [
        "wt-artist2", "wt-nobody"]
    await claim(a, field="series", value="wt-series-x", create=True)
    got = await claim(a, field="series", value="wt-series-y", create=True)
    assert slugs(field(got, "series")["added"]) == slugs(field(got, "series")["result"]) == [
        "wt-series-y"]
    got = await api.clear_work_field(a, field="series")
    assert slugs(field(got, "series")["result"]) == ["wt-series"]
    series_id = field(got, "series")["model"][0]["concept_id"]
    s = field(await claim(a, field="series", concept_id=series_id, negated=True), "series")
    assert s["result"] == [] and s["model"] == [] and slugs(s["negated"]) == ["wt-series"], s
    rating = field(got, "rating")["result"][0]
    got = await claim(a, field="rating", concept_id=rating["concept_id"], negated=True)
    assert field(got, "rating")["result"] == []
    async with state.store.acquire() as conn:
        work = await queries.get_work(conn, A_PATH)
        assert work.metadata.series is None and work.rating is None, work
    drawer = await api.get_overrides_by_path(path=A_PATH)
    assert {(o["field"], o["value"], o["negated"]) for o in drawer} >= {
        ("series", "wt-series", True), ("rating", rating["slug"], True)}, drawer
    await refused(claim(a, field="tag", value="wt-tag1", negated=True), 409, "concept_unknown")
    tag = field(await claim(a, field="tag", value="wt-tag1", negated=True, create=True), "tag")
    assert {v["slug"]: v["excluded"] for v in tag["model"]} == {
        "wt-tag1": True, "wt-tag_x": False}, tag
    assert slugs(tag["result"]) == ["wt-tag_x"]
    await refused(api.drop_work_claim(b, tag["negated"][0]["claim_id"]), 404, "claim_not_found")
    got = await api.drop_work_claim(a, tag["negated"][0]["claim_id"])
    assert slugs(field(got, "tag")["result"]) == ["wt-tag1", "wt-tag_x"]
    char = field(got, "character")["model"][0]["concept_id"]
    for call, status, code in (
            (claim(a, field="series", concept_id=char), 409, "concept_kind_mismatch"),
            (claim(a, field="nope", value="x"), 400, "field_unknown"),
            (claim(a, field="work_type", value="wt-kind", create=True), 409, "concept_enum"),
            (claim(a, field="artist"), 400, "value_required"),
            (api.work_claims(-1), 404, "work_not_found")):
        await refused(call, status, code)
    print("  ok  claims per field: create, add, replace, clear, negate, restore; bad ones refused")


async def check_list(a, b, e, series_id):
    async def ids(**kw):
        got = await api.list_works(**{"filter": None, "concept": None, "q": "wt-",
                                      "limit": 50, "offset": 0, **kw})
        assert got["total"] == len(got["items"]), got
        return {i["id"] for i in got["items"]}

    for kw, want in (({}, {a, b, e}), ({"filter": "claimed"}, {a}), ({"filter": "negated"}, {a}),
                     ({"filter": "no_artist"}, {b, e}), ({"filter": "untyped"}, {a, b, e}),
                     ({"concept": series_id}, {b}), ({"q": "wt-tag_"}, set()),
                     ({"q": "_"}, set()), ({"q": "wt-B"}, {b})):
        assert await ids(**kw) == want, (kw, want)
    item = next(i for i in (await api.list_works(
        filter="claimed", concept=None, q=None, limit=50, offset=0))["items"] if i["id"] == a)
    assert item["claims"] == 2 and item["negations"] == 2, item
    assert item["collection"] == COLL and item["folder_path"].endswith("wt-a")
    print("  ok  the work list filters, searches literally, pages and counts")


async def check_explain(conn, fake, fake2, e):
    await conn.execute(
        "INSERT INTO derived.tag_thresholds (model_version_id, category, tag, threshold, source) "
        "VALUES ($1, 'general', 'wt-shared', 0.99, 'test'), "
        "($1, 'character', 'wt-char', 0.3, 'test')", fake)
    await conn.execute("UPDATE model_profile_roles SET score_cutoff = 0.8 "
                       "WHERE model_version_id = $1 AND role = 'general'", fake2)
    pid = await profiles.active_profile(conn)
    run = await ingest.begin_run(conn, TAG)
    scores = {"fake": [RawScore("general", "wt-shared", 0.95), RawScore("general", "wt-mid", 0.4),
                       RawScore("general", "wt-low", 0.2)],
              "fake2": [RawScore("general", "wt-shared", 0.9), RawScore("general", "wt-only2", 0.85),
                        RawScore("general", "wt-mid", 0.7)]}
    d = await ingest.record_observation(conn, run, observed("wt-d", scores, COLL),
                                        {"fake": fake, "fake2": fake2})
    await ingest.finish_run(conn, run)
    await derive.derive_works(conn, [d, e])

    got = {r["tag"] for r in await conn.fetch("SELECT tag FROM work_tags WHERE work_id = $1", d)}
    by_backend, predicted = {"fake": fake, "fake2": fake2}, set()
    for backend, raws in scores.items():
        for raw in raws:
            row = next(r for r in (await models_api.explain_tag(pid, raw.tag))["rows"]
                       if r["backend"] == backend and r["category"] == raw.category)
            assert row["model_version_id"] == by_backend[backend]
            if not row["skipped"] and raw.score >= row["threshold"]:
                predicted.add(raw.tag)
    assert got == predicted == {"wt-mid", "wt-only2"}, (got, predicted)

    rows = {(r["backend"], r["category"]): r
            for r in (await models_api.explain_tag(pid, "wt-shared"))["rows"]}
    f1, f2, fc = rows["fake", "general"], rows["fake2", "general"], rows["fake", "character"]
    assert (f1["official"], f1["threshold"], f1["source"]) == (0.99, 0.99, "official")
    assert not f1["skipped"] and f1["yielded_to"] == [] and f1["emits"]
    assert f2["skipped"] and f2["yielded_to"] == ["fake"], f2
    assert (f2["member_cutoff"], f2["source"]) == (0.8, "member")
    assert (f1["freq"], f1["freq_source"], f1["freqz_source"]) == (0.3, "freq:general", "freqz:default")
    assert (fc["source"], fc["score_category"], fc["emits"]) == ("score:character", 0.5, False)
    await refused(models_api.explain_tag(-1, "x"), 404, "profile_not_found")
    print("  ok  explain matches what the derivation kept, source by source")


async def check_coverage(conn, pid):
    got = {m["backend"]: m for m in (await models_api.profile_coverage(pid))["members"]}
    fake, fake2 = got["fake"], got["fake2"]
    assert fake["searchable"]["works"] == await conn.fetchval(
        "SELECT COUNT(*) FROM work_paths p JOIN collections c "
        "ON c.name = p.collection WHERE p.present AND c.searchable"), fake
    assert fake["searchable"]["scored"] == fake2["searchable"]["scored"] + 2
    assert fake["other"]["scored"] - fake2["other"]["scored"] == 1 and fake["other"]["works"] >= 1
    await refused(models_api.profile_coverage(-1), 404, "profile_not_found")
    print("  ok  coverage splits searchable collections from the rest")


async def check_overview(conn, pid, a):
    before = await jobs_api.overview()
    await conn.execute("INSERT INTO collections (name, root, searchable) "
                       "VALUES ($1, '/wt-empty', TRUE)", EMPTY)
    for kind, slug in (("character", "wt-lone"), ("artist", "wt-clash-artist")):
        await terms.map_term(conn, "wt-clash", await terms.concept(conn, kind, slug))
    job = await worker.enqueue_derive(conn, pid, [a])
    run = await ingest.begin_run(conn, TAG)
    await ingest.record_observation(conn, run, observed(
        "wt-f", [RawScore("character", "wt-unknown-char", 0.9),
                 RawScore("copyright", "wt-unknown-series", 0.9)], COLL),
        {"fake": await profiles.model_version(conn, "fake")})
    await ingest.finish_run(conn, run)
    after = await jobs_api.overview()
    want = {"conflicts": 1, "characters_without_series": 1, "empty_collections": 1,
            "unplaced_characters": 1, "unplaced_series": 1, "unplaced": 2, "untyped_works": 1}
    assert {k: after[k] - before[k] for k in want} == want, after
    await conn.execute("UPDATE scan_jobs SET state = 'failed', finished_at = NOW() "
                       "WHERE id = $1", job)
    after = await jobs_api.overview()
    assert after["failed_jobs"] == before["failed_jobs"] + 1
    assert job not in {j["id"] for j in after["jobs"]}
    await observation_api.delete_scan_job(job)
    assert (await jobs_api.overview())["failed_jobs"] == before["failed_jobs"]
    print("  ok  the overview counts each pending item; deleting a failed job clears it")


async def main():
    await state.store.connect()
    state.entity_mgr = EntityManager(state.store)
    await state.entity_mgr.init_schema()
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
            for name, searchable in ((COLL, True), (OTHER, False)):
                await conn.execute("INSERT INTO collections (name, root, searchable) "
                                   "VALUES ($1, $2, $3)", name, f"/{name}", searchable)
            fake = await member(conn, "fake", ["general", "character", "copyright", "rating"])
            fake2 = await member(conn, "fake2", ["general"], yields=True)
            pid = await profiles.active_profile(conn)
            series = await terms.concept(conn, "series", "wt-series")
            await terms.map_term(conn, "wt-char", await terms.concept(
                conn, "character", "wt-char", parent_id=series))
            run = await ingest.begin_run(conn, TAG)
            a, b, e = [await ingest.record_observation(
                conn, run, observed(key, scores, coll), {"fake": fake}) for key, scores, coll in (
                    ("wt-a", [RawScore("general", "wt-tag1", 0.9), RawScore("general", "wt-tag_x", 0.85),
                              RawScore("character", "wt-char", 0.9),
                              RawScore("rating", "explicit", 0.9)], COLL),
                    ("wt-b", [RawScore("character", "wt-char", 0.9)], COLL),
                    ("wt-e", [RawScore("general", "wt-tag1", 0.9)], OTHER))]
            await ingest.finish_run(conn, run)

        await check_claims(a, b)
        await check_list(a, b, e, series)
        async with state.store.acquire() as conn:
            await check_explain(conn, fake, fake2, e)
            await check_coverage(conn, pid)
            await check_overview(conn, pid, a)
            await clean(conn)
        print("all works endpoint checks passed")
    finally:
        await state.store.close()


asyncio.run(main())
