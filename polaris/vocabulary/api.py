"""HTTP endpoints for tags, concepts, terms, translations and overrides."""

import json
import re
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from polaris import state
from polaris.derivation import derive
from polaris.jobs import worker
from polaris.search import queries
from polaris.search.engine import FILTER_KINDS
from polaris.shared.errors import refuse
from polaris.vocabulary import terms as terms_module
from polaris.vocabulary.service import record_claim
from polaris.vocabulary.terms import TermError

router = APIRouter()


_series_display_cache: dict[str, str] = {}
_author_display_cache: dict[str, str] = {}


async def _sync_vocabulary() -> None:
    """Pick up term_map rows written since this process started.

    The caches load once at startup, so a mapping added from the CLI or
    another process is otherwise invisible until a restart.
    """
    if state.entity_mgr is None:
        return
    if await state.entity_mgr.refresh_if_changed():
        _series_display_cache.clear()
        _author_display_cache.clear()


async def _vocabulary_changed() -> None:
    """Reload the caches after this process wrote to the vocabulary."""
    await state.entity_mgr.refresh_cache()
    _series_display_cache.clear()
    _author_display_cache.clear()


@router.get("/api/tags/hidden")
async def list_hidden_tags():
    """List all hidden tags."""
    return await state.entity_mgr.list_hidden()


@router.post("/api/tags/hide")
async def hide_tag(tag: str = Query(...)):
    """Hide a tag from UI display."""
    await state.entity_mgr.hide_tag(tag)
    return {"ok": True}


@router.post("/api/tags/show")
async def show_tag(tag: str = Query(...)):
    """Un-hide a tag."""
    await state.entity_mgr.show_tag(tag)
    return {"ok": True}


@router.get("/api/tags/all")
async def all_tags(limit: int = Query(2000, ge=1, le=20000)):
    """Every tag the library actually carries, hidden ones included.

    Hiding used to be a text box: type the tag exactly and it disappears.
    Nobody hides a tag they have not seen, and nobody can spell thousands
    of them, so the box could only be used to undo something the page had
    already listed. The candidates are rows -- this is them, ordered by
    how many works carry each, which is the order in which a person cares.

    `work_tags` rather than `effective_tags`: the second one is
    already filtered by visibility, so a hidden tag would be missing from
    the list whose whole job is showing it.

    One row per tag, since hiding and naming are per tag: `categories` has
    every category it was seen in, most works first, and `category` is
    the first of them.
    """
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH per AS (
                SELECT tag, category, COUNT(DISTINCT work_id) AS n
                FROM work_tags GROUP BY tag, category
            ), total AS (
                SELECT tag, COUNT(DISTINCT work_id) AS works
                FROM work_tags GROUP BY tag
            )
            SELECT p.tag,
                   (array_agg(p.category ORDER BY p.n DESC, p.category))[1]
                       AS category,
                   array_agg(p.category ORDER BY p.n DESC, p.category)
                       AS categories,
                   t.works,
                   e.display_zh AS zh,
                   h.tag IS NULL AS visible
            FROM per p
            JOIN total t ON t.tag = p.tag
            LEFT JOIN tag_translations e ON e.tag = p.tag
            LEFT JOIN hidden_tags      h ON h.tag = p.tag
            GROUP BY p.tag, t.works, e.display_zh, h.tag
            ORDER BY t.works DESC, p.tag
            LIMIT $1
            """,
            limit)
    return [dict(r) for r in rows]


@router.get("/api/vocabulary/unplaced")
async def unplaced_terms(category: str = Query("character"),
                         kind: str = Query("character"),
                         limit: int = Query(200, ge=1, le=2000)):
    """Strings the models emit that no concept claims, worst first.

    The other half of `/api/concepts`. That one lists the things this
    library knows about; this lists the words it hears and cannot place.

    `asserts` is the whole question of whether the list is worth looking
    at. A concept only reaches a work through the `named` arm of
    `work_claims_model`, which joins `claim_fields ... AND model_mappable`
    -- so a `character` concept puts a character on every work carrying
    that tag, and a `tag` concept does nothing at all. `tag` is not a
    filter kind either, so its terms do not even become search filters;
    the only thing a tag concept ever does is hold two spellings together
    as synonyms, and that needs a second term; one with exactly one term
    is inert.

    `works` is the distinct count, not the sum: a work carrying three
    unplaced characters is one work either way. `total` counts the tags
    past `limit` too.
    """
    async with state.store.acquire() as conn:
        asserts = bool(await conn.fetchval(
            "SELECT model_mappable FROM claim_fields WHERE field = $1", kind))
        rows = await conn.fetch(
            """
            SELECT t.tag, t.category,
                   COUNT(DISTINCT t.work_id) AS works,
                   e.display_zh AS zh
            FROM work_tags t
            LEFT JOIN term_map tm ON lower(tm.term) = lower(t.tag)
            LEFT JOIN tag_translations e ON e.tag = t.tag
            WHERE tm.id IS NULL AND t.category = $1
            GROUP BY t.tag, t.category, e.display_zh
            ORDER BY COUNT(DISTINCT t.work_id) DESC, t.tag
            LIMIT $2
            """,
            category, limit)
        counts = await conn.fetchrow(
            """
            SELECT COUNT(DISTINCT t.tag) AS total,
                   COUNT(DISTINCT t.work_id) AS works
            FROM work_tags t
            LEFT JOIN term_map tm ON lower(tm.term) = lower(t.tag)
            WHERE tm.id IS NULL AND t.category = $1
            """,
            category)
    return {"kind": kind, "category": category, "asserts": asserts,
            "total": counts["total"],
            "works": counts["works"] if asserts else 0,
            "tags": [dict(r) for r in rows]}


@router.post("/api/vocabulary/place")
async def place_unplaced(category: str = Query("character"),
                         kind: str = Query("character")):
    """Make a concept of every word in that category nobody has placed.

    Nothing is inferred here. The tagger reported the category, so it is
    the tagger saying the word names a character; `tag_translations` is a
    dictionary somebody imported, so the Chinese name is already written
    down. All that was missing is the row, and clicking a button per word to
    copy two columns the database already holds is not curation.

    What stays a person's job is what neither table can say: that two
    spellings are the same person, and which series a character belongs
    to. Those are `term_map` rows and `concepts.parent_id`, and they are
    what the ledger below the list is for.

    The rows are written here; re-deriving the works they name is one
    derive job per maintained profile, whose ids come back in `jobs`.
    """
    async with state.store.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT DISTINCT t.tag
                FROM work_tags t
                LEFT JOIN term_map tm ON lower(tm.term) = lower(t.tag)
                WHERE tm.id IS NULL AND t.category = $1
                """,
                category)
            for r in rows:
                cid = await terms_module.concept(conn, kind, r["tag"])
                await terms_module.map_term(conn, r["tag"], cid)
            works = await derive.works_naming(conn, [r["tag"] for r in rows])
            jobs = await _queue_derive(conn, works)
    await _vocabulary_changed()
    return {"placed": len(rows), "works": len(works), "jobs": jobs}


