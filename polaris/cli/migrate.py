"""What the database is behind on, and -- with `--apply` -- catching it up.

    python -m polaris.cli.migrate
    python -m polaris.cli.migrate --apply

Prints every repair that has not been recorded, what state the database is
in with respect to it, and how many rows it would touch. Without `--apply`
nothing is written at all, including the bookkeeping. With it, the schema
a boot lays goes down first: a repair can move rows into a table that
only this build's boot creates.
"""
import argparse
import asyncio

import asyncpg

from polaris import config
from polaris.shared import migrations
from polaris.shared.schema import init_schema


def _wrap(text: str, width: int = 72, indent: str = " " * 8) -> str:
    import textwrap

    return textwrap.fill(text, width, initial_indent=indent,
                         subsequent_indent=indent)


async def main(do_apply: bool) -> int:
    conn = await asyncpg.connect(config.require_dsn())
    try:
        try:
            await migrations.require_baseline(conn)
        except migrations.Unsupported as e:
            print(f"  {e}")
            return 1
        await conn.execute(migrations.MIGRATIONS_SQL)
        plan = await migrations.plan(conn)

        for m in plan.drifted:
            print(f"  {m.version}  WAS APPLIED, BUT ITS TEXT HAS CHANGED")
            print(_wrap("Recorded under a different checksum. Nothing is "
                        "done about this automatically: what a database "
                        "already ran cannot be un-run by editing the "
                        "file it came from."))

        for m in plan.adopt:
            print(f"  {m.version}  already in this state")
            print(_wrap(m.note))
        for m, n in plan.free:
            print(f"  {m.version}  nothing to touch")
            print(_wrap(m.note))
        for m, n in plan.blocked:
            print(f"  {m.version}  WOULD TOUCH {n} ROWS")
            print(_wrap(m.note))

        if not (plan.adopt or plan.free or plan.blocked):
            print("  nothing pending; the database is up to date")
            return 0

        if not do_apply:
            print("\n  Nothing was written. Re-run with --apply to:")
            if plan.adopt:
                print(f"    record {len(plan.adopt)} as already done")
            if plan.free:
                print(f"    run {len(plan.free)} that touch no rows")
            if plan.blocked:
                print(f"    run {len(plan.blocked)} that touch rows")
            return 1

        for m in plan.adopt:
            await migrations._record(conn, m)
        for m, _ in plan.free + plan.blocked:
            print(f"  applying {m.version} ...", flush=True)
            await migrations.apply(conn, m)
        await init_schema(conn)
        print(f"\n  recorded {len(plan.adopt)}, ran "
              f"{len(plan.free) + len(plan.blocked)}")
        return 0
    finally:
        await conn.close()


def run() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="run the pending repairs instead of listing them")
    return asyncio.run(main(ap.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(run())
