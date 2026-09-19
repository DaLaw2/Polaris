"""Search engine: tag queries, similarity search, and hybrid queries.

Supports three query modes:
1. Tag search: filter by exact tags (AND/OR)
2. Similarity search: find works similar to a reference by embedding distance
3. Hybrid: filter by tags, rank by similarity
"""

import math
import re
from typing import Protocol

import numpy as np

from polaris import config
from polaris.models import profiles
from polaris.shared.db import Store

from .domain import SearchResult
from .queries import row_to_work, tags_by_work

LEAD = ""


def resolve_collection(collection: str | None) -> list[str] | None:
    """The collections to search, or None to narrow by none.

    Always a list, so callers have one shape to bind. LEAD asks the
    configuration, which answers with everything marked searchable.
    """
    if collection == LEAD:
        return config.searchable_collections()
    if not collection:
        return None
    names = [n.strip() for n in collection.split(",") if n.strip()]
    return names or None


class TagVocabulary(Protocol):
    """What the engine needs to turn query terms into tag names.

    Structural typing, like TaggerBackend: EntityManager satisfies this
    without importing anything from here, and tests can pass a stub.
    """

    def resolve_tag(self, term: str) -> str | None:
        """Map a term in any language onto a Danbooru tag, or None."""
        ...

    def expand_synonyms(self, tag: str) -> list[str]:
        """Return the tag plus any tags declared synonymous to it."""
        ...

    def resolve_filter(self, term: str) -> tuple[str, str] | None:
        """Map a term onto a (field, value) filter, or None if it is a tag."""
        ...

FILTER_KINDS = ("language", "rating", "color_mode", "work_type",
                "artist", "series")


HAS_COPIES = "EXISTS (SELECT 1 FROM work_copies c WHERE c.work_id = w.id)"


