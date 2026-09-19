"""A repair runs once, is recorded, and never runs over rows unasked.

    POLARIS_TEST_DSN=postgresql://.../polaris_test python tests/test_migrations.py
"""
import asyncio

from _common import dsn

DSN = dsn()

import asyncpg  # noqa: E402

from polaris.shared import migrations, schema, tuning  # noqa: E402

PROBE = migrations.Migration(
    version="9999", note="Probe. Deletes the probe concept.",
    settled="SELECT NOT EXISTS (SELECT 1 FROM concepts WHERE slug = 'probe')",
    counts="SELECT count(*) FROM concepts WHERE slug = 'probe'",
    sql="DELETE FROM concepts WHERE slug = 'probe'")
PROBES = "SELECT count(*) FROM concepts WHERE slug = 'probe'"


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        plan = await migrations.plan(conn)
        assert not (plan.adopt or plan.free or plan.blocked or plan.drifted), plan
        assert set(await migrations.applied(conn)) == {m.version for m in migrations.MIGRATIONS}
        before = await conn.fetchval("SELECT max(applied_at) FROM schema_migrations")
        await migrations.check(conn)
        assert await conn.fetchval("SELECT max(applied_at) FROM schema_migrations") == before
        print("  ok  a booted database records every migration; a second check writes nothing")

        original = migrations.MIGRATIONS
        migrations.MIGRATIONS = (*original, PROBE)
        try:
            await conn.execute("INSERT INTO concepts (kind, slug) VALUES ('character', 'probe')")
            assert {m.version: n for m, n in (await migrations.plan(conn)).blocked} == {"9999": 1}
            try:
                await migrations.check(conn)
                raise AssertionError("a repair with rows at stake was allowed through")
            except migrations.Behind as e:
                assert "9999" in str(e) and "1 rows" in str(e)
            assert await conn.fetchval(PROBES) == 1
            await migrations.apply(conn, PROBE)
            assert await conn.fetchval(PROBES) == 0
            assert not (await migrations.plan(conn)).blocked
            print("  ok  rows at stake refuse the boot; applying runs and records it")
        finally:
            migrations.MIGRATIONS = original
            await conn.execute("DELETE FROM concepts WHERE slug = 'probe'")
            await conn.execute("DELETE FROM schema_migrations WHERE version = '9999'")

        first = migrations.MIGRATIONS[0].version
        checksum = await conn.fetchval(
            "SELECT checksum FROM schema_migrations WHERE version = $1", first)
        await conn.execute("UPDATE schema_migrations SET checksum = 'nonsense' "
                           "WHERE version = $1", first)
        try:
            plan = await migrations.plan(conn)
            assert [m.version for m in plan.drifted] == [first] and not plan.blocked
        finally:
            await conn.execute("UPDATE schema_migrations SET checksum = $2 "
                               "WHERE version = $1", first, checksum)
        print("  ok  a migration edited after the fact is reported, not replayed")

        await conn.execute("INSERT INTO schema_migrations (version, checksum) "
                           "VALUES ('0020', 'old')")
        try:
            for call in (migrations.plan, schema.init_layers):
                try:
                    await call(conn)
                    raise AssertionError(f"{call.__name__} took a pre-baseline database")
                except migrations.Unsupported as e:
                    assert "0020" in str(e), e
        finally:
            await conn.execute("DELETE FROM schema_migrations WHERE version = '0020'")
        print("  ok  a version this build does not know is refused, not adopted")

        edits = {"score:default": 0.99, "sample:floor": 3}
        for name, value in edits.items():
            await tuning.set_param(conn, name, value)
        try:
            await schema.init_layers(conn)
            await migrations.check(conn)
            got = {"score:default": await conn.fetchval(
                       "SELECT value FROM model_profile_params WHERE profile_id = "
                       "active_profile_id() AND name = 'score:default'"),
                   "sample:floor": await conn.fetchval(
                       "SELECT value FROM derivation_params WHERE name = 'sample:floor'")}
            assert {k: float(v) for k, v in got.items()} == edits, got
        finally:
            for name in edits:
                await tuning.set_param(conn, name, tuning.param_default(name))
        print("  ok  a boot leaves an edited number alone")
    finally:
        await conn.close()
    print("all migration checks passed")


asyncio.run(main())