@router.get("/api/terms/conflicts")
async def term_conflicts():
    """Strings that name two things at once where that is a contradiction.

    A search-only synonym broadens on purpose. A mapping that also names
    works turns one model tag into two claims.
    """
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT lower(t.term) AS term,
                   json_agg(json_build_object(
                       'id', t.id, 'kind', c.kind, 'slug', c.slug,
                       'name', COALESCE(concept_display_zh(c.id), c.slug))
                       ORDER BY c.slug) AS concepts
            FROM term_map t
            JOIN concepts c ON c.id = t.concept_id
            WHERE NOT t.search_only
            GROUP BY lower(t.term)
            HAVING COUNT(DISTINCT t.concept_id) > 1
            ORDER BY lower(t.term)
            """)
    return [{**dict(r), "concepts": json.loads(r["concepts"]), "asserts": True}
            for r in rows]


@router.get("/api/tags/visible")
async def get_visible_tags(path: str = Query(...)):
    """Get visible (non-hidden) tags for a work."""
    await _sync_vocabulary()
    async with state.store.acquire() as conn:
        work = await queries.get_work(conn, path)
    if not work:
        return {"general": {}, "character": {}, "copyright": {}, "artist": {}}
    return {
        "general": work.general_tags,
        "character": work.character_tags,
        "copyright": getattr(work, 'copyright_tags', {}),
        "artist": getattr(work, 'artist_tags', {}),
    }


class OverrideRequest(BaseModel):
    action: str
    tag_category: str | None = None
    original_tag: str | None = None
    new_tag: str | None = None
    field_name: str | None = None
    field_value: str | None = None


async def _get_work_id(folder_path: str) -> int | None:
    """Look up DB work id by any path the work has answered to.

    Not `folder_path = $1`: that column names the one path the identity
    view chose, and a present path need not be it.
    """
    async with state.store.acquire() as conn:
        return await conn.fetchval(
            "SELECT work_id FROM work_paths WHERE path = $1 "
            "ORDER BY present DESC, last_seen DESC LIMIT 1",
            folder_path
        )


@router.get("/api/work/overrides")
async def get_overrides_by_path(path: str = Query(...)):
    """List overrides for a work by folder path."""
    work_id = await _get_work_id(path)
    if not work_id:
        return []
    return await state.entity_mgr.get_work_overrides(work_id)


@router.post("/api/work/overrides")
async def add_override_by_path(path: str = Query(...), req: OverrideRequest = ...):
    """Add a tag/field override by folder path."""
    work_id = await _get_work_id(path)
    if not work_id:
        return {"error": "work not found"}
    return await add_override(work_id, req)


@router.get("/api/works/{work_id}/overrides")
async def get_overrides(work_id: int):
    """List overrides for a work."""
    return await state.entity_mgr.get_work_overrides(work_id)


@router.post("/api/works/{work_id}/overrides")
async def add_override(work_id: int, req: OverrideRequest):
    """Add a tag/field override."""
    try:
        oid = await state.entity_mgr.add_work_override(
            work_id=work_id,
            action=req.action,
            tag_category=req.tag_category,
            original_tag=req.original_tag,
            new_tag=req.new_tag,
            field_name=req.field_name,
            field_value=req.field_value,
        )
    except (ValueError, TermError) as e:
        raise refuse(400, "invalid_override", str(e)) from None
    return {"id": oid}


@router.delete("/api/overrides/{override_id}")
async def delete_override(override_id: int):
    """Remove an override."""
    await state.entity_mgr.delete_work_override(override_id)
    return {"ok": True}


class ClaimRequest(BaseModel):
    """A claim by concept id, or by a string naming one; `create` makes the
    concept when nothing does."""

    field: str
    concept_id: int | None = None
    value: str | None = None
    negated: bool = False
    create: bool = False


_CLAIM_ROWS_SQL = """
    SELECT 'model' AS layer, m.field, m.concept_id, NULL::bigint AS claim_id,
           NULL::timestamptz AS created_at
    FROM work_model_claims m WHERE m.work_id = $1
    UNION ALL
    SELECT CASE WHEN c.negated THEN 'negated' ELSE 'added' END, c.field,
           c.concept_id, c.id, c.created_at
    FROM work_claims c WHERE c.work_id = $1
    UNION ALL
    SELECT 'result', r.field, r.concept_id, NULL, NULL
    FROM work_claims_current r JOIN claim_fields f ON f.field = r.field
    WHERE r.work_id = $1 AND NOT f.multi
    UNION ALL
    SELECT 'result', r.field, r.concept_id, NULL, NULL
    FROM work_claims_multi r WHERE r.work_id = $1 AND r.field <> 'tag'
