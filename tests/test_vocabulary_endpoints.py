"""Curating the vocabulary from the page: concepts, terms, merge, delete,
placing, overrides, hidden tags and translations; rows only.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_vocabulary_endpoints.py
"""
import asyncio
import json

from _common import dsn, member, observed, refused, run_derives

DSN = dsn()

from polaris import state  # noqa: E402
from polaris.jobs import worker  # noqa: E402
from polaris.models import profiles  # noqa: E402
from polaris.models.backends.base import RawScore  # noqa: E402
from polaris.observation import ingest  # noqa: E402
from polaris.vocabulary import api  # noqa: E402
from polaris.vocabulary import terms  # noqa: E402
from polaris.vocabulary.service import EntityManager  # noqa: E402

COLL = "vt-test"
TAG = {"test": "vocabulary_endpoints"}
SLUG, TERM = "vt-term-artist", "VT Term Artist"


async def clean(conn):
    ids = [r["id"] for r in await conn.fetch(
        "SELECT id FROM works WHERE content_key LIKE 'vt-test:%'")]
    await conn.execute("DELETE FROM work_claims WHERE work_id = ANY($1::int[])", ids)
    await conn.execute("DELETE FROM works WHERE id = ANY($1::int[])", ids)
    await conn.execute("DELETE FROM scan_runs WHERE params @> $1::jsonb", json.dumps(TAG))
    await conn.execute("DELETE FROM scan_jobs WHERE kind = 'derive'")
    vt = "(SELECT id FROM concepts WHERE slug LIKE 'vt-%')"
    await conn.execute(f"DELETE FROM term_map WHERE concept_id IN {vt}")
    await conn.execute("UPDATE concepts SET parent_id = NULL WHERE slug LIKE 'vt-%'")
    await conn.execute(f"DELETE FROM derived.work_model_claims WHERE concept_id IN {vt}")
    await conn.execute("DELETE FROM concepts WHERE slug LIKE 'vt-%'")
    for table in ("tag_translations", "hidden_tags"):
        await conn.execute(f"DELETE FROM {table} WHERE tag LIKE 'vt-%'")
    await conn.execute("DELETE FROM model_profile_members WHERE model_version_id IN "
                       "(SELECT id FROM model_versions WHERE backend = 'fake')")
    await conn.execute("DELETE FROM collections WHERE name = $1", COLL)


async def characters(conn, work_id):
    return {r["slug"] for r in await conn.fetch(
        "SELECT c.slug FROM work_model_claims m JOIN concepts c ON c.id = m.concept_id "
        "WHERE m.work_id = $1 AND m.field = 'character'", work_id)}


async def series(conn, work_id):
    return await conn.fetchval("SELECT series FROM work_search WHERE id = $1", work_id)


async def cid(conn, kind, slug):
    return await conn.fetchval(
        "SELECT id FROM concepts WHERE kind = $1 AND slug = $2", kind, slug)


def char(tag):
    return api.OverrideRequest(action="add", tag_category="character", new_tag=tag)


