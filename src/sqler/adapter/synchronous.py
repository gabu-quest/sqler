import sqlite3
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from itertools import islice
from typing import Any, ContextManager, Optional, Self

from sqler.logging import query_logger

from ..exceptions import NotConnectedError, QueryError, TransactionStateError, unique_violation_from
from .abstract import AdapterABC, CursorLike

_BROKEN = (
    "a nested transaction could not be {}, so the transaction it is part of ends rolled back: "
    "nothing written in it is committed"
)


def _broken(failed: tuple[str, BaseException]) -> TransactionStateError:
    """The error for a statement or an end in a transaction where a nested end failed ``(how, error)``."""
    error = TransactionStateError(_BROKEN.format(failed[0]))
    error.__cause__ = failed[1]
    return error


def _read_hit_missing_table(query: str, exc: BaseException) -> bool:
    """True when ``query`` is a read and ``exc`` is SQLite's "no such table" for it.

    A read opens no transaction and changes nothing, so its failure says nothing about
    the thread's earlier uncommitted statements: they stay for the caller to commit.
    Callers (a model read of a table nobody created) answer "empty" for it.
    """
    if not isinstance(exc, sqlite3.OperationalError) or not str(exc).startswith("no such table"):
        return False
    words = query.split(None, 1)
    return bool(words) and words[0].upper() in ("SELECT", "WITH")


def _names_memory_database(path: str) -> bool:
    """True for a ``file:`` URI that names an in-memory database: ``file::memory:``,
    ``mode=memory`` or ``vfs=memdb`` (not the plain ``":memory:"``, which is one connection)."""
    if not path.startswith("file:"):
        return False
    name, _, query = path[5:].partition("?")
    options = query.partition("#")[0].split("&")
    return name == ":memory:" or "mode=memory" in options or "vfs=memdb" in options


class _ThreadConnection:
    """Closes one thread's connection when the thread ends.

    Only the thread's local storage holds it (the adapter's registry holds it
    weakly). CPython drops a thread's local storage as the thread ends, before
    ``join()`` returns, so the connection closes then: SQLite rolls back what the
    thread left uncommitted and its lock is free.
    """

    __slots__ = ("conn", "__weakref__")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass  # Connection may already be closed

    __del__ = close


class _ThreadState(threading.local):
    """One thread's connection (``None`` until it opens one), depth of explicit transactions,
    and how a nested end in its transaction failed (``None``: none did)."""

    conn: Optional[sqlite3.Connection] = None
    txn_depth = 0
    broken: Optional[tuple[str, BaseException]] = None