"""

_TAG_ROWS_SQL = """
    SELECT x.layer, x.tag, x.freq_ratio, x.avg_score, x.visible,
           tc.id AS concept_id,
           COALESCE(concept_display_zh(tc.id), tr.display_zh) AS display_zh
    FROM (
        SELECT 'model' AS layer, t.tag, t.freq_ratio, t.avg_score,
               h.tag IS NULL AS visible
        FROM work_tags t LEFT JOIN hidden_tags h ON h.tag = t.tag
        WHERE t.work_id = $1 AND t.category = 'general'
        UNION ALL
        SELECT 'result', e.tag, e.freq_ratio, e.avg_score, e.visible
        FROM effective_tags e
        WHERE e.work_id = $1 AND e.category = 'general'
    ) x
    LEFT JOIN LATERAL (
        SELECT c.id FROM concepts c
        WHERE c.kind = 'tag'
          AND (lower(c.slug) = lower(x.tag)
               OR EXISTS (SELECT 1 FROM term_map tm
                          WHERE tm.concept_id = c.id AND NOT tm.search_only
                            AND lower(tm.term) = lower(x.tag)))
        ORDER BY (lower(c.slug) = lower(x.tag)) DESC, c.id LIMIT 1
    ) tc ON TRUE
    LEFT JOIN tag_translations tr ON tr.tag = x.tag
    ORDER BY x.freq_ratio * x.avg_score DESC, x.tag