async def check_terms():
    made = await api.create_concept(api.ConceptRequest(
        kind="artist", slug=SLUG, display_zh="端點測試", terms=[TERM],
        search_only=False, note="written by a test"))
    assert made["id"] and len(made["mapped"]) == 1, made
    term_id = made["mapped"][0]
    found = (await api.lookup_term(q=TERM.lower(), search_only=None))["mappings"]
    assert [(m["id"], m["kind"], m["slug"], m["display_zh"]) for m in found] == [
        (term_id, "artist", SLUG, "端點測試")], found
    assert (await api.lookup_term(q=TERM, search_only=True))["mappings"] == []
    again = await api.create_concept(api.ConceptRequest(
        kind="artist", slug=SLUG, terms=[TERM.upper()], search_only=False))
    assert again["id"] == made["id"] and again["mapped"] == [], again
    print("  ok  a concept and its term go in together; lookup ignores case; re-mapping is a no-op")

    resolved = await api.resolve_name(name=TERM.upper(), kind=None)
    assert (resolved["kind"], resolved["canonical"]) == ("artist", SLUG), resolved
    assert await api.resolve_name(name="vt-nothing", kind="artist") == {"error": "not found"}
    print("  ok  resolve names a concept by any of its terms")

    listed = {c["slug"]: c for c in await api.list_concepts(kind="artist")}
    assert listed[SLUG]["display"] == "端點測試" and listed[SLUG]["note"] == "written by a test"
    await api.edit_concept(made["id"], api.ConceptEdit(display_zh="改名", note=""))
    listed = {c["slug"]: c for c in await api.list_concepts(kind="artist")}
    assert (listed[SLUG]["display_zh"], listed[SLUG]["note"]) == ("改名", None), listed[SLUG]
    await refused(api.edit_concept(-1, api.ConceptEdit(display_zh="x")), 404)
    assert (await api.unmap_term(term_id))["deleted"] is True
    assert (await api.lookup_term(q=TERM, search_only=None))["mappings"] == []
    await refused(api.unmap_term(term_id), 404, "term_not_found")
    print("  ok  a concept's name and note are edited, an empty note clears; an unmapped term stays gone")


async def check_overrides(a, b):
    path = f"/{COLL}/b"
    added = await api.add_override_by_path(path=path, req=char("vt-char-x"))
    assert [o["value"] for o in await api.get_overrides_by_path(path=path)] == ["vt-char-x"]
    assert await api.get_overrides(b) == await api.get_overrides_by_path(path=path)
    await api.delete_override(added["id"])
    assert await api.get_overrides(b) == []
    assert await api.get_overrides_by_path(path="/nowhere") == []
    assert await api.add_override_by_path(path="/nowhere", req=char("x")) == {
        "error": "work not found"}
    await refused(api.add_override(a, api.OverrideRequest(action="rename")), 400,
                  "invalid_override")
    print("  ok  overrides are added, read and deleted by id or by path")

    assert "vt-dup" in (await api.get_visible_tags(path=f"/{COLL}/a"))["general"]
    await api.hide_tag(tag="vt-dup")
    assert {"tag": "vt-dup"} in await api.list_hidden_tags()
    assert not next(t for t in await api.all_tags(limit=20000) if t["tag"] == "vt-dup")["visible"]
    assert "vt-dup" not in (await api.get_visible_tags(path=f"/{COLL}/a"))["general"]
    await api.show_tag(tag="vt-dup")
    assert {"tag": "vt-dup"} not in await api.list_hidden_tags()
    assert "vt-dup" in (await api.get_visible_tags(path=f"/{COLL}/a"))["general"]
    print("  ok  a hidden tag leaves a work's tags and comes back when shown")


