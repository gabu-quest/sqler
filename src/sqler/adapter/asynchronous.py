import time
import uuid
from typing import Any, List, Optional, Self

import aiosqlite

from sqler.logging import query_logger

from .abstract import AsyncAdapterABC


class AsyncSQLiteAdapter(AsyncAdapterABC):
    """Asynchronous SQLite connector using aiosqlite with built-in PRAGMAs."""

    def __init__(self, path: str = "sqler.db", pragmas: Optional[list[str]] = None):
        self.path = path
        self.connection: Optional[aiosqlite.Connection] = None
        self.pragmas = pragmas or []

    async def connect(self) -> None:
        self.connection = await aiosqlite.connect(self.path, uri=True)
        # Apply configured pragmas (foreign_keys is included in factory defaults)
        for pragma in self.pragmas:
            await self.connection.execute(pragma)
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    async def execute(self, query: str, params: Optional[List[Any]] = None) -> aiosqlite.Cursor:
        """Execute a SQL query with optional parameters and return cursor.

        Automatically logs the query if query_logger is enabled.
        """
        if not self.connection:
            await self.connect()
        assert self.connection is not None  # Guaranteed by connect()
        start = time.perf_counter()
        error_msg = None
        cursor = None
        try:
            cursor = await self.connection.execute(query, params or [])
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            query_logger.log(
                sql=query,
                params=list(params) if params else [],
                duration_ms=duration_ms,
                rows_affected=cursor.rowcount if cursor and cursor.rowcount >= 0 else None,
                error=error_msg,
            )
        return cursor

    async def executemany(self, query: str, param_list: List[List[Any]]) -> aiosqlite.Cursor:
        if not self.connection:
            await self.connect()
        assert self.connection is not None  # Guaranteed by connect()
        cursor = await self.connection.executemany(query, param_list)
        await self.commit()
        return cursor

    async def executescript(self, script: str) -> aiosqlite.Cursor:
        if not self.connection:
            await self.connect()
        assert self.connection is not None  # Guaranteed by connect()
        cursor = await self.connection.executescript(script)
        await self.connection.commit()
        return cursor

    async def commit(self) -> None:
        if not self.connection:
            return
        await self.connection.commit()

    async def __aenter__(self) -> "AsyncSQLiteAdapter":
        if not self.connection:
            await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not self.connection:
            return
        if exc_type is None:
            await self.connection.commit()
        else:
            await self.connection.rollback()

    # factories
    @classmethod
    def in_memory(cls, shared: bool = True, name: Optional[str] = None) -> Self:
        pragmas = [
            "PRAGMA foreign_keys = ON",
            "PRAGMA synchronous = OFF",
            "PRAGMA journal_mode = MEMORY",
            "PRAGMA temp_store = MEMORY",
            "PRAGMA cache_size = -32000",
            "PRAGMA locking_mode = EXCLUSIVE",
        ]
        if shared:
            ident = name or f"sqler-{uuid.uuid4().hex}"
            uri = f"file:{ident}?mode=memory&cache=shared"
        else:
            uri = ":memory:"
        return cls(uri, pragmas=pragmas)

    @classmethod
    def on_disk(cls, path: str = "sqler.db") -> Self:
        pragmas = [
            "PRAGMA foreign_keys = ON",
            "PRAGMA busy_timeout = 5000",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA cache_size = -64000",
            "PRAGMA wal_autocheckpoint = 1000",
            "PRAGMA mmap_size = 268435456",
            "PRAGMA temp_store = MEMORY",
        ]
        return cls(path, pragmas=pragmas)
