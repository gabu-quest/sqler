import asyncio
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from functools import partial
from typing import Any, List, Optional, Self

import aiosqlite

from sqler.exceptions import (
    ConnectionPoolExhaustedError,
    LockTimeoutError,
    NotConnectedError,
    QueryError,
    TransactionStateError,
    unique_violation_from,
)
from sqler.logging import query_logger
from sqler.pool import PoolStats

from .abstract import AsyncAdapterABC
from .synchronous import _read_hit_missing_table

logger = logging.getLogger(__name__)

DEFAULT_ACQUIRE_TIMEOUT_S: Optional[float] = 30.0

_ENDED = "the transaction this nested transaction was part of has ended; nothing written in it was committed"
_BROKEN = (
    "a nested transaction could not be {}, so the transaction it is part of ends rolled back: "
    "nothing written in it is committed"
)


def _broken(failed: tuple[str, Exception]) -> TransactionStateError:
    """The error for a statement or an end in a transaction where a nested end failed ``(how, error)``."""
    error = TransactionStateError(_BROKEN.format(failed[0]))
    error.__cause__ = failed[1]
    return error


class _Savepoint:
    """A savepoint on a connection's stack and the task that opened it.

    With no task it holds the height of nested transactions whose end is being sent, by ``closing``.
    """

    __slots__ = ("task", "closing")

    def __init__(self, task: Optional[asyncio.Task]):
        self.task = task
        self.closing: Optional[asyncio.Task] = None


class _Turn:
    """A context waiting on a connection's stack, in the order asked.

    Its ``future`` is done once it has its turn: ``True`` for a statement, the savepoint
    put on top for a nested transaction. It stays in the queue until its task runs again
    and hands its statement to the connection, and holds its place until then: no other
    context opens a nested transaction on top of it.
    """

    __slots__ = ("inside", "task", "opens", "future")

    def __init__(self, inside: tuple[_Savepoint, ...], task: Optional[asyncio.Task], opens: bool):
        self.inside = inside  # the savepoints its context is inside
        self.task = task
        self.opens = opens  # it opens a nested transaction
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()


_OWN_ENDED = _Savepoint(None)  # the place of a context whose own nested transaction has ended under it


class _Lease:
    """One checkout of a pooled connection: the tasks using it, and whether it has gone back.

    A connection is pinned in a ``ContextVar``, so a task created while one is pinned
    inherits the pin. A lease is returned once, when its last holder releases it or ends,
    or when its transaction ends; a pin whose lease has been returned is stale and is ignored.
    """

    __slots__ = ("conn", "holders", "returned", "interrupted", "txn", "savepoints", "turns", "broken")

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self.holders: dict[asyncio.Task, Any] = {}  # task -> its done callback
        self.returned = False
        # a statement of this lease was cancelled inside a transaction: it may still be
        # running in the connection's thread, so a rollback would queue behind it
        self.interrupted = False
        self.txn = False  # a transaction is open on the connection
        self.savepoints: list[_Savepoint] = []  # the connection's open savepoints, innermost last
        self.turns: list[_Turn] = []  # contexts waiting for their turn, in the order they asked
        # a nested transaction's end that failed (how it ended, the error): the transaction ends rolled back
        self.broken: Optional[tuple[str, Exception]] = None


