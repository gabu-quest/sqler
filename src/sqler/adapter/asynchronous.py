import asyncio
import time
import uuid
from contextvars import ContextVar
from typing import Any, List, Optional, Self

import aiosqlite

from sqler.logging import query_logger

from .abstract import AsyncAdapterABC


class AsyncSQLiteAdapter(AsyncAdapterABC):
    """Asynchronous SQLite connector using aiosqlite with connection pool.

    Uses an ``asyncio.Queue``-based pool of connections with ``ContextVar``
    per-task pinning so that sequential operations within one coroutine
    (execute → auto_commit) share the same connection.
    """

    def __init__(
        self,
        path: str = "sqler.db",
        pragmas: Optional[list[str]] = None,
        pool_size: int = 1,
    ):
        self.path = path
        self.pragmas = pragmas or []
        self._pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=pool_size)
        self._all_conns: list[aiosqlite.Connection] = []
        self._task_conn: ContextVar[Optional[aiosqlite.Connection]] = ContextVar(
            "task_conn", default=None
        )
        self._txn_depth: ContextVar[int] = ContextVar("txn_depth", default=0)
        self._connected = False
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> None:
        for _ in range(self._pool_size):
            conn = await aiosqlite.connect(self.path, uri=True)
            for pragma in self.pragmas:
                await conn.execute(pragma)
            await conn.commit()
            self._all_conns.append(conn)
            self._pool.put_nowait(conn)
        self._connected = True

    async def _acquire(self) -> aiosqlite.Connection:
        """Return the task-pinned connection, or get one from the pool and pin it."""
        conn = self._task_conn.get(None)
        if conn is not None:
            return conn
        if not self._connected:
            async with self._connect_lock:
                if not self._connected:
                    await self.connect()
        conn = await self._pool.get()
        self._task_conn.set(conn)
        return conn

    async def _release(self) -> None:
        """If not in a transaction, unpin the connection and return it to the pool."""
        if self._txn_depth.get(0) > 0:
            return
        conn = self._task_conn.get(None)
        if conn is not None:
            self._task_conn.set(None)
            self._pool.put_nowait(conn)

    async def close(self) -> None:
        for conn in self._all_conns:
            try:
                await conn.close()
            except Exception:
                pass
        self._all_conns.clear()
        # Drain the queue
        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._connected = False

    async def execute(self, query: str, params: Optional[List[Any]] = None) -> aiosqlite.Cursor:
        """Execute a SQL query with optional parameters and return cursor."""
        conn = await self._acquire()
        start = time.perf_counter()
        error_msg = None
        cursor = None
        try:
            cursor = await conn.execute(query, params or [])
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
        conn = await self._acquire()
        cursor = await conn.executemany(query, param_list)
        await self.commit()
        return cursor

    async def executescript(self, script: str) -> aiosqlite.Cursor:
        conn = await self._acquire()
        cursor = await conn.executescript(script)
        await conn.commit()
        return cursor

    async def commit(self) -> None:
        conn = self._task_conn.get(None)
        if conn is None:
            return
        await conn.commit()

    async def auto_commit(self) -> None:
        """Commit only if NOT inside an explicit transaction, then release connection."""
        if self._txn_depth.get(0) == 0:
            await self.commit()
            await self._release()

    @property
    def in_transaction(self) -> bool:
        """Return True if currently inside an explicit transaction."""
        return self._txn_depth.get(0) > 0

    async def begin_transaction(self) -> None:
        """Begin an explicit transaction (increments per-task depth counter).

        Uses BEGIN IMMEDIATE to acquire a write lock immediately,
        preventing SQLITE_BUSY errors during the transaction.
        Uses SAVEPOINTs for nested transactions to allow proper inner rollback.
        """
        conn = await self._acquire()
        depth = self._txn_depth.get(0)
        if depth == 0:
            await conn.execute("BEGIN IMMEDIATE")
        else:
            await conn.execute(f"SAVEPOINT sp_{depth}")
        self._txn_depth.set(depth + 1)

    async def end_transaction(self, *, commit: bool = True) -> None:
        """End an explicit transaction (decrements per-task depth counter)."""
        depth = self._txn_depth.get(0)
        if depth <= 0:
            return
        depth -= 1
        self._txn_depth.set(depth)

        conn = self._task_conn.get(None)
        if conn is None:
            return

        if depth == 0:
            # Outermost: commit or rollback entire transaction
            if commit:
                await conn.commit()
            else:
                try:
                    await conn.rollback()
                except Exception:
                    pass
            # Release connection back to pool
            await self._release()
        else:
            # Nested: release or rollback savepoint
            sp = f"sp_{depth}"
            try:
                if commit:
                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                else:
                    await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception:
                pass

    async def __aenter__(self) -> "AsyncSQLiteAdapter":
        await self._acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        conn = self._task_conn.get(None)
        if conn is None:
            return
        if exc_type is None:
            await conn.commit()
        else:
            await conn.rollback()
        await self._release()

    @property
    def connection(self) -> Optional[aiosqlite.Connection]:
        """Backward-compat: returns the task-pinned connection, if any."""
        return self._task_conn.get(None)

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
        return cls(uri, pragmas=pragmas, pool_size=1)

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
        return cls(path, pragmas=pragmas, pool_size=4)
