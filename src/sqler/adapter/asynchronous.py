import asyncio
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from functools import partial
from typing import Any, List, Optional, Self

import aiosqlite

from sqler.exceptions import ConnectionPoolExhaustedError, NotConnectedError, QueryError
from sqler.logging import query_logger
from sqler.pool import PoolStats

from .abstract import AsyncAdapterABC

logger = logging.getLogger(__name__)

DEFAULT_ACQUIRE_TIMEOUT_S: Optional[float] = 30.0


class _Lease:
    """One checkout of a pooled connection: the tasks using it, and whether it has gone back.

    A connection is pinned in a ``ContextVar``, so a task created while one is pinned
    inherits the pin. A lease is returned once, when its last holder releases it or ends;
    a pin whose lease has been returned is stale and is ignored.
    """

    __slots__ = ("conn", "holders", "returned")

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self.holders: dict[asyncio.Task, Any] = {}  # task -> its done callback
        self.returned = False


class AsyncSQLiteAdapter(AsyncAdapterABC):
    """Asynchronous SQLite connector using aiosqlite with connection pool.

    Uses an ``asyncio.Queue``-based pool of connections with ``ContextVar``
    per-task pinning so that sequential operations within one coroutine
    (execute → auto_commit) share the same connection.

    A task that ends without releasing its connection (cancelled between a
    statement and its commit, say) does not keep it: when the last task using
    the connection is done, the connection is rolled back and returned to the pool.

    A statement that fails outside an explicit transaction (an SQLite error, a
    cancellation, a failed commit or ``BEGIN``) also rolls the connection back and
    returns it. Inside an explicit transaction the connection stays pinned until
    the transaction ends, and its end returns it whether the commit or rollback
    works.

    ``acquire_timeout`` bounds the wait for a free connection, in seconds
    (``None`` waits forever); a wait that runs out raises
    ``ConnectionPoolExhaustedError``.
    """

    def __init__(
        self,
        path: str = "sqler.db",
        pragmas: Optional[list[str]] = None,
        pool_size: int = 1,
        acquire_timeout: Optional[float] = DEFAULT_ACQUIRE_TIMEOUT_S,
    ):
        self.path = path
        self.pragmas = pragmas or []
        self.acquire_timeout = acquire_timeout
        self._pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=pool_size)
        self._all_conns: list[aiosqlite.Connection] = []
        # _task_conn mirrors the pin for code that reads it; _task_lease is the source of truth.
        self._task_conn: ContextVar[Optional[aiosqlite.Connection]] = ContextVar(
            "task_conn", default=None
        )
        self._task_lease: ContextVar[Optional[_Lease]] = ContextVar("task_lease", default=None)
        self._txn_depth: ContextVar[int] = ContextVar("txn_depth", default=0)
        self._leases: set[_Lease] = set()  # checked out, not yet returned
        self._reclaims: set[asyncio.Task] = set()  # rollbacks of connections left by ended tasks
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._closing = asyncio.Event()  # set (and replaced) by close(), to wake waiting tasks
        # pool statistics
        self._created_at = datetime.now()
        self._checkouts = 0
        self._timeouts = 0
        self._wait_total_ms = 0.0
        self._wait_max_ms = 0.0
        self._waiting = 0  # tasks waiting for a free connection right now (private, for tests)

    async def _open(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path, uri=True)
        for pragma in self.pragmas:
            await conn.execute(pragma)
        await conn.commit()
        return conn

    async def connect(self) -> None:
        for _ in range(self._pool_size):
            conn = await self._open()
            self._all_conns.append(conn)
            self._pool.put_nowait(conn)
        self._connected = True

    def _pinned(self) -> Optional[aiosqlite.Connection]:
        """The connection pinned in this context, unless its lease has been returned."""
        lease = self._task_lease.get(None)
        if lease is None or lease.returned:
            return None
        return lease.conn

    def _hold(self, lease: _Lease) -> None:
        """Count the current task as using ``lease`` until it releases it or ends."""
        task = asyncio.current_task()
        if task is None or task in lease.holders:
            return
        callback = partial(self._holder_done, lease)
        lease.holders[task] = callback
        task.add_done_callback(callback)

    def _unhold(self, lease: _Lease, task: Optional[asyncio.Task]) -> None:
        callback = lease.holders.pop(task, None)
        if callback is not None and task is not None:
            task.remove_done_callback(callback)

    def _give_back(self, lease: _Lease) -> None:
        lease.returned = True
        self._leases.discard(lease)
        for task in list(lease.holders):
            self._unhold(lease, task)

    def _leave(self, lease: Optional[_Lease], task: Optional[asyncio.Task]) -> bool:
        """``task`` stops using ``lease``. True if it was the last holder: the lease is given back
        and the caller now owns returning the connection."""
        if lease is None or lease.returned:
            return False
        self._unhold(lease, task)
        if lease.holders:  # a task that shares it returns it when it is done
            return False
        self._give_back(lease)
        return True

    def _drop_pin(self) -> Optional[_Lease]:
        """Unpin this context's connection; return its lease if this task was the last holder."""
        lease = self._task_lease.get(None)
        self._task_conn.set(None)
        self._task_lease.set(None)
        return lease if self._leave(lease, asyncio.current_task()) else None

    def _holder_done(self, lease: _Lease, task: asyncio.Task) -> None:
        """A task using ``lease`` ended; if it was the last one, roll back and return the connection."""
        if self._leave(lease, task):
            self._start_reclaim(lease.conn)

    async def _reclaim(self, conn: aiosqlite.Connection) -> None:
        """Roll back a connection left by an ended task and return it to the pool.

        If the rollback fails while the adapter is open, the connection is closed and
        a fresh one takes its place, so the pool keeps its size.
        """
        try:
            await conn.rollback()
        except Exception as exc:
            if conn not in self._all_conns:  # the adapter closed under us: nothing to give back
                return
            logger.warning(
                "rollback of a reclaimed connection failed (%s: %s); replacing it", type(exc).__name__, exc
            )
            await self._replace(conn)
            return
        if conn in self._all_conns:  # not a connection from before a close()
            self._pool.put_nowait(conn)

    async def _replace(self, bad: aiosqlite.Connection) -> None:
        try:
            await bad.close()
        except Exception:
            pass
        try:
            fresh = await self._open()
        except Exception:
            logger.warning("could not open a replacement connection; the pool is one smaller", exc_info=True)
            if bad in self._all_conns:
                self._all_conns.remove(bad)
            return
        if bad not in self._all_conns:  # the adapter closed while we were opening
            await fresh.close()
            return
        self._all_conns[self._all_conns.index(bad)] = fresh
        self._pool.put_nowait(fresh)

    async def _acquire(self) -> aiosqlite.Connection:
        """Return the task-pinned connection, or get one from the pool and pin it."""
        lease = self._task_lease.get(None)
        if lease is not None and not lease.returned:
            self._hold(lease)
            return lease.conn
        if not self._connected:
            async with self._connect_lock:
                if not self._connected:
                    await self.connect()
        conn = await self._checkout()
        lease = _Lease(conn)
        self._leases.add(lease)
        self._task_conn.set(conn)
        self._task_lease.set(lease)
        self._hold(lease)
        return conn

    async def _checkout(self) -> aiosqlite.Connection:
        """Take a connection from the pool, waiting at most ``acquire_timeout`` seconds."""
        start = time.perf_counter()
        try:
            conn = self._pool.get_nowait()
        except asyncio.QueueEmpty:
            conn = await self._wait_for_connection()
        waited_ms = (time.perf_counter() - start) * 1000
        self._checkouts += 1
        self._wait_total_ms += waited_ms
        self._wait_max_ms = max(self._wait_max_ms, waited_ms)
        return conn

    async def _wait_for_connection(self) -> aiosqlite.Connection:
        closing = self._closing
        get = asyncio.ensure_future(self._pool.get())
        closed = asyncio.ensure_future(closing.wait())
        self._waiting += 1
        try:
            done, _ = await asyncio.wait(
                {get, closed}, timeout=self.acquire_timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:  # the waiting task was cancelled
            self._abandon(get)
            closed.cancel()
            raise
        finally:
            self._waiting -= 1
        closed.cancel()
        if get in done:
            return get.result()
        self._abandon(get)
        if closing.is_set():
            raise NotConnectedError("the adapter was closed while waiting for a pooled connection")
        self._timeouts += 1
        raise ConnectionPoolExhaustedError(
            f"no pooled connection became free within {self.acquire_timeout:g} s "
            f"(pool size {self._pool_size})",
            details={"pool_size": self._pool_size, "acquire_timeout": self.acquire_timeout},
        )

    def _abandon(self, get: asyncio.Future) -> None:
        """Stop waiting on ``get``; a connection it took already goes back to the pool."""
        if not get.done():
            get.cancel()  # Queue.get leaves the item in the queue when it is cancelled
            return
        if not get.cancelled() and get.exception() is None:
            conn = get.result()
            if conn in self._all_conns:
                self._pool.put_nowait(conn)

    def pool_stats(self) -> PoolStats:
        """Return the pool's size, free and in-use connections, checkouts, timeouts and wait times.

        Synchronous and cheap; safe to call from any task. A connection being rolled
        back after its task ended counts as in use until it is back in the pool.
        """
        total = len(self._all_conns)
        available = self._pool.qsize()
        return PoolStats(
            total_connections=total,
            available_connections=available,
            in_use_connections=max(total - available, 0),
            max_connections=self._pool_size,
            total_checkouts=self._checkouts,
            total_timeouts=self._timeouts,
            avg_wait_time_ms=self._wait_total_ms / self._checkouts if self._checkouts else 0.0,
            max_wait_time_ms=self._wait_max_ms,
            created_at=self._created_at,
        )

    async def _release_after_failure(self, exc: BaseException) -> None:
        """A statement, commit or rollback ended with ``exc``: outside a transaction, give the connection up.

        The current task stops holding it; if no other task shares it, it is rolled
        back and returned to the pool (or replaced if the rollback fails). Inside an
        explicit transaction nothing changes: the caller ends the transaction.

        An error from SQLite means the call has finished in the connection's thread, so
        the rollback runs at once and is awaited: the pool is whole when the caller sees
        the error. A cancellation (or another ``BaseException``) may arrive while the call
        is still running in that thread, and the rollback queues behind it (up to the
        busy timeout); it is not awaited, so the cancellation reaches the caller at once
        and the reclaim returns the connection when the thread is done.
        """
        if self._txn_depth.get(0) > 0:
            return
        lease = self._drop_pin()
        if lease is None:
            return
        reclaim = self._start_reclaim(lease.conn)
        if isinstance(exc, Exception):
            # shielded: a cancellation of this task while it waits must not stop the return
            await asyncio.shield(reclaim)

    def _start_reclaim(self, conn: aiosqlite.Connection) -> asyncio.Task:
        reclaim = asyncio.get_running_loop().create_task(self._reclaim(conn))
        self._reclaims.add(reclaim)
        reclaim.add_done_callback(self._reclaims.discard)
        return reclaim

    async def _release(self) -> None:
        """If not in a transaction, unpin the connection; the last task using it returns it to the pool."""
        if self._txn_depth.get(0) > 0:
            return
        lease = self._drop_pin()
        if lease is not None:
            self._pool.put_nowait(lease.conn)

    async def close(self) -> None:
        closing, self._closing = self._closing, asyncio.Event()
        closing.set()  # tasks waiting for a connection raise NotConnectedError
        for lease in list(self._leases):
            self._give_back(lease)
        if self._reclaims:
            await asyncio.gather(*self._reclaims, return_exceptions=True)
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
        _logging = query_logger.enabled
        if _logging:
            start = time.perf_counter()
        error_msg = None
        cursor = None
        try:
            cursor = await conn.execute(query, params or [])
        except BaseException as e:  # a cancellation counts as a failure
            error_msg = str(e) or type(e).__name__
            await self._release_after_failure(e)
            raise
        finally:
            if _logging:
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
        """Run ``query`` once per parameter list.

        Inside an explicit transaction the rows are part of it (a rollback removes
        them); outside one they are committed and the connection is returned.
        """
        conn = await self._acquire()
        try:
            cursor = await conn.executemany(query, param_list)
            if self._txn_depth.get(0) == 0:
                await conn.commit()
        except BaseException as exc:
            await self._release_after_failure(exc)
            raise
        await self._release()
        return cursor

    async def executescript(self, script: str) -> aiosqlite.Cursor:
        """Run a script of statements, commit, and return the connection.

        Raises ``QueryError`` inside an explicit transaction: SQLite's driver commits
        the open transaction before it runs a script, which would end it unseen.
        """
        if self._txn_depth.get(0) > 0:
            raise QueryError(
                "executescript cannot run inside a transaction: "
                "SQLite commits the open transaction before it runs a script",
                sql=script,
            )
        conn = await self._acquire()
        try:
            cursor = await conn.executescript(script)
            await conn.commit()
        except BaseException as exc:
            await self._release_after_failure(exc)
            raise
        await self._release()
        return cursor

    async def commit(self) -> None:
        conn = self._pinned()
        if conn is None:
            return
        await conn.commit()

    async def auto_commit(self) -> None:
        """Commit only if NOT inside an explicit transaction, then release connection."""
        if self._txn_depth.get(0) == 0:
            try:
                await self.commit()
            except BaseException as exc:
                await self._release_after_failure(exc)
                raise
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
        try:
            if depth == 0:
                await conn.execute("BEGIN IMMEDIATE")
            else:
                await conn.execute(f"SAVEPOINT sp_{depth}")
        except BaseException as exc:
            await self._release_after_failure(exc)  # a failed BEGIN leaves no transaction to end
            raise
        self._txn_depth.set(depth + 1)

    async def end_transaction(self, *, commit: bool = True) -> None:
        """End an explicit transaction (decrements per-task depth counter)."""
        depth = self._txn_depth.get(0)
        if depth <= 0:
            return
        depth -= 1
        self._txn_depth.set(depth)

        conn = self._pinned()
        if conn is None:
            return

        if depth == 0:
            # Outermost: commit or rollback entire transaction
            # The connection goes back whether the commit or rollback works.
            if commit:
                try:
                    await conn.commit()
                except BaseException as exc:
                    await self._release_after_failure(exc)
                    raise
            else:
                try:
                    await conn.rollback()
                except BaseException as exc:
                    # not raised (the caller may be handling an exception already),
                    # but the connection is rolled back again or replaced, never reused as is
                    await self._release_after_failure(exc)
                    if not isinstance(exc, Exception):
                        raise
                    return
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
        conn = self._pinned()
        if conn is None:
            return
        try:
            if exc_type is None:
                await conn.commit()
            else:
                await conn.rollback()
        except BaseException as exc:
            await self._release_after_failure(exc)
            raise
        await self._release()

    @property
    def connection(self) -> Optional[aiosqlite.Connection]:
        """Backward-compat: returns the task-pinned connection, if any."""
        return self._pinned()

    _PRAGMA_KEYS = [
        "foreign_keys", "busy_timeout", "journal_mode", "synchronous",
        "cache_size", "wal_autocheckpoint", "mmap_size", "temp_store",
    ]

    async def pragma_report(self) -> dict[str, Any]:
        """Return current PRAGMA values for key settings.

        Queries the active connection for each tracked PRAGMA and returns
        a dict of ``{pragma_name: value}``. Useful for downstream libraries
        to verify that expected PRAGMAs are in effect.

        Returns:
            dict[str, Any]: Mapping of PRAGMA names to their current values.
        """
        report: dict[str, Any] = {}
        for key in self._PRAGMA_KEYS:
            cur = await self.execute(f"PRAGMA {key};")
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
            report[key] = row[0] if row else None
        await self.auto_commit()
        return report

    # factories
    @classmethod
    def in_memory(
        cls,
        shared: bool = True,
        name: Optional[str] = None,
        *,
        acquire_timeout: Optional[float] = DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> Self:
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
        return cls(uri, pragmas=pragmas, pool_size=1, acquire_timeout=acquire_timeout)

    @classmethod
    def on_disk(
        cls, path: str = "sqler.db", *, acquire_timeout: Optional[float] = DEFAULT_ACQUIRE_TIMEOUT_S
    ) -> Self:
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
        return cls(path, pragmas=pragmas, pool_size=4, acquire_timeout=acquire_timeout)