class SearchEngine:
    """Multi-mode search engine backed by PostgreSQL + pgvector."""

    EFFECTIVE_CTE = ""

    PAGING_TIEBREAK = ", w.id DESC"

    @staticmethod
    def _with(cte: str) -> str:
        """`WITH …` when there is a CTE, nothing when there is not."""
        return f"WITH {cte}" if cte.strip() else ""

    def __init__(self, store: Store, vocabulary: TagVocabulary | None = None):
        self.store = store
        self.vocabulary = vocabulary

    def _expand(self, tag: str) -> list[str]:
        """Query tag plus its declared synonyms (just the tag if none)."""
        if self.vocabulary is None:
            return [tag]
        return self.vocabulary.expand_synonyms(tag)

    def _resolve(self, terms: list[str]) -> list[str]:
        """Map query terms in any language onto Danbooru tag names."""
        if self.vocabulary is None:
            return terms
        return [self.vocabulary.resolve_tag(t) or t for t in terms]

    OVERLAY_FIELDS = ("artist", "series", "language")

    @staticmethod
    def _filter_condition(field_name: str, value: str, bind) -> str:
        """One field filter from a typed query term.

        Rating is the one field where asking for a value does not mean
        asking for exactly that value. This was written out twice, in
        `search_tags` and in `search_hybrid`, as the literal
        `w.rating IN ('questionable', 'explicit')` guarded by
        `value == 'explicit'` — a rule about which rating outranks which,
        stated in Python, in two places, while `rating_order` sat in the
        database holding precisely that ordering and being consulted by
        nobody. Adding a fifth rating meant finding both copies.

        The rule the ranks express: a rating term means that rating and
        everything stronger. The single exception is the mildest rating,
        which is the floor of the scale — "at or above the floor" is every
        work in the library, which is not what anyone typing it means, so
        the floor matches exactly itself. A value that is not a rating at
        all matches nothing, exactly as the equality test it replaces did.
        """
        if field_name in SearchEngine.OVERLAY_FIELDS:
            return f"""
                EXISTS (
                    SELECT 1 FROM work_claims_ranked cr
                    WHERE cr.work_id = w.id
                      AND cr.field = {bind(field_name)}
                      AND cr.value = {bind(value)})
            """
        if field_name != "rating":
            return f"w.{field_name} = {bind(value)}"
        v = bind(value)
        return f"""
            w.rating IN (
                SELECT r.name
                FROM rating_order r, rating_order q
                WHERE q.name = {v}
                  AND (r.name = q.name
                       OR (r.rank >= q.rank
                           AND q.rank > (SELECT MIN(rank) FROM rating_order))))
        """

    @staticmethod
    def _claim_layer(filters: list[tuple[str, str]], bind) -> str:
        """The strongest claimant that answered a field term, per work, so
        a person's answer ranks above a model's. Empty when no overlay
        field was asked for."""
        overlay = [(f, v) for f, v in filters
                   if f in SearchEngine.OVERLAY_FIELDS]
        if not overlay:
            return ""
        fields = bind([f for f, _ in overlay])
        values = bind([v for _, v in overlay])
        return f"""
            claim_layer AS (
                SELECT DISTINCT ON (cr.work_id)
                       cr.work_id, cr.priority
                FROM work_claims_ranked cr
                JOIN unnest({fields}::text[], {values}::text[])
                     AS q(field, value)
                  ON q.field = cr.field AND q.value = cr.value
                ORDER BY cr.work_id, cr.priority DESC
            )
        """

    async def _relevance(
        self, conn, tags: list[str], bind, collection: str | None = None
    ) -> tuple[str, str]:
        """Build the relevance CTE and ORDER BY for a tag query.

        Score is the sum over matched tags of `idf(tag) × confidence`.
        IDF stops a tag like `blush` — present in nearly every work —
        from counting as much as a rare one; confidence then separates a
        work that scored 0.95 on a tag from one that scored 0.41, which
        is the only signal left in AND mode where every result matches
        every tag.

        Computing this in SQL is the point: the score has to exist before
        ORDER BY and LIMIT run, or the page returns the newest matches
        rather than the best ones.

        Args:
            conn: Open connection (document frequencies are read fresh).
            tags: Query tags to weight by.
            bind: Callable that appends a parameter and returns its
                placeholder, so numbering stays consistent with the
                caller's WHERE clause.

        Returns:
            (cte_fragment, order_by). Both empty when there are no tags —
            relevance is undefined for a pure facet browse.
        """
        if not tags:
            return "", ""

        if collection:
            n_works = await conn.fetchval(
                "SELECT COUNT(*) FROM work_search "
                "WHERE collection = ANY($1::text[])",
                collection
            ) or 1
            rows = await conn.fetch(
                """SELECT et.tag, COUNT(DISTINCT et.work_id) AS df
                   FROM effective_tags et
                   JOIN work_search w ON w.id = et.work_id
                   WHERE et.tag = ANY($1::text[])
                     AND w.collection = ANY($2::text[])
                   GROUP BY et.tag""",
                tags, collection,
            )
        else:
            n_works = await conn.fetchval("SELECT COUNT(*) FROM works") or 1
            rows = await conn.fetch(
                """SELECT tag, COUNT(DISTINCT work_id) AS df
                   FROM effective_tags WHERE tag = ANY($1::text[])
                   GROUP BY tag""",
                tags,
            )
        df = {r["tag"]: r["df"] for r in rows}

        weights = [math.log(1 + n_works / (1 + df.get(t, 0))) for t in tags]

        total_w = sum(weights) or 1.0

        tag_ph = bind(tags)
        weight_ph = bind([w / total_w for w in weights])
        cte = f"""
            relevance AS (
                SELECT et.work_id, SUM(qw.weight * et.confidence) AS score
                FROM effective_tags et
                JOIN unnest({tag_ph}::text[], {weight_ph}::float8[])
                     AS qw(tag, weight) ON qw.tag = et.tag
                GROUP BY et.work_id
            )
        """
        return cte, "COALESCE(r.score, 0) DESC, w.analyzed_at DESC"

    async def search_tags(
        self,
        tags: list[str],
        mode: str = "AND",
        limit: int = 50,
        collection: str | None = LEAD,
    ) -> list[SearchResult]:
        """Search for works matching the given tags.

        Args:
            tags: List of tag names or aliases.
            mode: 'AND' (all tags required) or 'OR' (any tag matches).
            limit: Maximum results to return.
            collection: Which collection to search. `None` searches all of
                them, which is a deliberate act — the default is the main
                corpus, so a reference collection stays out of the way
                until it is asked for by name.

        Returns:
            List of SearchResult sorted by number of matched tags.
        """
        collection = resolve_collection(collection)
        filters, content_tags = self._parse_query_terms(tags)

        async with self.store.acquire() as conn:

            conditions = ["TRUE"]
            params: list = []

            def _bind(value) -> str:
                params.append(value)
                return f"${len(params)}"

            if collection:
                conditions.append(
                f"w.collection = ANY({_bind(collection)}::text[])")

            for field_name, value in filters:
                conditions.append(
                    self._filter_condition(field_name, value, _bind))

            expanded = [self._expand(t) for t in content_tags]
            if content_tags and mode == "AND":
                for group in expanded:
                    conditions.append(f"""
                        EXISTS (
                            SELECT 1 FROM effective_tags et
                            WHERE et.work_id = w.id
                              AND et.tag = ANY({_bind(group)}::text[])
                        )
                    """)
            elif content_tags and mode == "OR":
                flat = [t for group in expanded for t in group]
                conditions.append(f"""
                    EXISTS (
                        SELECT 1 FROM effective_tags et
                        WHERE et.work_id = w.id
                          AND et.tag = ANY({_bind(flat)}::text[])
                    )
                """)

            where = " AND ".join(conditions)
            rank_tags = [t for group in expanded for t in group]
            rel_cte, rel_order = await self._relevance(
                conn, rank_tags, _bind, collection)
            claim_cte = self._claim_layer(filters, _bind)
            rel_select = "r.score" if rel_cte else "NULL::float8"
            rel_join = "LEFT JOIN relevance r ON r.work_id = w.id" if rel_cte else ""
            claim_join = ("LEFT JOIN claim_layer cl ON cl.work_id = w.id"
                          if claim_cte else "")
            claim_order = "COALESCE(cl.priority, 0) DESC, " if claim_cte else ""
            order = (claim_order + (rel_order or "w.analyzed_at DESC")
                     + self.PAGING_TIEBREAK)

            rows = await conn.fetch(f"""
                {self._with(", ".join(c for c in (rel_cte, claim_cte) if c.strip()))}
                SELECT w.*, {rel_select} AS relevance,
                       (SELECT array_agg(et.tag)
                        FROM effective_tags et
                        WHERE et.work_id = w.id AND et.category = 'general')
                       AS all_tags
                FROM works_effective w
                {rel_join}
                {claim_join}
                WHERE {where}
                ORDER BY {order}
                LIMIT {_bind(limit)}
            """, *params)

            by_work = await tags_by_work(conn, (r["id"] for r in rows))
            results = []
            for row in rows:
                work = row_to_work(row, by_work[row["id"]])
                matched = [t for t in content_tags if t in (row["all_tags"] or [])]
                results.append(SearchResult(
                    work=work,
                    score=float(row["relevance"] or 0.0),
                    matched_tags=matched,
                ))

            return results

    _TEXT_FIELDS = ("artist", "series", "title")
    _ENUM_FIELDS = ("rating", "color_mode", "work_type", "language")
    _MAPPED_FIELDS = {"artist": "artist", "series": "series"}

    @staticmethod
    async def _mapped_values(conn, kind: str, like: str) -> list[str]:
        """Every stored spelling of a concept one of whose terms matches."""
        rows = await conn.fetch(
            """SELECT DISTINCT lower(v.name) AS name
               FROM concepts c
               JOIN term_map t ON t.concept_id = c.id
               CROSS JOIN LATERAL (VALUES (c.slug), (t.term)) AS v(name)
               WHERE c.kind = $1
                 AND EXISTS (SELECT 1 FROM term_map m
                             WHERE m.concept_id = c.id
                               AND m.term ILIKE $2)""",
            kind, like)
        return [r["name"] for r in rows]

    async def search_faceted(
        self,
        *,
        tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        category_tags: dict[str, list[str]] | None = None,
        text: dict[str, str] | None = None,
        enums: dict[str, list[str]] | None = None,
        mode: str = "AND",
        sort: str = "relevance",
        limit: int = 50,
        offset: int = 0,
        with_facets: bool = True,
        collection: str | None = LEAD,
        copies: bool = False,
    ) -> dict:
        """Search across every dimension at once, with facet counts.

        The plain tag search can only answer "which works carry this tag".
        This answers "works by this artist, in this series, that are full
        colour, tagged X but not Y" — which is what the collection is
        actually browsed by.

        Args:
            tags: Free tag terms, matched against any category.
            exclude_tags: Terms whose presence disqualifies a work.
            category_tags: Category-scoped terms, e.g.
                {"character": [...], "copyright": [...]}.
            text: Substring filters on metadata columns (see _TEXT_FIELDS).
            enums: Exact multi-value filters (see _ENUM_FIELDS).
            mode: "AND" (every tag) or "OR" (any tag) for `tags`.
            sort: relevance | newest | oldest | title | pages.
            collection: Which collection to search; `None` for all. The
                facet counts follow it, so browsing never offers a value
                that belongs to a collection the results cannot contain.
            copies: Only works a second path repeats. The facet counts
                follow it too.

        Returns:
            {"results": [SearchResult], "total": int, "facets": {...}}
        """
        collection = resolve_collection(collection)
        tags = self._resolve([t for t in (tags or []) if t])
        exclude_tags = self._resolve([t for t in (exclude_tags or []) if t])
        category_tags = {k: [v for v in vs if v]
                         for k, vs in (category_tags or {}).items() if vs}
        text = {k: v for k, v in (text or {}).items()
                if v and k in self._TEXT_FIELDS}
        enums = {k: [v for v in vs if v]
                 for k, vs in (enums or {}).items() if k in self._ENUM_FIELDS and vs}

        conditions: list[str] = ["TRUE"]
        params: list = []

        def _bind(value) -> str:
            """Append a parameter and return its placeholder.

            Binding and numbering have to happen together: a helper that only
            returns the next number hands out the same one twice when a single
            f-string needs two placeholders.
            """
            params.append(value)
            return f"${len(params)}"

        collection_slot = None
        if collection:
            collection_slot = len(params)
            off = _bind(False)
            conditions.append(
                f"({off} OR w.collection = ANY({_bind(collection)}::text[]))")

        if copies:
            conditions.append(HAS_COPIES)

        for field, values in enums.items():
            conditions.append(f"w.{field} = ANY({_bind(values)}::text[])")

        expanded = [self._expand(t) for t in tags]
        if tags:
            if mode.upper() == "OR":
                flat = [t for group in expanded for t in group]
                conditions.append(f"""
                    EXISTS (SELECT 1 FROM effective_tags et
                            WHERE et.work_id = w.id AND et.tag = ANY({_bind(flat)}::text[]))
                """)
            else:
                for group in expanded:
                    conditions.append(f"""
                        EXISTS (SELECT 1 FROM effective_tags et
                                WHERE et.work_id = w.id
                                  AND et.tag = ANY({_bind(group)}::text[]))
                    """)

        for category, values in category_tags.items():
            widened = [t for v in values for t in self._expand(v)]
            conditions.append(f"""
                EXISTS (SELECT 1 FROM effective_tags et
                        WHERE et.work_id = w.id
                          AND et.category = {_bind(category)}
                          AND et.tag = ANY({_bind(widened)}::text[]))
            """)

        if exclude_tags:
            excluded = [t for v in exclude_tags for t in self._expand(v)]
            conditions.append(f"""
                NOT EXISTS (SELECT 1 FROM effective_tags et
                            WHERE et.work_id = w.id AND et.tag = ANY({_bind(excluded)}::text[]))
            """)

        rank_tags = [t for group in expanded for t in group]
        for values in category_tags.values():
            rank_tags.extend(t for v in values for t in self._expand(v))

        async with self.store.acquire() as conn:
            for field, value in text.items():
                like = f"%{value}%"
                kind = self._MAPPED_FIELDS.get(field)
                names = (await self._mapped_values(conn, kind, like)
                         if kind else [])
                if names:
                    conditions.append(
                        f"(w.{field} ILIKE {_bind(like)}"
                        f" OR lower(w.{field}) = ANY({_bind(names)}::text[]))")
                else:
                    conditions.append(f"w.{field} ILIKE {_bind(like)}")

            where = " AND ".join(conditions)

            where_params = list(params)
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM works_effective w WHERE {where}",
                *where_params,
            )

            rel_cte, rel_order = await self._relevance(
                conn, rank_tags, _bind, collection)
            rel_select = "r.score" if rel_cte else "NULL::float8"
            rel_join = "LEFT JOIN relevance r ON r.work_id = w.id" if rel_cte else ""
            order = {
                "relevance": rel_order or "w.analyzed_at DESC",
                "newest": "w.analyzed_at DESC",
                "oldest": "w.analyzed_at ASC",
                "title": "COALESCE(w.title, w.folder_name) ASC",
                "pages": "w.total_images DESC",
            }.get(sort, "w.analyzed_at DESC") + self.PAGING_TIEBREAK

            page_params = params + [limit, offset]
            rows = await conn.fetch(f"""
                {self._with(rel_cte)}
                SELECT w.*, {rel_select} AS relevance,
                       (SELECT array_agg(et.tag) FROM effective_tags et
                        WHERE et.work_id = w.id) AS all_tags
                FROM works_effective w
                {rel_join}
                WHERE {where}
                ORDER BY {order}
                LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *page_params)

            by_work = await tags_by_work(conn, (r["id"] for r in rows))
            results = []
            for row in rows:
                work = row_to_work(row, by_work[row["id"]])
                present = set(row["all_tags"] or [])
                matched = [t for t in rank_tags if t in present]
                results.append(SearchResult(
                    work=work,
                    score=float(row["relevance"] or 0.0),
                    matched_tags=matched,
                ))

            facets = {}
            if with_facets:
                facets = await self._facets(conn, where, where_params)
                scope = list(where_params)
                if collection_slot is not None:
                    scope[collection_slot] = True
                facets["collection"] = [
                    {"value": r["value"], "count": r["count"]}
                    for r in await conn.fetch(
                        f"SELECT w.collection AS value, COUNT(*) AS count "
                        f"FROM works_effective w WHERE {where} "
                        f"GROUP BY w.collection "
                        f"ORDER BY count DESC, value ASC", *scope)]

        return {"results": results, "total": total or 0, "facets": facets}

    COLUMN_FACETS = ("artist", "series", "language",
                     "rating", "color_mode", "work_type")
    TAG_FACETS = ("character", "copyright", "general")
    MODEL_TAG_FACETS = ("character", "copyright")
    FACET_VALUES = 40

    async def _facets(self, conn, where: str, params: list) -> dict:
        """Value counts per dimension across the current result set."""
        arms = ",\n".join(
            f"{f} AS (SELECT '{f}'::text AS field, m.{f} AS value,"
            f" COUNT(*) AS count FROM m"
            f" WHERE m.{f} IS NOT NULL AND m.{f} <> '' GROUP BY m.{f}"
            f" ORDER BY count DESC, value ASC LIMIT {self.FACET_VALUES})"
            for f in self.COLUMN_FACETS)
        rows = await conn.fetch(
            f"WITH m AS MATERIALIZED ("
            f"  SELECT {', '.join('w.' + f for f in self.COLUMN_FACETS)}"
            f"  FROM works_effective w WHERE {where}),\n{arms}\n"
            + "\nUNION ALL ".join(f"SELECT * FROM {f}" for f in self.COLUMN_FACETS),
            *params)
        facets: dict[str, list[dict]] = {f: [] for f in self.COLUMN_FACETS}
        for r in rows:
            facets[r["field"]].append({"value": r["value"], "count": r["count"]})

        cat = f"${len(params) + 1}"
        rows = await conn.fetch(
            f"""
            WITH ids AS MATERIALIZED (
                SELECT w.id FROM works_effective w WHERE {where})
            SELECT category, value, count FROM (
                SELECT et.category, et.tag AS value, COUNT(*) AS count,
                       ROW_NUMBER() OVER (PARTITION BY et.category
                           ORDER BY COUNT(*) DESC, et.tag ASC) AS rn
                FROM effective_tags et JOIN ids ON ids.id = et.work_id
                WHERE et.category = ANY({cat}::text[]) AND et.visible
                GROUP BY et.category, et.tag) x
            WHERE rn <= {self.FACET_VALUES}
            """,
            *params, list(self.MODEL_TAG_FACETS))
        for f in self.TAG_FACETS:
            facets[f] = []
        for r in rows:
            facets[r["category"]].append(
                {"value": r["value"], "count": r["count"]})

        facets["general"] = await self._general_facet(conn, where, params)
        return facets

    async def _general_facet(self, conn, where: str, params: list) -> list[dict]:
        """General tags, counted over the curated ones and bucketed by
        concept. Every tag when nothing is curated.

        The tags a tagger is surest of are the ones every work carries,
        which are the ones nobody browses by.
        """
        vocab = await conn.fetch(
            """SELECT lower(t.term) AS term, c.slug
               FROM term_map t JOIN concepts c ON c.id = t.concept_id
               WHERE c.kind = 'tag' AND NOT t.search_only""")
        if not vocab:
            rows = await conn.fetch(
                f"""
                WITH ids AS MATERIALIZED (
                    SELECT w.id FROM works_effective w WHERE {where})
                SELECT et.tag AS value, COUNT(*) AS count
                FROM effective_tags et JOIN ids ON ids.id = et.work_id
                WHERE et.category = 'general' AND et.visible
                GROUP BY et.tag
                ORDER BY count DESC, value ASC LIMIT {self.FACET_VALUES}
                """, *params)
        else:
            terms = [r["term"] for r in vocab]
            slugs = [r["slug"] for r in vocab]
            t, s = f"${len(params) + 1}", f"${len(params) + 2}"
            rows = await conn.fetch(
                f"""
                WITH ids AS MATERIALIZED (
                    SELECT w.id FROM works_effective w WHERE {where}),
                vocab AS (SELECT * FROM unnest({t}::text[], {s}::text[])
                                        AS v(term, slug))
                SELECT v.slug AS value, COUNT(DISTINCT et.work_id) AS count
                FROM effective_tags et
                JOIN ids ON ids.id = et.work_id
                JOIN vocab v ON v.term = et.tag
                WHERE et.category = 'general' AND et.visible
                  AND et.tag = ANY({t}::text[])
                GROUP BY v.slug
                ORDER BY count DESC, value ASC LIMIT {self.FACET_VALUES}
                """, *params, terms, slugs)
        return [{"value": r["value"], "count": r["count"]} for r in rows]

    async def search_similar(
        self,
        reference_path: str | None = None,
        reference_embedding: list[float] | None = None,
        embedding_type: str = "style",
        top_k: int = 10,
        collection: str | None = LEAD,
        copies: bool = False,
    ) -> list[SearchResult]:
        """Find works similar to a reference work by embedding distance.

        Args:
            reference_path: Folder path of the reference work (looks up its embedding).
            reference_embedding: Direct embedding vector (alternative to reference_path).
            embedding_type: Which embedding to use ('style' or 'wd').
            top_k: Number of results to return.
            collection: Which collection the neighbours may come from.
                Nearest-neighbour search ignores every filter it is not
                given, so without this a look-alike from a collection the
                user never browses would surface among the results.
            copies: Only neighbours a second path repeats.

        Returns:
            List of SearchResult sorted by similarity (highest first).
        """
        collection = resolve_collection(collection)
        async with self.store.acquire() as conn:

            version = await profiles.embedding_version(conn)
            if version is None:
                return []
            reference_id = None
            if reference_embedding is not None:
                emb = np.array(reference_embedding, dtype=np.float32)
            elif reference_path:
                found = await self._reference(conn, reference_path, version)
                if found is None:
                    return []
                reference_id, emb = found
            else:
                return []

            rows = await conn.fetch(f"""
                SELECT w.*, (v.vec <=> $1) AS distance
                FROM works_effective w
                JOIN derived.work_vectors v ON v.work_id = w.id
                                   AND v.model_version_id = $6
                WHERE vector_dims(v.vec) = vector_dims($1)
                    AND w.id IS DISTINCT FROM $3
                    AND ($4::text[] IS NULL
                         OR w.collection = ANY($4::text[]))
                    AND (NOT $5 OR {HAS_COPIES})
                ORDER BY v.vec <=> $1
                LIMIT $2
            """, emb, top_k, reference_id, collection, copies, version)

            by_work = await tags_by_work(conn, (r["id"] for r in rows))
            results = []
            for row in rows:
                work = row_to_work(row, by_work[row["id"]])
                similarity = 1.0 - float(row["distance"])
                results.append(SearchResult(work=work, score=similarity))

            return results

    @staticmethod
    async def _reference(conn, path: str, version: int):
        """A work's id and vector under this model version, by any path it
        has answered to."""
        row = await conn.fetchrow(
            "SELECT v.work_id, v.vec FROM derived.work_vectors v "
            "WHERE v.model_version_id = $2 AND v.work_id = ("
            "  SELECT work_id FROM work_paths WHERE path = $1 "
            "  ORDER BY present DESC, last_seen DESC LIMIT 1)",
            path, version)
        if row is None:
            return None
        return row["work_id"], np.array(row["vec"].to_list(), dtype=np.float32)

    async def search_hybrid(
        self,
        tags: list[str],
        similar_to: str | None = None,
        similar_embedding: list[float] | None = None,
        embedding_type: str = "style",
        top_k: int = 10,
        collection: str | None = LEAD,
        copies: bool = False,
    ) -> list[SearchResult]:
        """Filter by tags, then rank by similarity.

        Args:
            tags: Tag filters (same format as search_tags).
            similar_to: Folder path of the reference work.
            similar_embedding: Direct reference embedding.
            embedding_type: Which embedding to use.
            top_k: Number of results to return.
            collection: Which collection to search; `None` for all.
            copies: Only works a second path repeats.

        Returns:
            List of SearchResult matching tags, ranked by similarity.
        """
        collection = resolve_collection(collection)
        filters, content_tags = self._parse_query_terms(tags)

        async with self.store.acquire() as conn:

            emb = None
            version = await profiles.embedding_version(conn)
            if version is not None and similar_embedding:
                emb = np.array(similar_embedding, dtype=np.float32)
            elif version is not None and similar_to:
                found = await self._reference(conn, similar_to, version)
                if found is not None:
                    emb = found[1]

            conditions = ["TRUE"]
            params: list = []

            def _bind(value) -> str:
                params.append(value)
                return f"${len(params)}"

            if collection:
                conditions.append(
                f"w.collection = ANY({_bind(collection)}::text[])")
            if copies:
                conditions.append(HAS_COPIES)

            for field_name, value in filters:
                conditions.append(
                    self._filter_condition(field_name, value, _bind))

            for tag in content_tags:
                conditions.append(f"""
                    EXISTS (
                        SELECT 1 FROM effective_tags et
                        WHERE et.work_id = w.id
                          AND et.tag = ANY({_bind(self._expand(tag))}::text[])
                    )
                """)

            where = " AND ".join(conditions)

            if emb is not None:
                emb_ph = _bind(emb)
                rows = await conn.fetch(f"""
                    SELECT w.*, (v.vec <=> {emb_ph}) AS distance
                    FROM works_effective w
                    JOIN derived.work_vectors v ON v.work_id = w.id
                                       AND v.model_version_id = {_bind(version)}
                    WHERE {where} AND vector_dims(v.vec) = vector_dims({emb_ph})
                    ORDER BY v.vec <=> {emb_ph}
                    LIMIT {_bind(top_k)}
                """, *params)
            else:
                rows = await conn.fetch(f"""
                    SELECT w.*, 0.0 as distance
                    FROM works_effective w
                    WHERE {where}
                    ORDER BY w.analyzed_at DESC{self.PAGING_TIEBREAK}
                    LIMIT {_bind(top_k)}
                """, *params)

            by_work = await tags_by_work(conn, (r["id"] for r in rows))
            results = []
            for row in rows:
                work = row_to_work(row, by_work[row["id"]])
                similarity = 1.0 - float(row["distance"]) if emb is not None else 0.0
                matched = [t for t in content_tags if t in work.general_tags]
                results.append(SearchResult(
                    work=work,
                    score=similarity,
                    matched_tags=matched,
                ))

            return results

    async def search_text(
        self,
        query: str,
        top_k: int = 50,
        collection: str | None = LEAD,
    ) -> list[SearchResult]:
        """Parse a natural language query and execute it.

        Supports queries like:
        - "中文 全彩 女僕 校園"
        - "maid school_uniform full_color"
        - "full_color Chinese maid"

        Args:
            query: Space-separated terms.
            collection: Which collection to search; `None` for all.

        Returns:
            List of matching SearchResult.
        """
        collection = resolve_collection(collection)
        terms = query.strip().split()
        return await self.search_tags(
            terms, mode="AND", limit=top_k, collection=collection)

    def _parse_query_terms(
        self, terms: list[str]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Parse query terms into field filters and content tags.

        Returns:
            (filters, content_tags) where filters are (field, value) pairs
            and content_tags are tag names for the work_tags table.
        """
        filters = []
        content_tags = []

        for term in terms:
            term_lower = term.lower().strip()
            if not term_lower:
                continue

            field = (self.vocabulary.resolve_filter(term)
                     if self.vocabulary is not None else None)
            if field is not None:
                filters.append(field)
                continue

            resolved = (
                self.vocabulary.resolve_tag(term)
                if self.vocabulary is not None else None
            )
            content_tags.append(resolved or term_lower)

        return filters, content_tags