"""


async def _work_row(conn, work_id: int):
    row = await conn.fetchrow(
        "SELECT w.id, COALESCE(w.title, i.folder_name) AS title, "
        "       i.folder_path, i.collection "
        "FROM works w JOIN work_identity i ON i.id = w.id WHERE w.id = $1",
        work_id)
    if row is None:
        raise refuse(404, "work_not_found", "no such work", id=work_id)
    return row


async def _work_claims(conn, work_id: int) -> dict:
    """Every claim field of a work: what the models said, what a person
    added or negated, and what stands."""
    work = await _work_row(conn, work_id)
    paths = await conn.fetch(
        "SELECT path, collection, present FROM work_paths WHERE work_id = $1 "
        "ORDER BY present DESC, last_seen DESC, id DESC", work_id)
    fields = await conn.fetch(
        "SELECT field, multi, COALESCE(model_mappable, FALSE) AS model_mappable "
        "FROM claim_fields ORDER BY field")
    rows = await conn.fetch(
        "SELECT x.*, co.slug, concept_display_zh(co.id) AS display_zh "
        f"FROM ({_CLAIM_ROWS_SQL}) x JOIN concepts co ON co.id = x.concept_id "
        "ORDER BY co.slug", work_id)
    out = {f["field"]: {**dict(f), "model": [], "added": [], "negated": [],
                        "result": []} for f in fields}
    for r in rows:
        value = {"concept_id": r["concept_id"], "slug": r["slug"],
                 "display_zh": r["display_zh"]}
        if r["claim_id"] is not None:
            value |= {"claim_id": r["claim_id"], "created_at": r["created_at"]}
        out[r["field"]][r["layer"]].append(value)
    for f in out.values():
        struck = {v["concept_id"] for v in f["negated"]}
        for v in f["model"]:
            v["excluded"] = v["concept_id"] in struck
    if "tag" in out:
        tags = await conn.fetch(_TAG_ROWS_SQL, work_id)
        kept = {r["tag"] for r in tags if r["layer"] == "result"}
        out["tag"]["model"] = []
        out["tag"]["result"] = []
        for r in tags:
            value = {"concept_id": r["concept_id"], "slug": r["tag"],
                     "display_zh": r["display_zh"],
                     "freq_ratio": r["freq_ratio"], "avg_score": r["avg_score"],
                     "visible": r["visible"]}
            if r["layer"] == "model":
                value["excluded"] = r["tag"] not in kept
            out["tag"][r["layer"]].append(value)
    return {**dict(work), "paths": [dict(p) for p in paths],
            "fields": list(out.values())}


@router.get("/api/works/{work_id}/claims")
async def work_claims(work_id: int):
    """A work's claims, field by field, in the active profile."""
    async with state.store.acquire() as conn:
        return await _work_claims(conn, work_id)


@router.post("/api/works/{work_id}/claims")
async def add_work_claim(work_id: int, req: ClaimRequest):
    """Claim a value for a field, or with `negated` that it is not so, then
    re-derive the work. A string nothing names is refused unless `create`."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            await _work_row(conn, work_id)
            if await conn.fetchval(
                    "SELECT 1 FROM claim_fields WHERE field = $1",
                    req.field) is None:
                raise refuse(400, "field_unknown",
                             f"{req.field!r} is not a claim field",
                             field=req.field)
            if req.concept_id is not None:
                row = await _concept(conn, req.concept_id)
                if row["kind"] != req.field:
                    raise refuse(409, "concept_kind_mismatch",
                                 f"a {row['kind']} cannot fill {req.field}",
                                 kind=row["kind"], field=req.field)
                cid = row["id"]
            elif (req.value or "").strip():
                value = req.value.strip()
                cid = await terms_module.find_concept(conn, req.field, value)
                if cid is None and not req.create:
                    raise refuse(409, "concept_unknown",
                                 f"no {req.field} is named {value!r}",
                                 field=req.field, value=value)
                if cid is None:
                    if req.field in ENUM_KINDS:
                        raise refuse(409, "concept_enum",
                                     f"{req.field} values are fixed",
                                     kind=req.field)
                    cid = await terms_module.concept(conn, req.field, value)
            else:
                raise refuse(400, "value_required",
                             "name a concept_id or a value")
            claim_id = await record_claim(conn, work_id, req.field, cid,
                                          negated=req.negated)
            return {"claim_id": claim_id, **await _work_claims(conn, work_id)}


@router.delete("/api/works/{work_id}/claims/{claim_id}")
async def drop_work_claim(work_id: int, claim_id: int):
    """Withdraw one claim, then re-derive the work."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval(
                    "DELETE FROM work_claims WHERE id = $1 AND work_id = $2 "
                    "RETURNING TRUE", claim_id, work_id):
                raise refuse(404, "claim_not_found",
                             "this work has no such claim", id=claim_id)
            await derive.derive_works(conn, [work_id])
            return await _work_claims(conn, work_id)