async def main():
    await state.store.connect()
    state.entity_mgr = EntityManager(state.store)
    await state.entity_mgr.init_schema()
    try:
        async with state.store.acquire() as conn:
            await clean(conn)
            await conn.execute("INSERT INTO collections (name, root, searchable) "
                               "VALUES ($1, '/vt-test', FALSE)", COLL)
            await member(conn, "fake", ("general", "character", "copyright"))
            run = await ingest.begin_run(conn, TAG)
            a = await ingest.record_observation(conn, run, observed("a", [
                RawScore("character", "vt-char-a", 0.9), RawScore("general", "vt-dup", 0.9),
                RawScore("character", "vt-dup", 0.9)], COLL))
            b = await ingest.record_observation(conn, run, observed("b", [
                RawScore("character", "vt-char-b", 0.9), RawScore("general", "vt-dup", 0.9)], COLL))
            await ingest.finish_run(conn, run)

        await check_terms()
        await check_overrides(a, b)

        got = await api.unplaced_terms(category="character", kind="character", limit=1)
        assert len(got["tags"]) == 1 and got["total"] == 3, got
        placed = await api.place_unplaced(category="character", kind="character")
        assert placed["placed"] == 3 and placed["jobs"], placed
        async with state.store.acquire() as conn:
            assert await characters(conn, a) == set()
            params = json.loads(await conn.fetchval(
                "SELECT params FROM scan_jobs WHERE id = $1", placed["jobs"][0]))
            assert set(params["works"]) == {a, b}, params
            assert await worker.enqueue_derive(
                conn, await profiles.active_profile(conn)) not in placed["jobs"]
        await run_derives()
        async with state.store.acquire() as conn:
            assert await characters(conn, a) == {"vt-char-a", "vt-dup"}
        assert (await api.unplaced_terms(category="character", kind="character",
                                         limit=1))["total"] == 0
        print("  ok  unplaced counts past its limit; placing runs as derive jobs")

        async with state.store.acquire() as conn:
            term_a = await conn.fetchval("SELECT id FROM term_map WHERE term = 'vt-char-a'")
        for search_only in (True, False):
            flipped = await api.edit_term(term_a, api.TermEdit(search_only=search_only))
            assert flipped["search_only"] is search_only, flipped
            await run_derives()
            async with state.store.acquire() as conn:
                assert ("vt-char-a" in await characters(conn, a)) is not search_only
        await refused(api.edit_term(-1, api.TermEdit(search_only=True)), 404, "term_not_found")
        print("  ok  a term switches between naming works and search only")

        async with state.store.acquire() as conn:
            char_a = await cid(conn, "character", "vt-char-a")
            char_b = await cid(conn, "character", "vt-char-b")
            s1 = await terms.concept(conn, "series", "vt-series-1", note="kept")
            s2 = await terms.concept(conn, "series", "vt-series-2")
            rating = await cid(conn, "rating", "general")
        out = await api.edit_concept(char_a, api.ConceptEdit(parent_id=s1))
        assert out["works"] == 1 and out["jobs"], out
        await run_derives()
        assert {c["id"]: c for c in await api.list_concepts("character")}[char_a]["parent_id"] == s1
        await api.edit_concept(s1, api.ConceptEdit(slug="vt-series-one"))
        await run_derives()
        await api.edit_concept(s1, api.ConceptEdit(display_zh="系列一"))
        async with state.store.acquire() as conn:
            assert await series(conn, a) == "vt-series-one"
            assert await conn.fetchval("SELECT note FROM concepts WHERE id = $1", s1) == "kept"
        print("  ok  a parent and a new slug queue a re-derive; unsent fields stay")

        detail = await refused(api.edit_concept(char_a, api.ConceptEdit(slug="VT-CHAR-B")),
                               409, "concept_exists")
        assert detail["id"] == char_b, detail
        await api.edit_concept(s2, api.ConceptEdit(parent_id=s1))
        for call, status, code in (
                (api.edit_concept(char_a, api.ConceptEdit(parent_id=char_b)), 400, "parent_not_series"),
                (api.edit_concept(s1, api.ConceptEdit(parent_id=s2)), 409, "parent_cycle"),
                (api.edit_concept(s1, api.ConceptEdit(parent_id=s1)), 409, "parent_cycle"),
                (api.edit_concept(rating, api.ConceptEdit(slug="vt-x")), 409, "concept_enum"),
                (api.delete_concept(rating), 409, "concept_enum"),
                (api.edit_concept(char_a, api.ConceptEdit(slug=" ")), 400, "slug_required")):
            await refused(call, status, code)
        print("  ok  slug clashes name the other; parents are series, acyclic; enums are fixed")

        for work, tag in ((a, "vt-char-b"), (a, "vt-char-a"), (b, "vt-char-b")):
            assert (await api.add_override(work, char(tag)))["id"]
        assert {o["value"] for o in await api.get_overrides(a)} == {"vt-char-a", "vt-char-b"}
        await refused(api.merge_concept(char_b, api.MergeRequest(into=s1)), 409,
                      "concept_kind_mismatch")
        await refused(api.merge_concept(char_b, api.MergeRequest(into=char_b)), 400,
                      "merge_into_self")
        merged = await api.merge_concept(char_b, api.MergeRequest(into=char_a))
        assert merged["terms"] == 1 and merged["claims"] == 1, merged
        await run_derives()
        async with state.store.acquire() as conn:
            assert await cid(conn, "character", "vt-char-b") is None
            assert await conn.fetchval(
                "SELECT concept_id FROM term_map WHERE term = 'vt-char-b'") == char_a
            assert sorted(r["work_id"] for r in await conn.fetch(
                "SELECT work_id FROM work_claims WHERE concept_id = $1", char_a)) == sorted([a, b])
            assert await series(conn, b) == "vt-series-one"
            assert "vt-char-a" in await characters(conn, b)
            s3 = await terms.concept(conn, "series", "vt-series-3")
        print("  ok  a merge moves terms and claims, the target wins a clash, and works re-derive")

        await api.merge_concept(s1, api.MergeRequest(into=s3))
        await run_derives()
        async with state.store.acquire() as conn:
            assert await terms.resolve_concept(conn, "series", "vt-series-one") == s3
            assert [await conn.fetchval("SELECT parent_id FROM concepts WHERE id = $1", c)
                    for c in (char_a, s2)] == [s3, s3]
            assert await series(conn, a) == "vt-series-3"
        await refused(api.merge_concept(s3, api.MergeRequest(into=s2)), 409, "parent_cycle")
        print("  ok  a merge moves children, keeps the old name as an alias, refuses to go under itself")

        detail = await refused(api.delete_concept(char_a), 409, "concept_in_use")
        assert detail["claims"] == 2 and detail["children"] == 0, detail
        assert (await refused(api.delete_concept(s3), 409, "concept_in_use"))["children"] == 2
        async with state.store.acquire() as conn:
            dup = await cid(conn, "character", "vt-dup")
        gone = await api.delete_concept(dup)
        assert gone["deleted"] and gone["works"] == 1, gone
        await run_derives()
        async with state.store.acquire() as conn:
            assert await conn.fetchval("SELECT 1 FROM term_map WHERE term = 'vt-dup'") is None
            assert "vt-dup" not in await characters(conn, a)
            lone = await terms.concept(conn, "character", "vt-lone")
            other = await terms.concept(conn, "artist", "vt-clash-artist")
            for concept in (lone, other):
                await terms.map_term(conn, "vt-clash", concept)
        await refused(api.delete_concept(dup), 404, "concept_not_found")
        print("  ok  delete refuses what is in use, and takes the terms along")

        clash = next(c for c in await api.term_conflicts() if c["term"] == "vt-clash")
        assert {c["slug"] for c in clash["concepts"]} == {"vt-lone", "vt-clash-artist"}, clash
        rows = [t for t in await api.all_tags(limit=20000) if t["tag"] == "vt-dup"]
        assert len(rows) == 1 and rows[0]["category"] == "general", rows
        assert set(rows[0]["categories"]) == {"general", "character"} and rows[0]["works"] == 2
        print("  ok  a term naming two things is a conflict; a tag in two categories is one row")

        await api.import_translations({"vt-dup": "舊名"})
        await api.set_translation("vt-dup", api.TranslationRequest(display_zh="新名"))
        assert state.entity_mgr.resolve_tag("新名") == "vt-dup"
        assert (await api.import_translations({"vt-dup": "再匯入"}))["added"] == 0
        assert (await api.translations())["vt-dup"] == "新名"
        await api.set_translation("vt-dup", api.TranslationRequest(display_zh=" "))
        assert "vt-dup" not in await api.translations()
        print("  ok  a reimport keeps an edited translation; empty removes it")

        async with state.store.acquire() as conn:
            await clean(conn)
        print("all vocabulary endpoint checks passed")
    finally:
        await state.store.close()


asyncio.run(main())
