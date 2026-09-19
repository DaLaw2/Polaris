"""The tag translation dictionary, hidden tags, and user claims.

Naming lives in `concepts` / `term_map`, written through `terms`; this
reads them for display and for the search vocabulary.
"""

from polaris.derivation import derive
from polaris.search.engine import FILTER_KINDS

from . import terms

ENTITY_SCHEMA_SQL = """
DROP TABLE IF EXISTS entity_aliases;
DROP TABLE IF EXISTS tag_rules;
"""

TAG_FIELDS = {"general": "tag", "character": "character",
              "copyright": "series", "artist": "artist"}
FIELD_CATEGORIES = {v: k for k, v in TAG_FIELDS.items()}


class EntityManager:
    """Manages entity mapping, tag visibility, and user overrides."""

    def __init__(self, store):
        self._store = store
        self._term_to_tag: dict[str, str] = {}
        self._synonyms: dict[str, list[str]] = {}
        self._term_to_filter: dict[str, tuple[str, str]] = {}
        self._version: tuple | None = None

    async def init_schema(self) -> None:
        """Create tables and populate defaults."""
        async with self._store.acquire() as conn:
            await conn.execute(ENTITY_SCHEMA_SQL)

        await self.refresh_cache()

    async def import_translations(self, translations: dict[str, str]) -> int:
        """Seed tag display names from a mapping. Returns rows inserted."""
        added = 0
        async with self._store.acquire() as conn:
            for tag_en, tag_zh in translations.items():
                spaced = tag_en.replace("_", " ")
                if not tag_zh or tag_zh in (tag_en, spaced):
                    continue
                row = await conn.fetchval("""
                    INSERT INTO tag_translations (tag, display_zh)
                    VALUES ($1, $2)
                    ON CONFLICT (tag) DO NOTHING
                    RETURNING tag
                """, tag_en, tag_zh)
                added += row is not None
        return added

    async def vocabulary_version(self) -> tuple:
        """A value that changes whenever anything these caches read changes."""
        async with self._store.acquire() as conn:
            return tuple(await conn.fetchrow("""
                SELECT (SELECT COUNT(*) FROM term_map),
                       (SELECT MAX(created_at) FROM term_map),
                       (SELECT COUNT(*) FROM concepts),
                       (SELECT MAX(created_at) FROM concepts),
                       (SELECT COUNT(*) FROM hidden_tags)
            """))

    async def refresh_if_changed(self) -> bool:
        """Reload the caches when the vocabulary has moved under them.

        Returns whether anything was reloaded.
        """
        version = await self.vocabulary_version()
        if version == self._version:
            return False
        await self.refresh_cache()
        return True

    async def refresh_cache(self) -> None:
        """Refresh in-memory caches from DB."""
        self._version = await self.vocabulary_version()
        async with self._store.acquire() as conn:
            await self._build_tag_index(conn)
            await self._load_term_map(conn)

    async def _build_tag_index(self, conn) -> None:
        """Index every known tag by its Chinese name and spaced English name.

        Ties go to the candidate with the highest document frequency in
        this collection, so a query lands on a tag that is actually here.
        """
        rows = await conn.fetch(
            "SELECT tag, COUNT(DISTINCT work_id) AS df FROM effective_tags "
            "GROUP BY tag"
        )
        df = {r["tag"]: r["df"] for r in rows}

        named = await conn.fetch(
            "SELECT tag AS canonical, display_zh FROM tag_translations")

        index: dict[str, str] = {}

        def offer(term: str, tag: str) -> None:
            key = term.strip().lower()
            if not key or key == tag:
                return
            current = index.get(key)
            if current is None or df.get(tag, 0) > df.get(current, 0):
                index[key] = tag

        for r in named:
            tag_en = r["canonical"]
            if r["display_zh"]:
                offer(r["display_zh"], tag_en)
            offer(tag_en.replace("_", " "), tag_en)

        self._term_to_tag = index

    async def _load_term_map(self, conn) -> None:
        """Cache the curated query vocabulary: synonyms and field filters."""
        rows = await conn.fetch(
            """
            SELECT t.term, c.id AS concept_id, c.kind, c.slug
            FROM term_map t
            JOIN concepts c ON c.id = t.concept_id
            """)

        by_concept: dict[int, list[str]] = {}
        filters: dict[str, tuple[str, str]] = {}
        for r in rows:
            if r["kind"] in FILTER_KINDS:
                filters[r["term"].strip().lower()] = (r["kind"], r["slug"])
            elif r["kind"] == "tag":
                by_concept.setdefault(r["concept_id"], []).append(r["term"])

        synonyms: dict[str, list[str]] = {}
        for members in by_concept.values():
            if len(members) < 2:
                continue
            for m in members:
                synonyms[m] = [o for o in members if o != m]

        self._synonyms = synonyms
        self._term_to_filter = filters

    def resolve_tag(self, term: str) -> str | None:
        """Map a query term in any language onto a Danbooru tag name."""
        return self._term_to_tag.get(term.strip().lower())

    def expand_synonyms(self, tag: str) -> list[str]:
        """Return `tag` together with any tags declared synonymous to it."""
        extra = self._synonyms.get(tag)
        return [tag, *extra] if extra else [tag]

    def resolve_filter(self, term: str) -> tuple[str, str] | None:
        """Map a term onto a (field, value) filter, or None if it is a tag."""
        return self._term_to_filter.get(term.strip().lower())

    async def hide_tag(self, tag: str) -> None:
        """Hide a tag from UI display."""
        async with self._store.acquire() as conn:
            await conn.execute(
                "INSERT INTO hidden_tags (tag) VALUES ($1) "
                "ON CONFLICT (tag) DO NOTHING", tag)

    async def show_tag(self, tag: str) -> None:
        """Un-hide a tag."""
        async with self._store.acquire() as conn:
            await conn.execute("DELETE FROM hidden_tags WHERE tag = $1", tag)

    async def list_hidden(self) -> list[dict]:
        """List all hidden tags."""
        async with self._store.acquire() as conn:
            rows = await conn.fetch("SELECT tag FROM hidden_tags ORDER BY tag")
            return [{"tag": r["tag"]} for r in rows]

    async def add_work_override(
        self,
        work_id: int,
        action: str,
        tag_category: str | None = None,
        original_tag: str | None = None,
        new_tag: str | None = None,
        field_name: str | None = None,
        field_value: str | None = None,
    ) -> int:
        """Record that the user said something about a work.

        `add` and `set_field` claim a value, `remove` claims it is not so.
        Both are one row per work, field and concept, so changing one's
        mind flips the row rather than adding another. Setting a
        single-valued field drops the person's earlier values for it.
        """
        if action == "set_field":
            field, value, negated = field_name, field_value, False
        elif action in ("add", "remove"):
            field = TAG_FIELDS.get(tag_category or "general")
            value = new_tag if action == "add" else original_tag
            negated = action == "remove"
        else:
            raise ValueError(f"unknown override action {action!r}")
        if field is None:
            raise ValueError(f"no claim field for tag category {tag_category!r}")

        async with self._store.acquire() as conn:
            async with conn.transaction():
                if await conn.fetchval(
                        "SELECT multi FROM claim_fields WHERE field = $1",
                        field) is None:
                    raise ValueError(f"{field!r} is not a claim field")
                concept_id = await terms.resolve_concept(conn, field, value)
                return await record_claim(conn, work_id, field, concept_id,
                                          negated=negated)

    async def get_work_overrides(self, work_id: int) -> list[dict]:
        """Every claim a person has made about a work."""
        async with self._store.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.id, c.field, co.slug AS value, c.negated, c.created_at "
                "FROM work_claims c JOIN concepts co ON co.id = c.concept_id "
                "WHERE c.work_id = $1 ORDER BY c.created_at, c.id",
                work_id,
            )
        out = []
        for r in rows:
            tagged = r["field"] == "tag" or r["negated"]
            action = ("remove" if r["negated"]
                      else "add" if r["field"] == "tag" else "set_field")
            out.append({
                "id": r["id"],
                "field": r["field"],
                "value": r["value"],
                "negated": r["negated"],
                "action": action,
                "tag_category": FIELD_CATEGORIES.get(r["field"]) if tagged else None,
                "original_tag": r["value"] if action == "remove" else None,
                "new_tag": r["value"] if action == "add" else None,
                "field_name": r["field"] if action == "set_field" else None,
                "field_value": r["value"] if action == "set_field" else None,
                "created_at": r["created_at"],
            })
        return out

    async def delete_work_override(self, override_id: int) -> None:
        """Delete a claim."""
        async with self._store.acquire() as conn:
            async with conn.transaction():
                work_id = await conn.fetchval(
                    "DELETE FROM work_claims WHERE id = $1 RETURNING work_id",
                    override_id)
                await derive.derive_works(conn, [work_id])

    async def resolve_name(self, name: str, kind: str | None = None) -> dict | None:
        """What a name means, per the curated mappings."""
        async with self._store.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT c.kind, c.slug AS canonical,
                       concept_display_zh(c.id) AS display_zh
                FROM concepts c
                LEFT JOIN term_map t ON t.concept_id = c.id
                WHERE (lower(t.term) = lower($1) OR lower(c.slug) = lower($1))
                  AND ($2::text IS NULL OR c.kind = $2)
                ORDER BY (lower(c.slug) = lower($1)) DESC
                LIMIT 1
                """,
                name, kind)
            return dict(row) if row else None


async def record_claim(conn, work_id: int, field: str, concept_id: int, *,
                       negated: bool = False) -> int:
    """Write one claim and re-derive the work. A value for a single-valued
    field replaces the person's earlier values; a negation replaces nothing."""
    multi = await conn.fetchval(
        "SELECT multi FROM claim_fields WHERE field = $1", field)
    if not multi and not negated:
        await conn.execute(
            "DELETE FROM work_claims WHERE work_id = $1 "
            "AND field = $2 AND concept_id <> $3 AND NOT negated",
            work_id, field, concept_id)
    claim_id = await terms.claim(conn, work_id, field, concept_id,
                                 negated=negated)
    await derive.derive_works(conn, [work_id])
    return claim_id
