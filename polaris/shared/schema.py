"""The whole schema, in an order every foreign key can be satisfied in."""

from __future__ import annotations

import asyncpg

from polaris.catalog import schema as catalog
from polaris.derivation import schema as derivation
from polaris.jobs import schema as jobs
from polaris.models import schema as models
from polaris.observation import schema as observation
from polaris.shared import tuning
from polaris.shared.migrations import MIGRATIONS, require_baseline
from polaris.vocabulary import schema as vocabulary

LAYERS_SQL = (catalog.SQL + models.SQL + observation.SQL + jobs.SQL
              + observation.CHECK_SQL + vocabulary.SQL + derivation.SQL
              + models.FUNCTIONS_SQL + vocabulary.FUNCTIONS_SQL
              + derivation.FUNCTIONS_SQL + derivation.TABLES_SQL
              + vocabulary.SEED_SQL + derivation.SEED_SQL + tuning.SEED_SQL)


async def behind(conn) -> bool:
    """Whether this database predates the shape the boot DDL describes."""
    if not await conn.fetchval(
            "SELECT to_regclass('public.scan_runs') IS NOT NULL"):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)",
        MIGRATIONS[-1].version)


async def init_layers(conn) -> None:
    """Create the layers, the derived tables, their views and the functions
    that fill them. Idempotent.

    A database older than the baseline is refused; one behind a later
    migration is left alone, so that `migrations.check` can refuse it and
    `polaris.cli.migrate` can bring it forward.
    """
    await require_baseline(conn)
    if await behind(conn):
        return
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '15s'")
        await conn.execute(LAYERS_SQL)
        try:
            async with conn.transaction():
                await conn.execute(derivation.VIEWS_SQL)
        except asyncpg.PostgresError:
            await conn.execute(derivation.DROP_VIEWS_SQL)
            await conn.execute(derivation.VIEWS_SQL)
        await conn.execute(derivation.DERIVE_SQL)


async def init_schema(conn) -> None:
    """The works table, then everything `init_layers` creates."""
    await conn.execute(catalog.WORKS_SQL)
    await init_layers(conn)
