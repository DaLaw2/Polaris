#!/usr/bin/env python
"""Export and re-import everything a rescan cannot produce.

A scan rebuilds the raw layer and everything derived from it. It does not
rebuild what a person decided: the terms they mapped, the corrections they
made, the titles they gave, the thresholds they tuned.

Rows are exported by name, not by id. `concepts` are keyed by (kind, slug),
works by `content_key` with the path as a fallback, model profile params by
profile name, so a reimport lands on whatever ids the new database hands out.

Claims and titles need their works to exist, which after a rebuild means
after the scan. Import twice: once before, to lay down the vocabulary the
scan reads, and once after, to place the corrections. Both runs are
idempotent.

    python -m polaris.cli.curated export -o curated.json
    python -m polaris.cli.curated import -i curated.json
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import io
import json

import asyncpg

from polaris import config
from polaris.derivation import derive

PLAIN = {
    "collections": ("name", ["name", "root", "searchable", "ordinal"]),
    "tag_translations": ("tag", ["tag", "display_zh"]),
    "hidden_tags": ("tag", ["tag"]),
    "derivation_params": ("name", ["name", "value"]),
    "derivation_tag_sets": ("set_name, tag", ["set_name", "tag"]),
    "rating_order": ("name", ["name", "rank"]),
    "claim_fields": ("field", ["field", "multi", "model_mappable"]),
}

WORK_KEY = """
    SELECT w.id FROM works w
    WHERE ($1::text IS NOT NULL AND w.content_key = $1)
    UNION
    SELECT p.work_id FROM work_paths p WHERE p.path = $2
    LIMIT 1
