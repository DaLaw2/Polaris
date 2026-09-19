"""HTTP endpoints for searching and reading works."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from polaris import state
from polaris.catalog.api import _size
from polaris.search import queries
from polaris.search.engine import LEAD
from polaris.vocabulary.api import (
    _author_display_cache,
    _series_display_cache,
    _sync_vocabulary,
)

router = APIRouter()


class WorkItem(BaseModel):
    """One work as the frontend consumes it.

    No `id`: the UI addresses a work by `folder_path` everywhere, and
    filling one cost `/api/work` a second query per drawer open.
    `/api/tags/visible` returns artist tags for the one caller that wants
    them.

    `rating`, `color_mode`, `work_type` and `collection` carry no defaults.
    `works_effective` is where a work with no evidence is decided about,
    and a `= "comic"` in a response model does not fall back to that
    decision, it overwrites it. None reaches the client as null, and the
    client shows nothing rather than a guess.
    """

    folder_path: str
    folder_name: str
    collection: str | None = None
    artist: str | None = None
    title: str | None = None
    series: str | None = None
    series_display: str | None = None
    artist_display: str | None = None
    language: str | None = None
    rating: str | None = None
    color_mode: str | None = None
    work_type: str | None = None
    total_images: int = 0
    has_video: bool = False
    duration_s: float | None = None
    general_tags: dict[str, float] = {}
    character_tags: dict[str, float] = {}
    copyright_tags: dict[str, float] = {}

    class Config:
        from_attributes = True


class SearchResultItem(BaseModel):
    work: WorkItem
    score: float = 0.0
    matched_tags: list[str] = []
    places: list[dict] | None = None


class StatsResponse(BaseModel):
    total_works: int
    by_rating: dict[str, int]
    by_color: dict[str, int]
    by_type: dict[str, int]
    top_tags: dict[str, int]
    top_series: dict[str, int]


async def _resolve_series_display(series: str | None) -> str | None:
    """Resolve series name to Chinese display name."""
    if not series or not state.entity_mgr:
        return series
    if series in _series_display_cache:
        return _series_display_cache[series]
    entity = await state.entity_mgr.resolve_name(series, "series")
    display = entity.get("display_zh") or series if entity else series
    _series_display_cache[series] = display
    return display


async def _resolve_author_display(artist: str | None) -> str | None:
    """Resolve author name to its Chinese display name."""
    if not artist or not state.entity_mgr:
        return artist
    if artist in _author_display_cache:
        return _author_display_cache[artist]
    entity = await state.entity_mgr.resolve_name(artist, "artist")
    display = entity.get("display_zh") or artist if entity else artist
    _author_display_cache[artist] = display
    return display


LIST_TAGS = 12


def _work_to_item(w, tag_limit: int | None = LIST_TAGS) -> WorkItem:
    """Convert a WorkAnalysis to a WorkItem. None sends every tag."""
    visible_tags = w.general_tags
    if tag_limit is not None and len(visible_tags) > tag_limit:
        visible_tags = dict(list(visible_tags.items())[:tag_limit])
    return WorkItem(
        folder_path=w.folder_path,
        folder_name=w.folder_name,
        collection=getattr(w, "collection", None),
        artist=w.metadata.artist,
        title=w.metadata.title,
        series=w.metadata.series,
        language=w.metadata.language,
        rating=w.rating,
        color_mode=w.color_mode,
        work_type=w.work_type,
        total_images=getattr(w, 'total_images', 0),
        has_video=getattr(w, 'has_video', False),
        duration_s=getattr(w, 'duration_s', None),
        general_tags=visible_tags,
        character_tags=w.character_tags,
        copyright_tags=getattr(w, 'copyright_tags', {}),
    )


async def _work_to_item_resolved(w, tag_limit: int | None = LIST_TAGS) -> WorkItem:
    """Convert with resolved display names (async)."""
    item = _work_to_item(w, tag_limit)
    item.series_display = await _resolve_series_display(w.metadata.series)
    item.artist_display = await _resolve_author_display(w.metadata.artist)
    return item


def _collection(name: str) -> str | None:
    """Turn the query parameter into what the engine expects.

    `all` is spelled out rather than left to an omitted parameter, so that
    searching every collection is something a caller has to mean. An
    omitted one is `LEAD`, which the engine resolves against the database.
    Several, comma separated, is ordinary: a collection narrows a search
    the way an artist does, and nobody would accept picking one artist.
    """
    name = name.strip()
    return None if name.lower() == "all" else (name or LEAD)


@router.get("/api/search", response_model=list[SearchResultItem])
async def search(
    q: str = Query(..., description="Search terms, e.g. '中文 全彩 女僕'"),
    mode: str = Query("AND", description="AND or OR"),
    limit: int = Query(50, ge=1, le=200),
    collection: str = Query(
        "",
        description="Collections to search, comma separated; omit for the "
                    "searchable ones, 'all' for every collection",
    ),
):
    """Tag-based search. Supports Chinese aliases and entity resolution."""
    await _sync_vocabulary()
    terms = q.strip().split()

    resolved_terms = []
    for term in terms:
        entity = await state.entity_mgr.resolve_name(term)
        if entity:
            resolved_terms.append(entity["canonical"])
        else:
            resolved_terms.append(term)

    results = await state.engine.search_tags(
        resolved_terms, mode=mode, limit=limit,
        collection=_collection(collection),
    )
    items = []
    for r in results:
        item = await _work_to_item_resolved(r.work)
        items.append(SearchResultItem(work=item, score=r.score, matched_tags=r.matched_tags))
    return items


@router.get("/api/search/faceted")
async def search_faceted(
    q: str = Query("", description="Free tag terms, space separated"),
    exclude: str = Query("", description="Tags that disqualify a work"),
    artist: str = Query(""),
    series: str = Query(""),
    title: str = Query(""),
    character: str = Query("", description="Comma separated character tags"),
    copyright_: str = Query("", alias="copyright"),
    rating: str = Query("", description="Comma separated"),
    color_mode: str = Query(""),
    work_type: str = Query(""),
    language: str = Query(""),
    mode: str = Query("AND"),
    sort: str = Query("newest"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    facets: bool = Query(True),
    collection: str = Query(
        "",
        description="Collection to search; omit for the lead one, "
                    "'all' for every collection",
    ),
    copies: bool = Query(False, description="Only works a second path repeats"),
):
    """Multi-dimensional search with facet counts.

    Every dimension is optional and they compose: artist + series + tag +
    colour + rating in one query. Returns the page of results, the total
    match count for paging, and per-dimension value counts so the UI can
    show what narrowing further would yield.
    """
    await _sync_vocabulary()

    def _split(raw: str) -> list[str]:
        """Split a term list, keeping multi-word terms intact.

        Commas are the explicit separator — the UI joins its tag chips
        with them — so when they are present a term may contain spaces
        ("school uniform", "long hair"). Only a comma-free string is
        split on whitespace, which keeps a typed "maid nurse" working.
        """
        parts = raw.split(",") if "," in raw else raw.split()
        return [p.strip() for p in parts if p.strip()]

    terms = _split(q)
    resolved = []
    for term in terms:
        entity = await state.entity_mgr.resolve_name(term)
        resolved.append(entity["canonical"] if entity else term)

    payload = await state.engine.search_faceted(
        tags=resolved,
        exclude_tags=_split(exclude),
        category_tags={
            "character": _split(character),
            "copyright": _split(copyright_),
        },
        text={"artist": artist, "series": series, "title": title},
        enums={
            "rating": _split(rating),
            "color_mode": _split(color_mode),
            "work_type": _split(work_type),
            "language": _split(language),
        },
        mode=mode,
        sort=sort,
        limit=limit,
        offset=offset,
        with_facets=facets,
        collection=_collection(collection),
        copies=copies,
    )

    items = []
    for r in payload["results"]:
        item = await _work_to_item_resolved(r.work)
        items.append(SearchResultItem(
            work=item, score=r.score, matched_tags=r.matched_tags,
        ))

    if copies:
        await _attach_places(items)
    return {
        "results": items,
        "total": payload["total"],
        "facets": payload["facets"],
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/similar", response_model=list[SearchResultItem])
async def similar(
    path: str = Query(..., description="Folder path of reference work"),
    embedding: str = Query("style", description="style or wd"),
    limit: int = Query(20, ge=1, le=100),
    collection: str = Query(""),
    copies: bool = Query(False),
):
    """Find works with similar visual style."""
    await _sync_vocabulary()
    results = await state.engine.search_similar(
        reference_path=path,
        embedding_type=embedding,
        top_k=limit,
        collection=_collection(collection),
        copies=copies,
    )
    items = []
    for r in results:
        item = await _work_to_item_resolved(r.work)
        items.append(SearchResultItem(work=item, score=r.score))
    if copies:
        await _attach_places(items)
    return items


@router.get("/api/hybrid", response_model=list[SearchResultItem])
async def hybrid(
    q: str = Query(..., description="Tag filter terms"),
    similar_to: str = Query(..., description="Folder path for similarity"),
    embedding: str = Query("style"),
    limit: int = Query(20, ge=1, le=100),
    collection: str = Query(""),
    copies: bool = Query(False),
):
    """Filter by tags, rank by similarity."""
    await _sync_vocabulary()
    terms = q.strip().split()
    results = await state.engine.search_hybrid(
        tags=terms,
        similar_to=similar_to,
        embedding_type=embedding,
        top_k=limit,
        collection=_collection(collection),
        copies=copies,
    )
    items = []
    for r in results:
        item = await _work_to_item_resolved(r.work)
        items.append(SearchResultItem(work=item, score=r.score, matched_tags=r.matched_tags))
    if copies:
        await _attach_places(items)
    return items


@router.get("/api/work", response_model=WorkItem | None)
async def get_work(path: str = Query(...)):
    """Get details for a single work.

    No id lookup: the overrides endpoints take a path and resolve the id
    themselves.
    """
    await _sync_vocabulary()
    async with state.store.acquire() as conn:
        w = await queries.get_work(conn, path)
    if not w:
        return None
    return await _work_to_item_resolved(w, tag_limit=None)


@router.get("/api/stats", response_model=StatsResponse)
async def stats():
    """Collection statistics."""
    async with state.store.acquire() as conn:
        return await queries.stats(conn)


async def _places(paths: list[str]) -> dict[str, list[dict]]:
    """Each work's own path and every copy of it, keyed by the work's path.

    Read against the disk now -- sizes, and whether each is still there --
    so a copy deleted since the last scan says so instead of waiting for
    one.
    """
    async with state.store.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.path, p.collection, "
            "       array_agg(c.path ORDER BY c.path) AS copies, "
            "       array_agg(c.collection ORDER BY c.path) AS colls "
            "FROM work_paths p JOIN work_copies c ON c.work_id = p.work_id "
            "WHERE p.present AND p.path = ANY($1::text[]) "
            "GROUP BY p.path, p.collection", paths)

    def place(path, collection, copy):
        size = _size(Path(path))
        return {"path": path, "collection": collection, "copy": copy,
                "exists": size is not None, "bytes": size}

    def measure():
        return {r["path"]: [place(r["path"], r["collection"], False)]
                + [place(c, k, True) for c, k in zip(r["copies"], r["colls"])]
                for r in rows}

    return await asyncio.to_thread(measure)


async def _attach_places(items: list["SearchResultItem"]) -> None:
    """Fill `places` on a page of results, in one query."""
    found = await _places([i.work.folder_path for i in items])
    for i in items:
        i.places = found.get(i.work.folder_path, [])
