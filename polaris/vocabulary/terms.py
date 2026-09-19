"""Writing to the authoritative layer: concepts, term mappings, claims.

    from polaris import terms

    async with store.acquire() as conn:
        cid = await terms.concept(conn, "artist", "some_artist",
                                  display_zh="某作者")
        await terms.map_term(conn, "Some Artist", cid, search_only=True)

Nothing here interprets a string. `concept()` records that a thing exists,
`map_term()` records that a string names it, and whether the string only
widens a search or may also name a work a model tagged with it.
"""

from __future__ import annotations


class TermError(ValueError):
    """A mapping that could not be made, said in one sentence."""


async def concept(
    conn,
    kind: str,
    slug: str,
    *,
    display_zh: str | None = None,
    parent_id: int | None = None,
    note: str | None = None,
) -> int:
    """Get or create a concept, returning its id. A value already set is
    never replaced by a missing one."""
    slug = (slug or "").strip()
    if not slug:
        raise TermError("a concept needs a non-empty slug")
    if not (kind or "").strip():
        raise TermError(f"concept {slug!r} needs a kind")

    return await conn.fetchval(
        """
        INSERT INTO concepts (kind, slug, display_zh, parent_id, note)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (kind, slug) DO UPDATE SET
            display_zh = COALESCE(EXCLUDED.display_zh, concepts.display_zh),
            parent_id  = COALESCE(EXCLUDED.parent_id,  concepts.parent_id),
            note       = COALESCE(EXCLUDED.note,       concepts.note)
        RETURNING id
        """,
        kind.strip(), slug, display_zh, parent_id, note,
    )


EDITABLE = ("display_zh", "note", "slug", "parent_id")


async def update_concept(conn, concept_id: int, **fields) -> bool:
    """Set the given fields exactly; a blank name or note clears it."""
    sets = {k: v for k, v in fields.items() if k in EDITABLE}
    if not sets:
        return await conn.fetchval(
            "SELECT TRUE FROM concepts WHERE id = $1", concept_id) is not None
    sql = ", ".join(
        f"{k} = NULLIF(btrim(${i}), '')" if k in ("display_zh", "note")
        else f"{k} = ${i}"
        for i, k in enumerate(sets, start=2))
    return await conn.fetchval(
        f"UPDATE concepts SET {sql} WHERE id = $1 RETURNING TRUE",
        concept_id, *sets.values()) is not None


async def resolve_concept(conn, kind: str, name: str) -> int:
    """The concept of this kind a string names, created when none does."""
    found = await find_concept(conn, kind, name)
    if found is not None:
        return found
    return await concept(conn, kind, name.strip())


async def find_concept(conn, kind: str, name: str) -> int | None:
    """The concept of this kind a string names, by slug or term, or None."""
    name = (name or "").strip()
    if not name:
        raise TermError("a claim needs a non-empty value")
    return await conn.fetchval(
        """
        SELECT c.id FROM concepts c
        WHERE c.kind = $1
          AND (lower(c.slug) = lower($2)
               OR EXISTS (SELECT 1 FROM term_map t
                          WHERE t.concept_id = c.id
                            AND lower(t.term) = lower($2)))
        ORDER BY (c.slug = $2) DESC, (lower(c.slug) = lower($2)) DESC, c.id
        LIMIT 1
        """, kind, name)


async def map_term(
    conn,
    term: str,
    concept_id: int,
    *,
    search_only: bool = False,
) -> int | None:
    """Record that `term` names this concept.

    Returns the new row's id, or None if the mapping was already there.
    """
    term = (term or "").strip()
    if not term:
        return None
    return await conn.fetchval(
        "INSERT INTO term_map (term, search_only, concept_id) "
        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING id",
        term, search_only, concept_id)


async def set_search_only(conn, term_id: int, search_only: bool) -> str | None:
    """Switch what a mapping is for. Returns its term, or None if absent."""
    return await conn.fetchval(
        "UPDATE term_map SET search_only = $2 WHERE id = $1 RETURNING term",
        term_id, search_only)


async def unmap_term(conn, term_id: int) -> bool:
    """Delete a mapping."""
    return await conn.fetchval(
        "DELETE FROM term_map WHERE id = $1 RETURNING TRUE", term_id) is not None


async def claim(
    conn,
    work_id: int,
    field: str,
    concept_id: int,
    *,
    negated: bool = False,
) -> int:
    """State something about one work: "the artist of #12 is X", or with
    `negated`, that it is not."""
    known = await conn.fetchval(
        "SELECT 1 FROM claim_fields WHERE field = $1", field)
    if not known:
        fields = [r["field"] for r in await conn.fetch(
            "SELECT field FROM claim_fields ORDER BY field")]
        raise TermError(
            f"{field!r} is not a claim field. Known: {', '.join(fields)}")
    return await conn.fetchval(
        """
        INSERT INTO work_claims (work_id, field, concept_id, negated)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (work_id, field, concept_id) DO UPDATE
            SET negated = EXCLUDED.negated, created_at = NOW()
        RETURNING id
        """,
        work_id, field, concept_id, negated)


async def lookup(conn, term: str, *,
                 search_only: bool | None = None) -> list[dict]:
    """What a string currently means. Useful before mapping."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.term, t.search_only,
               c.id AS concept_id, c.kind, c.slug,
               concept_display_zh(c.id) AS display_zh
        FROM term_map t JOIN concepts c ON c.id = t.concept_id
        WHERE lower(t.term) = lower($1)
          AND ($2::boolean IS NULL OR t.search_only = $2)
        ORDER BY c.kind, c.slug
        """,
        term, search_only)
    return [dict(r) for r in rows]
