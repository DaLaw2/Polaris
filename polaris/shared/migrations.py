"""Numbered schema changes, applied once and recorded in `schema_migrations`.

`0001` is the baseline: the schema as the boot DDL creates it. A later
change is a new `Migration` whose `settled` asks whether its end state is
already here (then it is recorded without running) and whose `counts`
asks how many rows it would touch (zero: applied on boot; otherwise the
boot refuses until `polaris-migrate --apply`). A database that predates
the baseline is not supported.

    python -m polaris.cli.migrate            # what is pending, and what it would touch
    python -m polaris.cli.migrate --apply    # do it
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    """One schema change, and the two questions that decide what to do with it."""

    version: str
    note: str
    settled: str
    counts: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha1(self.sql.encode("utf-8")).hexdigest()[:12]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version="0001",
        note="Baseline: the schema as the boot DDL creates it.",
        settled="SELECT TRUE",
        counts="SELECT 0",
        sql="",
    ),
)


class Behind(RuntimeError):
    """The database has repairs pending that would touch rows."""


class Unsupported(RuntimeError):
    """The database predates the baseline and has no upgrade path."""


async def require_baseline(conn) -> None:
    """Raise `Unsupported` for a populated database this build cannot adopt."""
    if not await conn.fetchval(
            "SELECT COALESCE(to_regclass('public.scan_runs'), "
            "to_regclass('public.tag_scores')) IS NOT NULL"):
        return
    if not await conn.fetchval(
            "SELECT to_regclass('public.schema_migrations') IS NOT NULL"):
        unknown = ["no schema_migrations"]
    else:
        known = [m.version for m in MIGRATIONS]
        unknown = [r["version"] for r in await conn.fetch(
            "SELECT version FROM schema_migrations "
            "WHERE version <> ALL($1::text[]) ORDER BY version", known)]
    if unknown:
        raise Unsupported(
            "this database predates schema baseline 0001 and cannot be "
            f"upgraded (found: {', '.join(unknown)}). Start from an empty "
            "database.")


async def applied(conn) -> dict[str, str]:
    """Every recorded version, and the checksum it was recorded with."""
    return {r["version"]: r["checksum"] for r in await conn.fetch(
        "SELECT version, checksum FROM schema_migrations")}


async def _ask(conn, sql: str):
    """One scalar, or None if the query cannot run against this database."""
    import asyncpg

    try:
        return await conn.fetchval(sql)
    except asyncpg.PostgresError:
        return None


@dataclass
class Plan:
    """What `check` worked out, so a caller can print it or act on it."""

    adopt: list[Migration]
    free: list[tuple[Migration, int]]
    blocked: list[tuple[Migration, int]]
    drifted: list[Migration]


async def plan(conn) -> Plan:
    """Sort the unrecorded migrations into adopt / free / blocked; writes
    nothing. `drifted` is one whose text changed after it was recorded."""
    await require_baseline(conn)
    done = await applied(conn)
    out = Plan([], [], [], [])
    for m in MIGRATIONS:
        if m.version in done:
            if done[m.version] != m.checksum:
                out.drifted.append(m)
            continue
        if await _ask(conn, m.settled):
            out.adopt.append(m)
            continue
        n = await _ask(conn, m.counts)
        (out.free if not n else out.blocked).append((m, n or 0))
    return out


async def _record(conn, m: Migration) -> None:
    await conn.execute(
        "INSERT INTO schema_migrations (version, checksum) "
        "VALUES ($1, $2) ON CONFLICT (version) DO NOTHING",
        m.version, m.checksum)


async def apply(conn, m: Migration) -> None:
    """Run one migration and record it, both or neither."""
    async with conn.transaction():
        await conn.execute(m.sql)
        await _record(conn, m)


async def check(conn) -> Plan:
    """Record what is settled, apply what touches no rows, and raise
    `Behind` if a migration would touch rows. Called on every boot."""
    await conn.execute(MIGRATIONS_SQL)
    p = await plan(conn)

    for m in p.adopt:
        await _record(conn, m)
    for m, _ in p.free:
        await apply(conn, m)

    if p.blocked:
        raise Behind(
            "the database is behind and the repairs would touch rows:\n" +
            "\n".join(f"  {m.version}  {n} rows  {m.note.split('.')[0]}."
                      for m, n in p.blocked) +
            "\n\nNothing has been changed. To see what each would do:\n"
            "  python -m polaris.cli.migrate\n"
            "and to let them run:\n"
            "  python -m polaris.cli.migrate --apply")
    return p