@router.delete("/api/works/{work_id}/claims")
async def clear_work_field(work_id: int, field: str = Query(...)):
    """Withdraw every claim a person made on one field, leaving the models'
    answer, then re-derive the work."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            await _work_row(conn, work_id)
            await conn.execute(
                "DELETE FROM work_claims WHERE work_id = $1 AND field = $2",
                work_id, field)
            await derive.derive_works(conn, [work_id])
            return await _work_claims(conn, work_id)


_WORK_FILTERS = {
    "untyped": "w.work_type IS NULL",
    "no_artist": "w.artist IS NULL",
    "claimed": "EXISTS (SELECT 1 FROM work_claims c "
               "WHERE c.work_id = w.id AND NOT c.negated)",
    "negated": "EXISTS (SELECT 1 FROM work_claims c "
               "WHERE c.work_id = w.id AND c.negated)",
}


@router.get("/api/works")
async def list_works(
        filter: Literal["untyped", "no_artist", "claimed", "negated"] | None = None,
        concept: int | None = None, q: str | None = None,
        limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    """Works in the active profile, narrowed by a filter, a concept they
    carry, and text in the title or path; `total` counts past the page."""
    sql = (_WORK_FILTERS[filter] if filter else "TRUE") + """
        AND ($1::int IS NULL OR w.id IN (SELECT work_id FROM work_claims_ranked
                                         WHERE concept_id = $1))
        AND ($2::text IS NULL OR w.title ILIKE $2 OR w.folder_path ILIKE $2)"""
    text = (q or "").strip()
    like = "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%" if text else None
    async with state.store.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM work_search w WHERE {sql}", concept, like)
        rows = await conn.fetch(
            f"""
            SELECT p.id, p.title, p.folder_path, p.collection, p.work_type,
                   p.artist,
                   COUNT(c.id) FILTER (WHERE NOT c.negated) AS claims,
                   COUNT(c.id) FILTER (WHERE c.negated) AS negations
            FROM (SELECT w.id, w.title, w.folder_path, w.collection,
                         w.work_type, w.artist
                  FROM work_search w WHERE {sql}
                  ORDER BY w.title, w.id LIMIT $3 OFFSET $4) p
            LEFT JOIN work_claims c ON c.work_id = p.id
            GROUP BY p.id, p.title, p.folder_path, p.collection, p.work_type,
                     p.artist
            ORDER BY p.title, p.id
            """, concept, like, limit, offset)
    return {"total": total, "items": [dict(r) for r in rows]}


class ConceptRequest(BaseModel):
    kind: str
    slug: str
    display_zh: str | None = None
    terms: list[str] | None = None
    search_only: bool = True
    note: str | None = None


class ConceptEdit(BaseModel):
    """Only the fields sent are changed."""

    display_zh: str | None = None
    note: str | None = None
    slug: str | None = None
    parent_id: int | None = None


class MergeRequest(BaseModel):
    into: int


class TermEdit(BaseModel):
    search_only: bool


ENUM_KINDS = ("rating", "color_mode", "work_type")


async def _concept(conn, concept_id: int):
    row = await conn.fetchrow(
        "SELECT id, kind, slug, parent_id FROM concepts WHERE id = $1",
        concept_id)
    if row is None:
        raise refuse(404, "concept_not_found", "no such concept",
                     id=concept_id)
    return row


def _fixed(row) -> None:
    if row["kind"] in ENUM_KINDS:
        raise refuse(409, "concept_enum",
                     f"{row['kind']} concepts are looked up by slug; they "
                     f"cannot be renamed, merged or deleted", kind=row["kind"])


async def _ancestors(conn, concept_id: int) -> set[int]:
    """The concept and every parent above it."""
    return {r["id"] for r in await conn.fetch(
        "WITH RECURSIVE up(id, parent_id) AS ("
        " SELECT id, parent_id FROM concepts WHERE id = $1"
        " UNION SELECT c.id, c.parent_id FROM concepts c"
        " JOIN up ON c.id = up.parent_id) SELECT id FROM up", concept_id)}


async def _works_of(conn, concept_ids) -> list[int]:
    """Works a person or a model put any of these concepts on."""
    return [r["work_id"] for r in await conn.fetch(
        "SELECT work_id FROM work_claims WHERE concept_id = ANY($1::int[]) "
        "UNION SELECT work_id FROM derived.work_model_claims "
        "WHERE concept_id = ANY($1::int[])", list(concept_ids))]


async def _drop_concept(conn, concept_id: int) -> None:
    await conn.execute(
        "DELETE FROM derived.work_model_claims WHERE concept_id = $1",
        concept_id)
    await conn.execute("DELETE FROM concepts WHERE id = $1", concept_id)


async def _queue_derive(conn, works) -> list[int]:
    """Queue a re-derive of these works for every maintained profile."""
    if not works:
        return []
    return [await worker.enqueue_derive(conn, p["id"], works)
            for p in await conn.fetch(
                "SELECT id FROM model_profiles WHERE maintained ORDER BY id")]


async def _rederive_concept(conn, concept_id: int) -> list[int]:
    named = [r["term"] for r in await conn.fetch(
        "SELECT term FROM term_map WHERE concept_id = $1 OR concept_id "
        "IN (SELECT id FROM concepts WHERE parent_id = $1) "
        "UNION SELECT slug FROM concepts WHERE id = $1", concept_id)]
    return await _queue_derive(conn, await derive.works_naming(conn, named))


@router.get("/api/concepts")
async def list_concepts(kind: str = Query(...)):
    """Every concept of one kind, with the strings that name it."""
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.kind, c.slug, c.display_zh, c.note, c.parent_id,
                   concept_display_zh(c.id) AS display,
                   COALESCE(json_agg(json_build_object('id', t.id,
                                                       'term', t.term,
                                                       'search_only',
                                                       t.search_only)
                            ORDER BY t.term)
                            FILTER (WHERE t.id IS NOT NULL),
                            '[]') AS terms
            FROM concepts c
            LEFT JOIN term_map t ON t.concept_id = c.id
            WHERE c.kind = $1
            GROUP BY c.id
            ORDER BY c.slug
            """,
            kind)
        return [{**dict(r), "terms": json.loads(r["terms"])} for r in rows]


