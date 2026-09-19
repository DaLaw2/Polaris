"""The connection pool, and the schema every connection finds in place.

Requires PostgreSQL with the pgvector extension.
"""

from polaris import config

from .migrations import check
from .schema import init_schema


class Store:
    """Async PostgreSQL storage layer for work analyses."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn
        self._pool = None

    @property
    def dsn(self) -> str:
        """The DSN, or a clear instruction if none is configured.

        No compiled-in default: one carrying a password and a database name
        gives anyone else a confusing auth error, or a connection somewhere
        they did not intend.
        """
        return self._dsn or config.require_dsn()

    async def connect(self) -> None:
        """Establish connection pool and initialize schema."""
        import asyncpg
        from pgvector.asyncpg import register_vector

        bootstrap = await asyncpg.connect(self.dsn)
        try:
            await bootstrap.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await bootstrap.close()

        self._pool = await asyncpg.create_pool(
            self.dsn, min_size=2, max_size=10, init=register_vector,
            server_settings={"jit": "off"})

        async with self.acquire() as conn:
            await init_schema(conn)

            await check(conn)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    def acquire(self):
        """A connection with this project's codecs registered."""
        if self._pool is None:
            raise RuntimeError("Store.connect() has not been awaited")
        return self._pool.acquire()
