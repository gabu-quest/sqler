from typing import Any, List, Optional, Self

import aiosqlite


class AsyncSQLiteAdapter:
    """Asynchronous SQLite connector using aiosqlite with built-in PRAGMAs."""

    def __init__(self, path: str = "sqler.db", pragmas: Optional[list[str]] = None):
        self.path = path
        self.connection: Optional[aiosqlite.Connection] = None
        self.pragmas = pragmas or []

    async def connect(self) -> None:
        self.connection = await aiosqlite.connect(self.path, uri=True)
        # ensure row returns tuples, not dicts
        await self.connection.execute("PRAGMA foreign_keys = ON")
        for pragma in self.pragmas:
            await self.connection.execute(pragma)
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    async def execute(self, query: str, params: Optional[List[Any]] = None) -> aiosqlite.Cursor:
        if not self.connection:
            await self.connect()
        cursor = await self.connection.execute(query, params or [])
        return cursor

    async def executemany(self, query: str, param_list: List[List[Any]]) -> aiosqlite.Cursor:
        if not self.connection:
            await self.connect()
        cursor = await self.connection.executemany(query, param_list)
        await self.commit()
        return cursor

    async def executescript(self, script: str) -> aiosqlite.Cursor:
        if not self.connection:
            await self.connect()
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
    def in_memory(cls, shared: bool = True) -> Self:
        pragmas = [
            "PRAGMA foreign_keys = ON",
            "PRAGMA synchronous = OFF",
            "PRAGMA journal_mode = MEMORY",
            "PRAGMA temp_store = MEMORY",
            "PRAGMA cache_size = -32000",
            "PRAGMA locking_mode = EXCLUSIVE",
        ]
        uri = "file::memory:?cache=shared" if shared else ":memory:"
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