class _Cursor:
    """The cursor ``execute`` returns: an ``aiosqlite`` cursor whose failed fetch gives its connection back.

    Every attribute and method is the wrapped cursor's. A ``fetchone``, ``fetchall``,
    ``fetchmany`` or iteration that raises (or is cancelled) ends the statement like a
    failed ``execute`` does: outside a transaction the task stops holding the connection.
    """

    __slots__ = ("_cursor", "_adapter", "_lease")

    def __init__(self, cursor: aiosqlite.Cursor, adapter: "AsyncSQLiteAdapter", lease: Optional[_Lease]):
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_adapter", adapter)
        object.__setattr__(self, "_lease", lease)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._cursor, name, value)

    async def _failed(self, exc: BaseException) -> None:
        # a pin that is not this statement's (already given back, or a later one) is not ours to drop
        if self._lease is not None and self._adapter._task_lease.get(None) is self._lease:
            await self._adapter._release_after_failure(exc)

    async def fetchone(self):
        try:
            return await self._cursor.fetchone()
        except BaseException as exc:
            await self._failed(exc)
            raise

    async def fetchmany(self, size: Optional[int] = None):
        try:
            return await self._cursor.fetchmany(size)
        except BaseException as exc:
            await self._failed(exc)
            raise

    async def fetchall(self):
        try:
            return await self._cursor.fetchall()
        except BaseException as exc:
            await self._failed(exc)
            raise

    async def close(self) -> None:
        await self._cursor.close()

    async def __aiter__(self):
        try:
            async for row in self._cursor:
                yield row
        except GeneratorExit:
            raise
        except BaseException as exc:
            await self._failed(exc)
            raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._cursor.close()


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

    Tasks started inside a transaction share its connection, which has one stack of
    savepoints. A context's place is its innermost savepoint still open on that stack,
    else the transaction itself; whether it is in a transaction, how deep, and the name
    of its next savepoint are read from there. It sends a statement on the connection
    only while the top of the stack is its place; otherwise it waits its turn, in the
    order asked, at most ``acquire_timeout`` seconds, then ``LockTimeoutError``. A turn it
    has been given holds its place until its statement has been handed to the
    connection: no other context's nested transaction opens on top of it before. A
    transaction ends for every context that shares it when its ``COMMIT`` or
    ``ROLLBACK`` is handed to the connection; from then on they are in no transaction.
    A transaction or nested transaction that ends while another context's nested
    transaction is open on top of it is rolled back instead, and both get
    ``TransactionStateError``; so does one in which the end of a nested transaction failed,
    and from that failure on every statement sent in it raises that error and sends nothing.
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
        # the savepoints this context opened or was started inside, innermost last
        self._txn_savepoints: ContextVar[tuple[_Savepoint, ...]] = ContextVar("txn_savepoints", default=())
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
        lease.txn = False
        self._leases.discard(lease)
        for turn in lease.turns:  # the transaction they wait in has ended: they look again
            if not turn.future.done():
                turn.future.set_result(None)
        lease.turns.clear()
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
        """A task using ``lease`` ended; if it was the last one, roll back and return the connection.

        A nested transaction it left open is rolled back and closed, and the turn passes on.
        """
        if self._leave(lease, task):
            self._start_reclaim(lease.conn)
        elif lease.txn and (left := next((sp for sp in lease.savepoints if sp.task is task), None)):
            self._end_savepoint(lease, left, rollback=True)

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
        """Return the task-pinned connection once it is this context's turn, or get one from the pool and pin it."""
        lease = self._task_lease.get(None)
        while lease is not None and not lease.returned:
            self._hold(lease)
            if not lease.txn:
                return lease.conn
            if lease.broken:
                raise _broken(lease.broken)
            inside = self._txn_savepoints.get()
            if self._turn(lease, inside) or await self._wait_turn(lease, inside, opens=False):
                return lease.conn
        if inside := self._txn_savepoints.get():  # the transaction it was in has ended: it is in none
            if self._place((), inside) is _OWN_ENDED:
                raise TransactionStateError(_ENDED)
            self._txn_savepoints.set(())
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
        if (lease := self._txn()) is not None:
            if not isinstance(exc, Exception):  # cancelled: the statement may still be running
                lease.interrupted = True
            return
        lease = self._drop_pin()
        if lease is not None:
            await self._give_up(lease.conn, exc)

    async def _give_up(self, conn: aiosqlite.Connection, exc: BaseException) -> None:
        """Roll back and return ``conn`` after ``exc``; awaited unless ``exc`` is a cancellation."""
        reclaim = self._start_reclaim(conn)
        if isinstance(exc, Exception):
            # shielded: a cancellation of this task while it waits must not stop the return
            await asyncio.shield(reclaim)

    def _start_reclaim(self, conn: aiosqlite.Connection) -> asyncio.Task:
        return self._background(self._reclaim(conn))

    def _background(self, work) -> asyncio.Task:
        """Run ``work`` as a task that ``close()`` waits for."""
        task = asyncio.get_running_loop().create_task(work)
        self._reclaims.add(task)
        task.add_done_callback(self._reclaims.discard)
        return task

    async def _release(self) -> None:
        """If not in a transaction, unpin the connection; the last task using it returns it to the pool."""
        if self._txn() is not None:
            return
        lease = self._drop_pin()
        if lease is not None:
            self._pool.put_nowait(lease.conn)

    async def close(self) -> None:
        closing, self._closing = self._closing, asyncio.Event()
        closing.set()  # tasks waiting for a connection or a turn raise NotConnectedError
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
        was_pinned = self._pinned() is not None
        conn = await self._acquire()
        _logging = query_logger.enabled
        if _logging:
            start = time.perf_counter()
        error_msg = None
        cursor = None
        try:
            cursor = await conn.execute(query, params or [])
        except BaseException as exc:  # a cancellation counts as a failure
            error_msg = str(exc) or type(exc).__name__
            # a read of a missing table changed nothing: work this task left uncommitted
            # on its pinned connection stays for the caller to commit; a pinned connection
            # with nothing uncommitted is given back, as after any other failed statement
            if not (was_pinned and conn.in_transaction and _read_hit_missing_table(query, exc)):
                await self._release_after_failure(exc)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
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
        return _Cursor(cursor, self, self._task_lease.get(None))  # type: ignore[return-value]

    async def executemany(self, query: str, param_list: List[List[Any]]) -> aiosqlite.Cursor:
        """Run ``query`` once per parameter list.

        Inside an explicit transaction the rows are part of it (a rollback removes
        them); outside one they are committed and the connection is returned.
        """
        conn = await self._acquire()
        outside = self._txn() is None
        try:
            cursor = await conn.executemany(query, param_list)
            if outside:
                await conn.commit()
        except BaseException as exc:
            await self._release_after_failure(exc)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        await self._release()
        return cursor

    async def executescript(self, script: str) -> aiosqlite.Cursor:
        """Run a script of statements, commit, and return the connection.

        Raises ``QueryError`` inside an explicit transaction: SQLite's driver commits
        the open transaction before it runs a script, which would end it unseen.
        """
        if self._txn() is not None:
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
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
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
        if self._txn() is None:
            try:
                await self.commit()
            except BaseException as exc:
                await self._release_after_failure(exc)
                raise
            await self._release()

    @property
    def in_transaction(self) -> bool:
        """Return True if currently inside an explicit transaction."""
        return self._txn() is not None

    def _txn(self) -> Optional[_Lease]:
        """The lease of the transaction this context is in; None outside one, also once it has ended."""
        lease = self._task_lease.get(None)
        return lease if lease is not None and lease.txn else None

    @staticmethod
    def _place(stack: list[_Savepoint], inside: tuple[_Savepoint, ...], task: Optional[asyncio.Task] = None):
        """The innermost savepoint of ``inside`` still open on ``stack``; None: the transaction itself.

        ``_OWN_ENDED`` if one that ``task`` (default: the current task) opened has ended under it.
        """
        task = task or asyncio.current_task()
        for sp in reversed(inside):
            if sp in stack:
                return sp
            if sp.task is task:
                return _OWN_ENDED
        return None

    def _depth(self) -> int:
        """0 outside a transaction, 1 directly in one, one more for each open savepoint this context is inside."""
        lease = self._txn()
        if lease is None:
            return 0
        place = self._place(lease.savepoints, self._txn_savepoints.get())
        return 0 if place is _OWN_ENDED else 1 if place is None else lease.savepoints.index(place) + 2

    def _turn(self, lease: _Lease, inside: tuple[_Savepoint, ...]) -> bool:
        """Whether the top of ``lease``'s stack is the place of a context inside ``inside``."""
        stack = lease.savepoints
        top = stack[-1] if stack else None
        if top is (inside[-1] if inside else None):
            return True
        place = self._place(stack, inside)
        if place is _OWN_ENDED:
            raise TransactionStateError(_ENDED)
        return place is top

    async def _wait_turn(self, lease: _Lease, inside: tuple[_Savepoint, ...], *, opens: bool):
        """Wait, in the order asked, until the top of ``lease``'s stack is this context's place.

        Returns True, or with ``opens`` the savepoint put on top for it; None when the
        transaction has ended before it ran. It takes its turn in the step it returns in,
        so the caller hands its statement to the connection before any other context
        runs. If its place ended between its turn and its run (a nested transaction it was
        in ended), it waits again where it was in the order.
        """
        loop = asyncio.get_running_loop()
        closing = self._closing
        deadline = None if self.acquire_timeout is None else loop.time() + self.acquire_timeout
        turn = _Turn(inside, asyncio.current_task(), opens)
        lease.turns.append(turn)
        while True:
            try:
                await asyncio.wait({turn.future}, timeout=None if deadline is None else deadline - loop.time())
            except BaseException:  # cancelled while waiting, or after its turn and before it ran
                self._drop_turn(lease, turn)
                raise
            if closing.is_set():
                raise NotConnectedError("the adapter was closed while a statement waited for its turn")
            if not lease.txn:
                return None
            if lease.broken:
                self._drop_turn(lease, turn)
                raise _broken(lease.broken)
            if not turn.future.done():
                self._drop_turn(lease, turn)
                raise LockTimeoutError(
                    f"waited {self.acquire_timeout:g} s for a nested transaction of another task on the same "
                    "connection to end: either it is still running, or it is waiting for this task and can never end"
                )
            granted = turn.future.result()
            if granted is None:  # a nested transaction of its own has ended under it
                raise TransactionStateError(_ENDED)
            stack = lease.savepoints
            if (granted in stack) if opens else (self._place(stack, inside) is (stack[-1] if stack else None)):
                lease.turns.remove(turn)
                if not opens:  # the place it held is free: its statement is handed over before anyone runs
                    self._hand_turns(lease)
                return granted
            turn.future = loop.create_future()
            self._hand_turns(lease)

    def _drop_turn(self, lease: _Lease, turn: _Turn) -> None:
        """Take ``turn`` out of the queue; a turn it had and did not take passes on."""
        if turn not in lease.turns:  # the transaction, or a nested transaction of its own, ended
            return
        lease.turns.remove(turn)
        if turn.future.done():
            if turn.opens:
                self._pop_to(lease, turn.future.result())
            else:
                self._hand_turns(lease)

    def _hand_turns(self, lease: _Lease) -> None:
        """Give their turn, in the order asked, to the waiting contexts whose place is the top of the stack.

        One that opens a nested transaction gets a savepoint put on top for it, once every
        context given its turn has taken it; those that asked after it wait behind it. In a
        transaction whose nested end failed nobody gets a turn: every waiter is woken to raise.
        """
        if lease.broken:
            for turn in lease.turns:
                if not turn.future.done():
                    turn.future.set_result(None)
            return
        stack = lease.savepoints
        top = stack[-1] if stack else None
        held = any(turn.future.done() for turn in lease.turns)
        behind = False
        for turn in list(lease.turns):
            if turn.future.done():
                continue
            place = self._place(stack, turn.inside, turn.task)
            if place is _OWN_ENDED:
                lease.turns.remove(turn)
                turn.future.set_result(None)  # it raises
            elif behind or place is not top:
                continue
            elif not turn.opens:
                turn.future.set_result(True)
                held = True
            elif held:
                behind = True
            else:
                stack.append(top := _Savepoint(turn.task))
                turn.future.set_result(top)

    async def begin_transaction(self) -> None:
        """Begin an explicit transaction, or a nested one inside the transaction this context is in.

        Uses BEGIN IMMEDIATE to acquire a write lock immediately,
        preventing SQLITE_BUSY errors during the transaction.
        Uses SAVEPOINTs for nested transactions to allow proper inner rollback.
        A nested transaction opens on top of this context's place: while another
        context's nested transaction is open above it, or another context has its turn
        there and has not taken it, it waits its turn.
        """
        lease = self._txn()
        if lease is None:
            conn = await self._acquire()
            try:
                await conn.execute("BEGIN IMMEDIATE")
            except BaseException as exc:
                await self._release_after_failure(exc)  # a failed BEGIN leaves no transaction to end
                raise
            self._task_lease.get().txn = True
            return
        if lease.broken:
            raise _broken(lease.broken)
        self._hold(lease)
        inside = self._txn_savepoints.get()
        if self._turn(lease, inside) and not any(turn.future.done() for turn in lease.turns):
            lease.savepoints.append(sp := _Savepoint(asyncio.current_task()))
        elif (sp := await self._wait_turn(lease, inside, opens=True)) is None:
            raise TransactionStateError(_ENDED)
        try:
            await lease.conn.execute(f"SAVEPOINT sp_{len(lease.savepoints)}")
        except BaseException as exc:
            if isinstance(exc, Exception):
                self._pop_to(lease, sp)  # SQLite refused it: it opened nothing
            else:
                self._end_savepoint(lease, sp, rollback=True)  # a cancelled SAVEPOINT still runs
            await self._release_after_failure(exc)
            raise
        self._txn_savepoints.set((*inside, sp))

    def _pop_to(self, lease: _Lease, sp: _Savepoint) -> None:
        """Take ``sp`` and every savepoint above it off the stack, and hand the turn on."""
        stack = lease.savepoints
        if sp in stack:
            del stack[stack.index(sp) :]
        self._hand_turns(lease)

    def _end_savepoint(self, lease: _Lease, sp: _Savepoint, *, rollback: bool) -> Optional[asyncio.Task]:
        """Close ``sp`` and every savepoint above it, in a task that a cancelled caller does not stop.

        Their height is held until the end has been sent, so no context sends a statement
        there before; ends above it that are being sent go first. The task returns how the
        end failed, or None.
        """
        stack = lease.savepoints
        if sp not in stack:
            return None
        index = stack.index(sp)
        before = [above.closing for above in stack[index:] if above.closing]
        stack[index:] = [held := _Savepoint(None)]
        name = f"sp_{index + 1}"
        statements = [f"ROLLBACK TO SAVEPOINT {name}"] * rollback + [f"RELEASE SAVEPOINT {name}"]
        how = "rolled back" if rollback else "committed"
        held.closing = self._background(self._send_and_close(lease, held, statements, before, how))
        return held.closing

    async def _send_and_close(self, lease: _Lease, held: _Savepoint, statements: list[str], before, how: str):
        """Send ``statements`` in order, then take ``held`` off the stack; return ``(how, error)`` if one failed.

        A failure stops them (a RELEASE after a failed ROLLBACK TO would keep the rows it
        was to undo) and the transaction ends rolled back.
        """
        try:
            if before:
                await asyncio.wait(before)
            for sql in statements:
                if not lease.txn:  # the transaction has ended: the connection is no longer this one's
                    return None
                try:
                    await lease.conn.execute(sql)
                except Exception as exc:
                    lease.broken = lease.broken or (how, exc)
                    return how, exc
            return None
        finally:
            self._pop_to(lease, held)

    async def _settle(self, lease: _Lease, sp: Optional[_Savepoint]) -> None:
        """Wait until no end of a nested transaction above ``sp`` (None: the transaction) is being sent."""
        while lease.txn and (sp is None or sp in lease.savepoints):
            stack = lease.savepoints
            closing = [above.closing for above in stack[stack.index(sp) + 1 if sp else 0 :] if above.closing]
            if not closing:
                return
            await asyncio.wait(closing)

    async def end_transaction(self, *, commit: bool = True) -> None:
        """End this context's own innermost nested transaction, or else the transaction itself.

        Ends of nested transactions above it that are being sent go first. Raises
        ``TransactionStateError`` when the nested transaction has already ended under
        this context; when it commits while a nested transaction of another context is
        open on top of it (it is rolled back instead); and when the end of a nested
        transaction in it failed (the transaction ends rolled back, and a nested
        transaction ended after that failure sends nothing).
        """
        inside = self._txn_savepoints.get()
        lease = self._txn()
        own = inside[-1] if inside and inside[-1].task is asyncio.current_task() else None
        if own is not None:
            self._txn_savepoints.set(inside[:-1])
        if lease is not None and lease.savepoints and lease.savepoints[-1] is not own:
            await self._settle(lease, own)
        if own is not None and (lease is None or not lease.txn or own not in lease.savepoints):
            raise TransactionStateError(_ENDED)
        if lease is None or not lease.txn:
            return
        stack = lease.savepoints
        above = len(stack) - (stack.index(own) + 1 if own is not None else 0)
        if own is None:
            failed = lease.broken
            await self._end(lease, commit and not above and not failed)
        elif lease.broken:  # the transaction ends rolled back: its end undoes this one too
            failed = lease.broken
            self._pop_to(lease, own)
        else:
            failed = await asyncio.shield(self._end_savepoint(lease, own, rollback=not commit or above > 0))
        if failed is not None:
            raise _broken(failed)
        if commit and above:
            raise TransactionStateError(
                f"the transaction was rolled back: {above} nested transaction"
                + (" of another task was" if above == 1 else "s of other tasks were")
                + " still open when it ended; end them before the block ends "
                "(await the tasks inside the block, or use a TaskGroup)"
            )

    async def _end(self, lease: _Lease, commit: bool) -> None:
        """Send ``COMMIT`` or ``ROLLBACK``: the transaction ends for every context, and the connection goes back."""
        self._give_back(lease)
        self._task_conn.set(None)
        self._task_lease.set(None)
        conn = lease.conn
        if not commit and lease.interrupted:
            # a cancelled statement may still be running in the connection's thread and the
            # rollback would queue behind it (up to the busy timeout): the caller goes on at
            # once, and the reclaim rolls back and returns the connection when the thread is done
            self._start_reclaim(conn)
            return
        try:
            await (conn.commit() if commit else conn.rollback())
        except BaseException as exc:
            # a failed rollback is not raised (the caller may be handling an exception already),
            # but the connection is rolled back again or replaced, never reused as is
            await self._give_up(conn, exc)
            if commit or not isinstance(exc, Exception):
                raise
            return
        if conn in self._all_conns:  # not a connection the adapter closed meanwhile
            self._pool.put_nowait(conn)

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