"""


def _when(value):
    """A timestamp comes back from JSON as text; asyncpg wants a datetime."""
    return dt.datetime.fromisoformat(value) if isinstance(value, str) else value


async def export(conn) -> dict:
    out: dict = {"tables": {}}

    for table, (_, cols) in PLAIN.items():
        rows = await conn.fetch(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1")
        out["tables"][table] = [dict(r) for r in rows]

    out["profile_params"] = [dict(r) for r in await conn.fetch("""
        SELECT p.name AS profile, pp.name, pp.value
        FROM model_profile_params pp JOIN model_profiles p ON p.id = pp.profile_id
        ORDER BY p.name, pp.name""")]

    out["concepts"] = [dict(r) for r in await conn.fetch("""
        SELECT c.kind, c.slug, c.display_zh, c.note,
               p.kind AS parent_kind, p.slug AS parent_slug
        FROM concepts c LEFT JOIN concepts p ON p.id = c.parent_id
        ORDER BY c.kind, c.slug""")]

    out["term_map"] = [dict(r) for r in await conn.fetch("""
        SELECT t.term, t.search_only, c.kind, c.slug
        FROM term_map t JOIN concepts c ON c.id = t.concept_id
        ORDER BY t.id""")]

    out["claims"] = [dict(r) for r in await conn.fetch("""
        SELECT w.content_key, i.folder_path, cl.field, cl.negated,
               cl.created_at, co.kind AS concept_kind, co.slug AS concept_slug
        FROM work_claims cl
        JOIN works w ON w.id = cl.work_id
        JOIN concepts co ON co.id = cl.concept_id
        LEFT JOIN work_identity i ON i.id = cl.work_id
        ORDER BY cl.id""")]

    out["titles"] = [dict(r) for r in await conn.fetch("""
        SELECT w.content_key, i.folder_path, w.title
        FROM works w LEFT JOIN work_identity i ON i.id = w.id
        WHERE w.title IS NOT NULL ORDER BY w.id""")]
    return out


async def load(conn, data: dict) -> dict[str, int]:
    n: dict[str, int] = {}

    for table, (key, cols) in PLAIN.items():
        rows = data["tables"].get(table) or []
        if not rows:
            continue
        keys = key.split(", ")
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in keys)
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
               f"VALUES ({placeholders}) ON CONFLICT ({key}) "
               + (f"DO UPDATE SET {updates}" if updates else "DO NOTHING"))
        for r in rows:
            await conn.execute(sql, *[r[c] for c in cols])
        n[table] = len(rows)

    for r in data.get("profile_params", []):
        await conn.execute("""
            INSERT INTO model_profile_params (profile_id, name, value)
            SELECT id, $2, $3 FROM model_profiles WHERE name = $1
            ON CONFLICT (profile_id, name) DO UPDATE SET value = EXCLUDED.value""",
            r["profile"], r["name"], r["value"])
    n["profile_params"] = len(data.get("profile_params", []))

    for r in data.get("concepts", []):
        await conn.execute("""
            INSERT INTO concepts (kind, slug, display_zh, note)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (kind, slug) DO UPDATE SET
                display_zh = EXCLUDED.display_zh, note = EXCLUDED.note""",
            r["kind"], r["slug"], r["display_zh"], r["note"])
    for r in data.get("concepts", []):
        if r["parent_kind"]:
            await conn.execute("""
                UPDATE concepts SET parent_id =
                    (SELECT id FROM concepts WHERE kind = $3 AND slug = $4)
                WHERE kind = $1 AND slug = $2""",
                r["kind"], r["slug"], r["parent_kind"], r["parent_slug"])
    n["concepts"] = len(data.get("concepts", []))

    for r in data.get("term_map", []):
        await conn.execute("""
            INSERT INTO term_map (term, search_only, concept_id)
            SELECT $1, $2, c.id FROM concepts c WHERE c.kind = $3 AND c.slug = $4
            ON CONFLICT DO NOTHING""",
            r["term"], r["search_only"], r["kind"], r["slug"])
    n["term_map"] = len(data.get("term_map", []))

    placed = missing = 0
    for r in data.get("claims", []):
        work_id = await conn.fetchval(WORK_KEY, r["content_key"],
                                      r["folder_path"])
        if work_id is None:
            missing += 1
            continue
        await conn.execute("""
            INSERT INTO work_claims (work_id, field, concept_id, negated,
                                     created_at)
            SELECT $1, $2, c.id, $3, $4 FROM concepts c
            WHERE c.kind = $5 AND c.slug = $6
            ON CONFLICT (work_id, field, concept_id) DO UPDATE SET
                negated = EXCLUDED.negated, created_at = EXCLUDED.created_at""",
            work_id, r["field"], r["negated"], _when(r["created_at"]),
            r["concept_kind"], r["concept_slug"])
        placed += 1
    for r in data.get("titles", []):
        work_id = await conn.fetchval(WORK_KEY, r["content_key"],
                                      r["folder_path"])
        if work_id is None:
            missing += 1
            continue
        await conn.execute("UPDATE works SET title = $2 WHERE id = $1",
                           work_id, r["title"])
        placed += 1
    n["placed"] = placed
    n["unplaced"] = missing
    return n


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("-o", "--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("-i", "--in", dest="path", required=True)

    args = ap.parse_args()
    conn = await asyncpg.connect(config.require_dsn())
    try:
        if args.cmd == "export":
            data = await export(conn)
            io.open(args.out, "w", encoding="utf-8").write(
                json.dumps(data, ensure_ascii=False, indent=1, default=str))
            for k, v in data["tables"].items():
                print(f"  {k:24} {len(v)}", flush=True)
            for k in ("profile_params", "concepts", "term_map", "claims",
                      "titles"):
                print(f"  {k:24} {len(data[k])}", flush=True)
            print(f"wrote {args.out}", flush=True)
        else:
            data = json.loads(io.open(args.path, encoding="utf-8").read())
            async with conn.transaction():
                n = await load(conn, data)
            for k, v in n.items():
                print(f"  {k:24} {v}", flush=True)

            async def progress(done: int, total: int) -> None:
                print(f"  derived {done}/{total}", flush=True)

            await derive.refresh_derived(conn, on_batch=progress)
            if n.get("unplaced"):
                print("re-run after the scan to place the rest", flush=True)
    finally:
        await conn.close()
    return 0


def run() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    raise SystemExit(run())