class _SharedConnection:
    """The one connection of a plain ``":memory:"`` adapter, which every thread shares.

    A transaction on it belongs to the thread that opened it, by ``BEGIN`` or by the
    write Python's ``sqlite3`` opens one for: until it ends, every statement of another
    thread waits, at most ``timeout`` seconds, as a second connection waits for a locked
    file database. ``take`` holds the turn's lock for one statement and ``give`` frees it;
    the thread stays the holder while its transaction is open. A thread that ends as the
    holder has its transaction rolled back.

    Every change that can let a waiter go on (the holder gives the connection up, ends, or
    the connection closes) wakes every waiter: each checks again, and those that still
    cannot go on wait again. A waiter that leaves ``take`` by an exception holds nothing.
    """

    __slots__ = ("conn", "timeout", "_turn", "_holder", "_waiting", "_closed", "_ends", "__weakref__")

    def __init__(self, conn: sqlite3.Connection, timeout: float):
        self.conn = conn
        self.timeout = timeout
        self._turn = threading.Condition(threading.Lock())
        self._holder: Optional[int] = None  # thread ident; None: no transaction is open
        self._waiting = 0  # threads that wait for the holder's transaction to end
        self._closed = False
        self._ends = threading.local()  # per thread: what gives the turn back when it ends

    def _free(self) -> bool:
        return self._holder is None or self._closed

    def take(self, broken: Optional[tuple[str, BaseException]] = None) -> sqlite3.Connection:
        """Wait for the turn and hold it for one statement; the caller then calls ``give``.

        ``broken``: the calling thread's transaction had a nested end fail; nothing is taken.
        """
        if broken is not None:
            raise _broken(broken)
        turn = self._turn
        turn.acquire()
        try:
            holder = self._holder
            if holder is not None and holder != threading.get_ident():
                self._waiting += 1
                try:
                    free = turn.wait_for(self._free, self.timeout)
                finally:
                    self._waiting -= 1
                if not free:
                    raise sqlite3.OperationalError(
                        "database is locked: another thread's transaction holds the in-memory connection"
                    )
            if self._closed:
                raise NotConnectedError("Database not connected, call connect() first")
        except BaseException:  # an interrupt while waiting too: nothing stays held
            turn.release()
            raise
        return self.conn

    def give(self, depth: int) -> None:
        """After the statement: the thread keeps the turn while its transaction is open."""
        try:
            try:
                open_ = depth > 0 or self.conn.in_transaction
            except sqlite3.Error:  # the connection was closed under the statement
                open_ = False
            if open_:
                if self._holder is None:
                    if getattr(self._ends, "end", None) is None:
                        self._ends.end = _TurnEnd(self)
                    self._holder = threading.get_ident()
            elif self._holder is not None:
                self._holder = None
                if self._waiting:
                    self._turn.notify_all()
        finally:
            self._turn.release()

    def end_of(self, ident: int) -> None:
        """Thread ``ident`` ended: if it held the turn, roll its transaction back and free it."""
        with self._turn:
            if self._holder != ident:
                return
            self._holder = None
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            finally:
                self._turn.notify_all()

    def close(self) -> None:
        """Close the connection; a thread that waits for the turn raises ``NotConnectedError``."""
        with self._turn:
            self._closed = True
            self._holder = None
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            finally:
                self._turn.notify_all()


class _TurnEnd:
    """Held by one thread's local storage only: dropped as the thread ends, it hands the
    turn of the shared connection back (see ``_ThreadConnection`` for when that runs)."""

    __slots__ = ("shared", "ident")

    def __init__(self, shared: _SharedConnection):
        self.shared = weakref.ref(shared)  # weak: ``shared._ends`` holds this
        self.ident = threading.get_ident()

    def __del__(self) -> None:
        shared = self.shared()
        if shared is not None:
            shared.end_of(self.ident)


class _ReadRows:
    """The rows of one statement on the shared ``":memory:"`` connection, all read inside
    the statement's turn, served as its ``sqlite3.Cursor`` would serve them.

    SQLite reads a statement's rows as they are fetched; on a connection another thread
    writes on once the turn is given back, a later fetch would see that thread's
    uncommitted rows.
    """

    __slots__ = ("description", "rowcount", "lastrowid", "arraysize", "_rows")

    def __init__(self, cursor: sqlite3.Cursor):
        self._rows: Optional[Iterator[Any]] = iter(cursor.fetchall())
        self.description = cursor.description
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid
        self.arraysize = cursor.arraysize
        cursor.close()

    def _left(self) -> Iterator[Any]:
        rows = self._rows
        if rows is None:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        return rows

    def fetchone(self) -> Any:
        return next(self._left(), None)

    def fetchmany(self, size: Optional[int] = None) -> list[Any]:
        return list(islice(self._left(), self.arraysize if size is None else size))

    def fetchall(self) -> list[Any]:
        return list(self._left())

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> Any:
        return next(self._left())

    def close(self) -> None:
        self._rows = None


