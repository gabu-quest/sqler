import sqlite3
import threading
import time
import uuid
import weakref
from typing import Any, Optional, Self

from sqler.logging import query_logger

from ..exceptions import NotConnectedError, QueryError, unique_violation_from
from .abstract import AdapterABC


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


class SQLiteAdapter(AdapterABC):
    """Synchronous SQLite adapter with WAL and thread-local connections.

    Features:
        - Thread-local connections for safe concurrent access; a thread's
          connection closes when the thread ends
        - WAL mode for improved concurrency
        - Configurable query timeout via busy_timeout pragma
        - Optional slow query logging
        - Transaction-aware auto-commit (respects explicit transactions)
        - A statement or commit that fails outside an explicit transaction
          rolls back the transaction Python's ``sqlite3`` opened for it, so the
          thread's connection keeps no lock

    Args:
        path: Path to the database file, or ":memory:" for in-memory.
        pragmas: Optional list of PRAGMA statements to execute on connect.
        timeout_ms: Query timeout in milliseconds (default 5000).
    """

    def __init__(
        self,
        path: str = "sqler.db",
        pragmas: Optional[list[str]] = None,
        timeout_ms: int = 5000,
    ):
        """Initialize the adapter.

        Args:
            path: Path to the SQLite database file.
            pragmas: Optional list of SQL PRAGMA statements to apply on connection.
            timeout_ms: Busy timeout in milliseconds (how long to wait for locks).
        """
        self.path = path
        self.pragmas = pragmas or []
        self.timeout_ms = timeout_ms
        self._local = threading.local()
        self._conns_lock = threading.RLock()
        self._conns: weakref.WeakSet[_ThreadConnection] = weakref.WeakSet()
        self._connected: bool = False
        self._single_conn: Optional[sqlite3.Connection] = None
        # SQLite drops a shared-cache in-memory database when its last connection closes,
        # and a thread's connection closes when the thread ends. This connection runs no
        # statement; it keeps the database alive from connect() to close().
        self._anchor: Optional[sqlite3.Connection] = None
        self._memory_singleton = path == ":memory:"
        # Depth of explicit transactions (see ``_txn_depth``); one number when
        # every thread shares the single ":memory:" connection.
        self._shared_txn_depth: int = 0

    @property
    def _txn_depth(self) -> int:
        """Depth of this thread's explicit transactions (0 = none).

        Each thread has its own connection, so each thread has its own depth.
        A plain ``":memory:"`` adapter has one connection for every thread; a
        transaction on it is everyone's, so the depth is shared too.
        """
        if self._memory_singleton:
            return self._shared_txn_depth
        return getattr(self._local, "txn_depth", 0)

    @_txn_depth.setter
    def _txn_depth(self, value: int) -> None:
        if self._memory_singleton:
            self._shared_txn_depth = value
        else:
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
        # Ensure a connection for this thread
        self._connected = True
        if self._anchor is None and _names_memory_database(self.path):
            with self._conns_lock:
                if self._anchor is None:
                    self._anchor = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        _ = self._conn()

    def _conn(self) -> sqlite3.Connection:
        if not self._connected:
            raise NotConnectedError("Database not connected, call connect() first")
        if self._memory_singleton:
            if self._single_conn is None:
                self._single_conn = sqlite3.connect(self.path, uri=True, check_same_thread=False)
                self._single_conn.row_factory = sqlite3.Row
                cur = self._single_conn.cursor()
                for pragma in self.pragmas:
                    cur.execute(pragma)
                self._single_conn.commit()
            return self._single_conn
        local = self._local  # read once: close() on another thread replaces it
        conn: Optional[sqlite3.Connection] = getattr(local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for pragma in self.pragmas:
            cur.execute(pragma)
        conn.commit()
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
        if self._single_conn is not None:
            try:
                self._single_conn.close()
            except sqlite3.Error:
                pass  # Connection may already be closed
            self._single_conn = None
        with self._conns_lock:
            anchor, self._anchor = self._anchor, None
        if anchor is not None:
            try:
                anchor.close()
            except sqlite3.Error:
                pass
        # A fresh thread-local object: a thread that outlives close() holds nothing of the
        # old one (connection, transaction depth) and opens a new connection on connect().
        self._local = threading.local()

    def execute(self, query: str, params: Optional[list[Any]] = None) -> sqlite3.Cursor:
        """Execute a SQL query with optional parameters and return cursor.

        Automatically logs the query if query_logger is enabled.
        """
        conn = self._conn()  # Raises NotConnectedError if not connected
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
        return cursor

    def executemany(self, query: str, param_list: Optional[list[Any]]) -> sqlite3.Cursor:
        """Execute a param query multiple times with different parameter sets.

        Inside an explicit transaction the rows are part of it (a rollback
        removes them); outside one they are committed, and a failure stores
        none of the rows.
        """
        conn = self._conn()  # Raises NotConnectedError if not connected
        cursor = conn.cursor()
        try:
            cursor.executemany(query, param_list or [])
            if self._txn_depth == 0:
                conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        return cursor

    def executescript(self, script: str) -> sqlite3.Cursor:
        """Run a script of statements and commit.

        Raises ``QueryError`` inside an explicit transaction: SQLite's driver commits
        the open transaction before it runs a script, which would end it unseen.
        """
        if self._txn_depth > 0:
            raise QueryError(
                "executescript cannot run inside a transaction: "
                "SQLite commits the open transaction before it runs a script",
                sql=script,
            )
        conn = self._conn()  # Raises NotConnectedError if not connected
        cursor = conn.cursor()
        try:
            cursor.executescript(script)
            conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        return cursor

    def commit(self) -> None:
        """Commit the current transaction.

        Outside an explicit transaction a commit that fails (a deferred
        constraint, a lock) rolls the transaction back before the error is raised.
        """
        conn = self._conn()  # Raises NotConnectedError if not connected
        try:
            conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise

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
        if self._txn_depth == 0:
            self._conn().execute("BEGIN IMMEDIATE")
        else:
            # Nested: create savepoint
            self._conn().execute(f"SAVEPOINT sp_{self._txn_depth}")
        self._txn_depth += 1

    def end_transaction(self, *, commit: bool = True) -> None:
        """End an explicit transaction (decrements depth counter).

        Args:
            commit: If True, commit on outermost transaction. If False, rollback.
        """
        if self._txn_depth <= 0:
            return  # No active transaction
        self._txn_depth -= 1
        conn = self._conn()

        if self._txn_depth == 0:
            # Outermost: commit or rollback entire transaction
            if commit:
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
        else:
            # Nested: release or rollback savepoint
            sp = f"sp_{self._txn_depth}"
            try:
                if commit:
                    conn.execute(f"RELEASE SAVEPOINT {sp}")
                else:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    conn.execute(f"RELEASE SAVEPOINT {sp}")  # Clean up after rollback
            except sqlite3.Error:
                pass

    def __enter__(self):
        """Enter context manager; connect if not connected"""
        if getattr(self._local, "conn", None) is None:
            self.connect()
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        """Exit context manager; commit or rollback depending on exceptions"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        if exception_type is None:
            conn.commit()
        else:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass  # Rollback may fail if connection is broken

    ### factories

    @classmethod
    def in_memory(cls, shared: bool = True, name: Optional[str] = None) -> Self:
        """Connects to an in memory db with some pragmas applied"""
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
    def on_disk(cls, path: str = "sqler.db") -> Self:
        """Connects (creates if not exist) a db on disk with some pragmas applied"""
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


class _LockedCursor:
    # Deprecated: no longer used with thread-local connections (kept for history)
    pass