@router.post("/api/concepts")
async def create_concept(req: ConceptRequest):
    """Record that a thing exists, and which strings name it."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            cid = await terms_module.concept(
                conn, req.kind, req.slug,
                display_zh=req.display_zh, note=req.note)
            mapped = [await terms_module.map_term(
                          conn, term, cid, search_only=req.search_only)
                      for term in req.terms or []]
            jobs = await _rederive_concept(conn, cid)
    await state.entity_mgr.refresh_cache()
    return {"id": cid, "mapped": [m for m in mapped if m is not None],
            "jobs": jobs}


@router.patch("/api/concepts/{concept_id}")
async def edit_concept(concept_id: int, req: ConceptEdit):
    """Set the fields sent: an empty name or note clears it, a null
    parent detaches. A new slug or parent re-derives the concept's works."""
    fields = req.model_dump(exclude_unset=True)
    async with state.store.acquire() as conn:
        async with conn.transaction():
            row = await _concept(conn, concept_id)
            if "slug" in fields:
                fields["slug"] = (fields["slug"] or "").strip()
                if not fields["slug"]:
                    raise refuse(400, "slug_required", "a slug cannot be empty")
                if fields["slug"] == row["slug"]:
                    del fields["slug"]
                else:
                    _fixed(row)
                    other = await conn.fetchval(
                        "SELECT id FROM concepts WHERE kind = $1 "
                        "AND lower(slug) = lower($2) AND id <> $3",
                        row["kind"], fields["slug"], concept_id)
                    if other is not None:
                        raise refuse(409, "concept_exists",
                                     f"a {row['kind']} named "
                                     f"{fields['slug']!r} exists",
                                     id=other, slug=fields["slug"])
            parent = fields.get("parent_id")
            if parent is not None:
                kind = await conn.fetchval(
                    "SELECT kind FROM concepts WHERE id = $1", parent)
                if kind != "series":
                    raise refuse(400, "parent_not_series",
                                 "a parent must be a series concept",
                                 parent_id=parent)
                if concept_id in await _ancestors(conn, parent):
                    raise refuse(409, "parent_cycle",
                                 "that parent sits below this concept",
                                 parent_id=parent)
            if "parent_id" in fields and parent == row["parent_id"]:
                del fields["parent_id"]
            await terms_module.update_concept(conn, concept_id, **fields)
            works = (await _works_of(conn, [concept_id])
                     if {"slug", "parent_id"} & fields.keys() else [])
            jobs = await _queue_derive(conn, works)
    await _vocabulary_changed()
    return {"id": concept_id, "works": len(works), "jobs": jobs}


