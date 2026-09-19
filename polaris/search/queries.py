"""Reading works back, always through the effective views.

Everything here reads `works_effective` / `effective_tags` rather than
base tables, so a caller cannot get uncorrected values.
"""

from .domain import FolderMetadata, WorkAnalysis


async def tags_by_work(conn, work_ids) -> dict[int, list]:
    """Every listed work's tags, in one query rather than one query each.

    One query per row would be 48 round trips at the API's page size, each
    planning over a view that unions the tag matview with the claim layer.

    Returns `{}` for an empty page so the caller needs no special case.
    """
    ids = list(work_ids)
    if not ids:
        return {}
    rows = await conn.fetch(
        "SELECT work_id, category, tag, confidence "
        "FROM effective_tags "
        "WHERE work_id = ANY($1::int[]) AND visible "
        "ORDER BY work_id, category, confidence DESC, tag",
        ids)
    grouped: dict[int, list] = {i: [] for i in ids}
    for r in rows:
        grouped[r["work_id"]].append(r)
    return grouped


async def get_work(conn, folder_path: str) -> WorkAnalysis | None:
    """Retrieve a work by folder path."""

    row = await conn.fetchrow(
        "SELECT * FROM works_effective WHERE id = ("
        "  SELECT work_id FROM work_paths WHERE path = $1 "
        "  ORDER BY present DESC, last_seen DESC LIMIT 1)",
        folder_path,
    )
    if not row:
        return None

    tags = await conn.fetch(
        "SELECT category, tag, confidence FROM effective_tags "
        "WHERE work_id = $1 AND visible "
        "ORDER BY category, confidence DESC, tag",
        row["id"],
    )

    return row_to_work(row, tags)


async def stats(conn) -> dict:
    """Get collection statistics."""
    total = await conn.fetchval("SELECT COUNT(*) FROM works")
    by_rating = await conn.fetch(
        "SELECT rating, COUNT(*) as cnt FROM works_effective "
        "GROUP BY rating ORDER BY cnt DESC"
    )
    by_color = await conn.fetch(
        "SELECT color_mode, COUNT(*) as cnt FROM works_effective "
        "GROUP BY color_mode ORDER BY cnt DESC"
    )
    by_type = await conn.fetch(
        "SELECT work_type, COUNT(*) as cnt FROM works_effective "
        "GROUP BY work_type ORDER BY cnt DESC"
    )
    top_tags = await conn.fetch(
        "SELECT tag, COUNT(*) as cnt FROM effective_tags "
        "WHERE category = 'general' AND visible "
        "GROUP BY tag ORDER BY cnt DESC LIMIT 30"
    )
    top_series = await conn.fetch(
        "SELECT series, COUNT(*) as cnt FROM works_effective "
        "WHERE series IS NOT NULL "
        "GROUP BY series ORDER BY cnt DESC LIMIT 20"
    )

    def tally(rows, field):
        return {(r[field] or "undetermined"): r["cnt"] for r in rows}

    return {
        "total_works": total,
        "by_rating": tally(by_rating, "rating"),
        "by_color": tally(by_color, "color_mode"),
        "by_type": tally(by_type, "work_type"),
        "top_tags": {r["tag"]: r["cnt"] for r in top_tags},
        "top_series": {r["series"]: r["cnt"] for r in top_series},
    }


def row_to_work(row, tags) -> WorkAnalysis:
    """Convert a `works_effective` row + its tags to a WorkAnalysis.

    The row must come from `works_effective`; a row from anywhere else
    is missing columns and fails here rather than quietly returning
    uncorrected data.

    No defaults for `rating`, `color_mode`, `work_type` or
    `collection`. The view is the only place that can see the evidence
    for them, and a Python `or "comic"` downstream of it does not fall
    back, it overrides — the moment the view stops asserting `comic`
    for a work with nothing to go on, the default puts it back. NULL
    means "nobody has said", and it travels.
    """
    def get(key, default=None):
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    metadata = FolderMetadata(
        raw_name=row["folder_name"],
        artist=row["artist"],
        title=row["title"],
        series=row["series"],
        language=row["language"],
        source_path=row["folder_path"],
    )

    general_tags = {}
    character_tags = {}
    copyright_tags = {}
    artist_tags = {}

    for t in tags:
        cat = t["category"]
        if cat == "general":
            general_tags[t["tag"]] = t["confidence"]
        elif cat == "character":
            character_tags[t["tag"]] = t["confidence"]
        elif cat == "copyright":
            copyright_tags[t["tag"]] = t["confidence"]
        elif cat == "artist":
            artist_tags[t["tag"]] = t["confidence"]

    return WorkAnalysis(
        folder_path=row["folder_path"],
        folder_name=row["folder_name"],
        metadata=metadata,
        collection=get("collection"),
        general_tags=general_tags,
        character_tags=character_tags,
        copyright_tags=copyright_tags,
        artist_tags=artist_tags,
        rating=row["rating"],
        color_mode=row["color_mode"],
        work_type=row["work_type"],
        total_images=get("total_images") or 0,
        has_video=bool(get("has_video")),
        duration_s=(float(get("duration_s"))
                    if get("duration_s") is not None else None),
        analyzed_at=str(row["analyzed_at"]) if row["analyzed_at"] else "",
    )