class SQLiteAdapter(AdapterABC):
    """Synchronous SQLite adapter with WAL and thread-local connections.

    Features:
        - Thread-local connections for safe concurrent access; a thread's
          connection closes when the thread ends
        - A plain ``":memory:"`` adapter has one connection for every thread: a
          transaction on it belongs to the thread that opened it, and the other
          threads' statements wait for it to end (at most ``timeout_ms``)
        - WAL mode for improved concurrency
        - One lock wait, ``timeout_ms``, on every connection
        - Optional slow query logging
        - Transaction-aware auto-commit (respects explicit transactions)
        - A statement or commit that fails outside an explicit transaction
          rolls back the transaction Python's ``sqlite3`` opened for it, so the
          thread's connection keeps no lock
        - A nested transaction whose end fails in SQLite ends its transaction
          rolled back: that end, every later statement of the thread in the
          transaction and the transaction's own end raise ``TransactionStateError``

    Args:
        path: Path to the database file, or ":memory:" for in-memory.
        pragmas: Optional list of PRAGMA statements to execute on connect.
        timeout_ms: How long a statement waits for a lock, in milliseconds.
    """

    def __init__(
        self,
        path: str = "sqler.db",
        pragmas: Optional[list[str]] = None,
        timeout_ms: Optional[int] = None,
    ):
        """Initialize the adapter.

        Args:
            path: Path to the SQLite database file.
            pragmas: Optional list of SQL PRAGMA statements to apply on connection.
            timeout_ms: How long a statement waits for another connection's lock, in
                milliseconds (SQLite's ``busy_timeout`` on every connection the adapter
                opens; on a plain ``":memory:"`` adapter, also the wait for another
                thread's transaction). It does not reach a shared-cache in-memory
                database: SQLite calls no busy handler for a table lock there, so a
                statement that meets one raises ``sqlite3.OperationalError`` ("database
                table is locked") at once.
                Not given: a ``busy_timeout`` pragma in ``pragmas``, else 5000. Given
                beside such a pragma, the two must agree (``connect()`` raises
                ``ValueError`` otherwise).

        Raises:
            ValueError: ``timeout_ms`` is not a whole number from 0 to 2**31 - 1.
        """
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 0 <= timeout_ms < 2**31
        ):
            raise ValueError(f"timeout_ms must be a whole number of milliseconds from 0 to 2**31 - 1, not {timeout_ms!r}")
        self.path = path
        self.pragmas = pragmas or []
        self.timeout_ms = 5000 if timeout_ms is None else timeout_ms
        self._timeout_given = timeout_ms is not None
        self._local = _ThreadState()
        self._conns_lock = threading.RLock()
        self._conns: weakref.WeakSet[_ThreadConnection] = weakref.WeakSet()
        self._connected: bool = False
        # The one connection of a plain ":memory:" adapter (see ``_SharedConnection``).
        self._shared: Optional[_SharedConnection] = None
        # SQLite drops a shared-cache in-memory database when its last connection closes,
        # and a thread's connection closes when the thread ends. This connection runs no
        # statement; it keeps the database alive from connect() to close().
        self._anchor: Optional[sqlite3.Connection] = None
        self._memory_singleton = path == ":memory:"

    @property
    def _txn_depth(self) -> int:
        """Depth of this thread's explicit transactions (0 = none).

        Per thread on every database: on the single ``":memory:"`` connection a
        transaction belongs to the thread that opened it.
        """
        return self._local.txn_depth

    @_txn_depth.setter
    def _txn_depth(self, value: int) -> None:
        self._local.txn_depth = value

    def _end_failed_implicit_transaction(self, conn: sqlite3.Connection) -> None:
        """A statement or commit on ``conn`` failed: outside an explicit transaction, roll back.

        Python's ``sqlite3`` opens a transaction before a write and keeps it open
        when the write fails; left open it holds the write lock (or a shared-cache
        table lock) until this thread's next commit. Inside an explicit
        transaction nothing changes: the caller ends the transaction.
        """
        if self._txn_depth > 0 or not conn.in_transaction:
            return
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # the caller sees the original error

    def connect(self) -> None:
        if self._memory_singleton:
            with self._conns_lock:
                if self._shared is None:
                    self._shared = _SharedConnection(self._open(), self.timeout_ms / 1000)
        elif self._anchor is None and _names_memory_database(self.path):
            with self._conns_lock:
                if self._anchor is None:
                    self._anchor = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        was_connected = self._connected
        self._connected = True
        try:
            self._conn()  # a connection for this thread
        except BaseException:
            if not was_connected:
                self.close()  # the connect that failed leaves nothing open
            raise

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            # not connect(timeout=): Python truncates the seconds to milliseconds (1.001 s waits 1000 ms)
            cur.execute(f"PRAGMA busy_timeout = {self.timeout_ms}")
            for pragma in self.pragmas:
                cur.execute(pragma)
            conn.commit()
            if self.pragmas:
                # SQLite has read the pragmas: its busy_timeout is the one number
                busy = cur.execute("PRAGMA busy_timeout").fetchone()[0]
                if busy != self.timeout_ms:
                    if self._timeout_given:
                        raise ValueError(
                            f"timeout_ms={self.timeout_ms} and the busy_timeout pragma ({busy}) disagree: "
                            "give one of them"
                        )
                    self.timeout_ms = busy
        except BaseException:
            conn.close()  # a connection that could not be set up is not left open
            raise
        return conn

    def _holding(self) -> ContextManager[sqlite3.Connection]:
        """This thread's connection, held for a block of work outside the statement methods
        (a backup, a restore): on a plain ``":memory:"`` adapter the block waits for the turn
        as a statement does and gives it back by the same rule; elsewhere it is ``_conn()``."""
        shared = self._shared
        return nullcontext(self._conn()) if shared is None else self._turn_of(shared)

    @contextmanager
    def _turn_of(self, shared: _SharedConnection) -> Iterator[sqlite3.Connection]:
        conn = shared.take(self._local.broken)
        try:
            yield conn
        finally:
            shared.give(self._txn_depth)

    def _conn(self) -> sqlite3.Connection:
        """This thread's connection; on a plain ``":memory:"`` adapter the shared one,
        whose statements go through ``_shared.take()`` and ``give()``. Code outside the
        adapter that works on the connection itself holds it with ``_holding()``.

        Raises ``TransactionStateError`` in a transaction whose nested end failed: the ends
        of a transaction read ``_local.conn`` themselves."""
        if not self._connected:
            raise NotConnectedError("Database not connected, call connect() first")
        local = self._local  # read once: close() on another thread replaces it
        if local.broken is not None:
            raise _broken(local.broken)
        conn = local.conn
        if conn is not None:
            return conn
        if self._memory_singleton:
            shared = self._shared
            if shared is None:  # close() on another thread
                raise NotConnectedError("Database not connected, call connect() first")
            return shared.conn
        conn = self._open()
        local.conn = conn  # what every statement reads
        local.owner = owner = _ThreadConnection(conn)
        with self._conns_lock:
            self._conns.add(owner)
        return conn

    def close(self) -> None:
        self._connected = False
        with self._conns_lock:
            for owner in list(self._conns):
                owner.close()
            self._conns.clear()
        shared, self._shared = self._shared, None
        if shared is not None:
            shared.close()
        with self._conns_lock:
            anchor, self._anchor = self._anchor, None
        if anchor is not None:
            try:
                anchor.close()
            except sqlite3.Error:
                pass
        # A fresh thread-local object: a thread that outlives close() holds nothing of the
        # old one (connection, transaction depth) and opens a new connection on connect().
        self._local = _ThreadState()

    def execute(self, query: str, params: Optional[list[Any]] = None) -> CursorLike:
        """Execute a SQL query with optional parameters and return cursor.

        On the one connection of a plain ``":memory:"`` adapter, a statement that returns
        rows has every row read before its turn is given back, and returns them in a
        ``_ReadRows``; everywhere else the ``sqlite3.Cursor`` is returned unread.

        Automatically logs the query if query_logger is enabled.
        """
        shared = self._shared  # not None: the one ":memory:" connection, which waits for its turn
        conn = self._conn() if shared is None else shared.take(self._local.broken)
        try:
            cursor = conn.cursor()
            _logging = query_logger.enabled
            if _logging:
                start = time.perf_counter()
            error_msg = None
            try:
                if params is not None:
                    if isinstance(params, list):
                        params = tuple(params)
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                if shared is not None and cursor.description is not None:
                    return _ReadRows(cursor)  # a read that fails is a failed statement
            except BaseException as exc:  # an interrupt counts as a failure
                error_msg = str(exc) or type(exc).__name__
                if not _read_hit_missing_table(query, exc):
                    self._end_failed_implicit_transaction(conn)
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
                        rows_affected=cursor.rowcount if cursor.rowcount >= 0 else None,
                        error=error_msg,
                    )
        finally:
            if shared is not None:
                shared.give(self._txn_depth)
        return cursor

    def executemany(self, query: str, param_list: Optional[list[Any]]) -> sqlite3.Cursor:
        """Execute a param query multiple times with different parameter sets.

        Inside an explicit transaction the rows are part of it (a rollback
        removes them); outside one they are committed, and a failure stores
        none of the rows.
        """
        shared = self._shared
        conn = self._conn() if shared is None else shared.take(self._local.broken)
        try:
            cursor = conn.cursor()
            cursor.executemany(query, param_list or [])
            if self._txn_depth == 0:
                conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        finally:
            if shared is not None:
                shared.give(self._txn_depth)
        return cursor

    def executescript(self, script: str) -> sqlite3.Cursor:
        """Run a script of statements and commit.

        Raises ``QueryError`` inside an explicit transaction: SQLite's driver commits
        the open transaction before it runs a script, which would end it unseen.
        """
        if self._txn_depth > 0:
            if self._local.broken is not None:
                raise _broken(self._local.broken)
            raise QueryError(
                "executescript cannot run inside a transaction: "
                "SQLite commits the open transaction before it runs a script",
                sql=script,
            )
        shared = self._shared
        conn = self._conn() if shared is None else shared.take(self._local.broken)
        try:
            cursor = conn.cursor()
            cursor.executescript(script)
            conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        finally:
            if shared is not None:
                shared.give(self._txn_depth)
        return cursor

    def commit(self) -> None:
        """Commit the current transaction.

        Outside an explicit transaction a commit that fails (a deferred
        constraint, a lock) rolls the transaction back before the error is raised.
        """
        shared = self._shared
        conn = self._conn() if shared is None else shared.take(self._local.broken)
        try:
            conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        finally:
            if shared is not None:
                shared.give(self._txn_depth)

    def auto_commit(self) -> None:
        """Commit only if NOT inside an explicit transaction.

        Use this instead of commit() in operations like insert/update/delete
        so that explicit transactions work correctly with model.save().
        """
        if self._txn_depth == 0:
            self.commit()

    @property
    def in_transaction(self) -> bool:
        """Return True if currently inside an explicit transaction."""
        return self._txn_depth > 0

    def begin_transaction(self) -> None:
        """Begin an explicit transaction (increments depth counter).

        Uses SAVEPOINTs for nested transactions to allow proper inner rollback.
        """
        shared = self._shared
        conn = self._conn() if shared is None else shared.take(self._local.broken)
        try:
            depth = self._txn_depth  # nested: a savepoint
            conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT sp_{depth}")
            self._txn_depth = depth + 1
        finally:
            if shared is not None:
                shared.give(self._txn_depth)

    def end_transaction(self, *, commit: bool = True) -> None:
        """End this thread's innermost explicit transaction (decrements depth counter).

        Args:
            commit: If True, commit on outermost transaction. If False, rollback.

        Raises:
            TransactionStateError: the end of a nested transaction failed in SQLite, here
                or earlier in this transaction (``__cause__`` is SQLite's error). The
                transaction ends rolled back whatever ``commit`` says; a nested end after
                the failure sends nothing. An interrupt during a nested end leaves as it
                is, and the transaction ends rolled back the same way.
        """
        local = self._local
        depth = local.txn_depth
        if depth <= 0:
            return  # No active transaction
        local.txn_depth = depth = depth - 1
        failed = local.broken
        shared = self._shared
        if depth == 0:
            local.broken = None
            conn = local.conn if shared is None else shared.take()
            try:
                if commit and failed is None:
                    try:
                        conn.commit()
                    except BaseException:
                        # depth is 0 now: the failed commit must not leave the transaction open
                        self._end_failed_implicit_transaction(conn)
                        raise
                else:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
            finally:
                if shared is not None:
                    shared.give(0)
        elif failed is None:
            try:
                conn = local.conn if shared is None else shared.take()
                try:
                    if not commit:
                        conn.execute(f"ROLLBACK TO SAVEPOINT sp_{depth}")
                    conn.execute(f"RELEASE SAVEPOINT sp_{depth}")
                finally:
                    if shared is not None:
                        shared.give(depth)
            except BaseException as exc:
                # a RELEASE after a failed ROLLBACK TO would keep the rows it was to undo
                local.broken = failed = ("committed" if commit else "rolled back", exc)
                if not isinstance(exc, Exception):
                    raise
        if failed is not None:
            raise _broken(failed)

    def __enter__(self):
        """Enter context manager; connect if not connected"""
        if self._local.conn is None:
            self.connect()
        else:
            self._conn()  # in a transaction whose nested end failed: raises
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        """Exit context manager: commit the block's writes, or roll them back while an
        exception leaves; inside this thread's explicit transaction, the transaction goes on."""
        if not self._connected or self._txn_depth > 0:
            return
        if exception_type is None:
            self.commit()
            return
        shared = self._shared
        conn = self._conn() if shared is None else shared.take()
        try:
            self._end_failed_implicit_transaction(conn)  # rolls back; an error of its own is not raised
        finally:
            if shared is not None:
                shared.give(self._txn_depth)

    ### factories

    @classmethod
    def in_memory(cls, shared: bool = True, name: Optional[str] = None, *, timeout_ms: int = 5000) -> Self:
        """Connects to an in memory db with some pragmas applied; ``timeout_ms`` as for the constructor."""
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
        return cls(uri, pragmas=pragmas, timeout_ms=timeout_ms)

    _PRAGMA_KEYS = [
        "foreign_keys", "busy_timeout", "journal_mode", "synchronous",
        "cache_size", "wal_autocheckpoint", "mmap_size", "temp_store",
    ]

    def pragma_report(self) -> dict[str, Any]:
        """Return current PRAGMA values for key settings.

        Queries the active connection for each tracked PRAGMA and returns
        a dict of ``{pragma_name: value}``. Useful for downstream libraries
        to verify that expected PRAGMAs are in effect.

        Returns:
            dict[str, Any]: Mapping of PRAGMA names to their current values.
        """
        report: dict[str, Any] = {}
        for key in self._PRAGMA_KEYS:
            cursor = self.execute(f"PRAGMA {key};")
            row = cursor.fetchone()
            report[key] = row[0] if row else None
        return report

    @classmethod
    def on_disk(cls, path: str = "sqler.db", *, timeout_ms: int = 5000) -> Self:
        """Connects (creates if not exist) a db on disk with some pragmas applied; ``timeout_ms`` as for the constructor."""
        pragmas = [
            "PRAGMA foreign_keys = ON",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA cache_size = -64000",
            "PRAGMA wal_autocheckpoint = 1000",
            "PRAGMA mmap_size = 268435456",
            "PRAGMA temp_store = MEMORY",
        ]
        return cls(path, pragmas=pragmas, timeout_ms=timeout_ms)