@router.post("/api/concepts/{concept_id}/merge")
async def merge_concept(concept_id: int, req: MergeRequest):
    """Move a concept's terms, claims and children onto another of its kind,
    then delete it. Where both hold the same term or claim, `into` wins; the
    old slug stays behind as a search alias of `into`."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            src = await _concept(conn, concept_id)
            dst = await _concept(conn, req.into)
            if src["id"] == dst["id"]:
                raise refuse(400, "merge_into_self",
                             "a concept cannot merge into itself")
            if src["kind"] != dst["kind"]:
                raise refuse(409, "concept_kind_mismatch",
                             f"cannot merge a {src['kind']} into a "
                             f"{dst['kind']}", kind=src["kind"],
                             into_kind=dst["kind"])
            _fixed(src)
            if concept_id in await _ancestors(conn, req.into):
                raise refuse(409, "parent_cycle",
                             "the target sits below this concept",
                             parent_id=req.into)
            children = [r["id"] for r in await conn.fetch(
                "SELECT id FROM concepts WHERE parent_id = $1", concept_id)]
            works = await _works_of(conn, [concept_id, *children])
            await conn.execute(
                "DELETE FROM term_map s WHERE s.concept_id = $1 AND EXISTS "
                "(SELECT 1 FROM term_map d WHERE d.concept_id = $2 "
                " AND lower(d.term) = lower(s.term))", concept_id, req.into)
            moved_terms = await conn.fetchval(
                "WITH m AS (UPDATE term_map SET concept_id = $2 "
                "WHERE concept_id = $1 RETURNING 1) SELECT COUNT(*) FROM m",
                concept_id, req.into)
            await terms_module.map_term(conn, src["slug"], req.into,
                                        search_only=True)
            await conn.execute(
                "DELETE FROM work_claims s WHERE s.concept_id = $1 AND EXISTS "
                "(SELECT 1 FROM work_claims d WHERE d.concept_id = $2 "
                " AND d.work_id = s.work_id AND d.field = s.field)",
                concept_id, req.into)
            moved_claims = await conn.fetchval(
                "WITH m AS (UPDATE work_claims SET concept_id = $2 "
                "WHERE concept_id = $1 RETURNING 1) SELECT COUNT(*) FROM m",
                concept_id, req.into)
            await conn.execute(
                "UPDATE concepts SET parent_id = $2 WHERE parent_id = $1",
                concept_id, req.into)
            jobs = await _queue_derive(conn, works)
            await _drop_concept(conn, concept_id)
    await _vocabulary_changed()
    return {"id": req.into, "merged": concept_id, "terms": moved_terms,
            "claims": moved_claims, "children": len(children),
            "works": len(works), "jobs": jobs}


@router.delete("/api/concepts/{concept_id}")
async def delete_concept(concept_id: int):
    """Delete a concept nothing claims or hangs under, with its terms."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            row = await _concept(conn, concept_id)
            _fixed(row)
            claims = await conn.fetchval(
                "SELECT COUNT(*) FROM work_claims WHERE concept_id = $1",
                concept_id)
            children = await conn.fetchval(
                "SELECT COUNT(*) FROM concepts WHERE parent_id = $1",
                concept_id)
            if claims or children:
                raise refuse(409, "concept_in_use",
                             f"{claims} claims and {children} children refer "
                             f"to this concept; merge it instead",
                             claims=claims, children=children)
            works = await _works_of(conn, [concept_id])
            await conn.execute(
                "DELETE FROM term_map WHERE concept_id = $1", concept_id)
            jobs = await _queue_derive(conn, works)
            await _drop_concept(conn, concept_id)
    await _vocabulary_changed()
    return {"id": concept_id, "deleted": True, "works": len(works),
            "jobs": jobs}


@router.get("/api/terms")
async def lookup_term(q: str = Query(...), search_only: bool | None = None):
    """What a string currently means, with the mapping ids."""
    async with state.store.acquire() as conn:
        return {"term": q, "mappings": await terms_module.lookup(
            conn, q, search_only=search_only)}


