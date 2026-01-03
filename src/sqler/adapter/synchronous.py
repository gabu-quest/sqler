import sqlite3
import threading
import time
import uuid
from typing import Any, Optional, Self

from sqler.logging import query_logger

from ..exceptions import NotConnectedError
from .abstract import AdapterABC


class SQLiteAdapter(AdapterABC):
    """Synchronous SQLite adapter with WAL and thread-local connections.

    Features:
        - Thread-local connections for safe concurrent access
        - WAL mode for improved concurrency
        - Configurable query timeout via busy_timeout pragma
        - Optional slow query logging
        - Transaction-aware auto-commit (respects explicit transactions)

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
        self._conns: set[sqlite3.Connection] = set()
        self._connected: bool = False
        self._single_conn: Optional[sqlite3.Connection] = None
        self._memory_singleton = path == ":memory:"
        # Track transaction depth per-thread for nested transaction support
        self._txn_depth: int = 0

    def connect(self) -> None:
        # Ensure a connection for this thread
        self._connected = True
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
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for pragma in self.pragmas:
            cur.execute(pragma)
        conn.commit()
        self._local.conn = conn
        with self._conns_lock:
            self._conns.add(conn)
        return conn

    def close(self) -> None:
        self._connected = False
        with self._conns_lock:
            for c in list(self._conns):
                try:
                    c.close()
                except sqlite3.Error:
                    pass  # Connection may already be closed
            self._conns.clear()
        if self._single_conn is not None:
            try:
                self._single_conn.close()
            except sqlite3.Error:
                pass  # Connection may already be closed
            self._single_conn = None
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")

    def execute(self, query: str, params: Optional[list[Any]] = None) -> sqlite3.Cursor:
        """Execute a SQL query with optional parameters and return cursor.

        Automatically logs the query if query_logger is enabled.
        """
        conn = self._conn()  # Raises NotConnectedError if not connected
        cursor = conn.cursor()
        start = time.perf_counter()
        error_msg = None
        try:
            if params is not None:
                if isinstance(params, list):
                    params = tuple(params)
                cursor.execute(query, params)
            else:
                cursor.execute(query)
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
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
        """Execute a param query multiple times with different parameter sets"""
        conn = self._conn()  # Raises NotConnectedError if not connected
        cursor = conn.cursor()
        cursor.executemany(query, param_list or [])
        conn.commit()
        return cursor

    def executescript(self, script: str) -> sqlite3.Cursor:
        """Execute multiple statements from a script in a single action"""
        conn = self._conn()  # Raises NotConnectedError if not connected
        cursor = conn.cursor()
        cursor.executescript(script)
        conn.commit()
        return cursor

    def commit(self) -> None:
        """Commit the current transaction."""
        conn = self._conn()  # Raises NotConnectedError if not connected
        conn.commit()

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
        """Begin an explicit transaction (increments depth counter)."""
        if self._txn_depth == 0:
            self._conn().execute("BEGIN IMMEDIATE")
        self._txn_depth += 1

    def end_transaction(self, *, commit: bool = True) -> None:
        """End an explicit transaction (decrements depth counter).

        Args:
            commit: If True, commit on outermost transaction. If False, rollback.
        """
        if self._txn_depth <= 0:
            return  # No active transaction
        self._txn_depth -= 1
        if self._txn_depth == 0:
            conn = self._conn()
            if commit:
                conn.commit()
            else:
                try:
                    conn.rollback()
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