@router.patch("/api/terms/{term_id}")
async def edit_term(term_id: int, req: TermEdit):
    """Switch a mapping between naming works and only widening a search,
    and rebuild what it names."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            term = await terms_module.set_search_only(
                conn, term_id, req.search_only)
            if term is None:
                raise refuse(404, "term_not_found", "no such mapping")
            jobs = await _queue_derive(
                conn, await derive.works_naming(conn, [term]))
    await _vocabulary_changed()
    return {"id": term_id, "term": term, "search_only": req.search_only,
            "jobs": jobs}


@router.delete("/api/terms/{term_id}")
async def unmap_term(term_id: int):
    """Delete a mapping, and rebuild what it named."""
    async with state.store.acquire() as conn:
        async with conn.transaction():
            term = await conn.fetchval(
                "SELECT term FROM term_map WHERE id = $1", term_id)
            if not await terms_module.unmap_term(conn, term_id):
                raise refuse(404, "term_not_found", "no such mapping")
            jobs = await _queue_derive(
                conn, await derive.works_naming(conn, [term]))
    await state.entity_mgr.refresh_cache()
    return {"id": term_id, "deleted": True, "jobs": jobs}


@router.get("/api/resolve")
async def resolve_name(name: str = Query(...), kind: str | None = None):
    """What a name means, per the curated mappings."""
    result = await state.entity_mgr.resolve_name(name, kind)
    return result or {"error": "not found"}


@router.get("/api/vocabulary")
async def vocabulary(collection: str | None = Query(None)):
    """Every tag a search in this library could actually land on.

    What the box suggests has to be what the library holds. Suggesting
    from the translation table instead offers terms most of which match no
    work here, and withholds the tags that do match but have no Chinese
    name yet — every character tag among them. A name is a display
    detail; existing is not.

    `works` is what orders the list, because a term on 200 works is a
    better guess at what someone typing three letters meant than a term
    on one.
    """
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.tag, t.category,
                   COUNT(DISTINCT t.work_id) AS works,
                   e.display_zh
            FROM effective_tags t
            JOIN work_search w ON w.id = t.work_id
            LEFT JOIN tag_translations e ON e.tag = t.tag
            WHERE ($1::text IS NULL OR w.collection = $1)
              AND t.visible
            GROUP BY t.tag, t.category, e.display_zh
            ORDER BY COUNT(DISTINCT t.work_id) DESC
            """,
            collection)
        fields = await conn.fetch(
            """
            SELECT t.term, c.kind, c.slug,
                   concept_display_zh(c.id) AS name,
                   (SELECT COUNT(DISTINCT cr.work_id)
                    FROM work_claims_ranked cr
                    WHERE cr.field = c.kind AND cr.concept_id = c.id) AS works
            FROM term_map t
            JOIN concepts c ON c.id = t.concept_id
            WHERE c.kind = ANY($1::text[])
            """,
            list(FILTER_KINDS))

    seen: set[str] = set()
    out = [
        {"tag": r["tag"], "category": r["category"], "works": r["works"],
         "zh": r["display_zh"], "key": r["tag"]}
        for r in rows
    ]
    for r in fields:
        if r["term"] in seen or not r["works"]:
            continue
        seen.add(r["term"])
        out.append({"tag": r["term"], "category": r["kind"],
                    "works": r["works"], "zh": r["name"],
                    "key": f"{r['kind']}:{r['slug']}"})
    out.sort(key=lambda x: -x["works"])
    return out


@router.get("/api/translations")
async def translations():
    """Every tag's Chinese name, as stored.

    Read from `entities`, which is where an edited display name lands. The
    file this used to return was a seed, so a name corrected in the
    database never reached the page that shows it.
    """
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tag AS canonical, display_zh FROM tag_translations")
    return {r["canonical"]: r["display_zh"] for r in rows}


@router.post("/api/translations")
async def import_translations(mapping: dict[str, str]):
    """Seed display names in bulk, from the shape `GET` already returns.

    Insert-only: a name already stored has possibly been edited by hand,
    and running an import twice must not undo that. So this adds what is
    missing and reports how much, rather than claiming to have set
    everything it was given.
    """
    added = await state.entity_mgr.import_translations(mapping)
    await _vocabulary_changed()
    return {"read": len(mapping), "added": added}


class TranslationRequest(BaseModel):
    display_zh: str


@router.put("/api/translations/{tag:path}")
async def set_translation(tag: str, req: TranslationRequest):
    """Write one tag's Chinese name over whatever was there; empty deletes."""
    zh = req.display_zh.strip() or None
    async with state.store.acquire() as conn:
        if zh:
            await conn.execute(
                "INSERT INTO tag_translations (tag, display_zh) VALUES ($1, $2) "
                "ON CONFLICT (tag) DO UPDATE SET display_zh = EXCLUDED.display_zh",
                tag, zh)
        else:
            await conn.execute(
                "DELETE FROM tag_translations WHERE tag = $1", tag)
    await _vocabulary_changed()
    return {"tag": tag, "display_zh": zh}
