import sqlite3
import threading
from typing import Any, Optional, Self

from .abstract import AdapterABC, NotConnectedError


class SQLiteAdapter(AdapterABC):
    """Synchronous SQLite adapter with WAL and thread-local connections."""

    def __init__(self, path: str = "sqler.db", pragmas: Optional[list[str]] = None):
        """pragmas are an optional list of sql statements to apply on connection"""

        self.path = path
        self.pragmas = pragmas or []
        self._local = threading.local()
        self._conns_lock = threading.RLock()
        self._conns: set[sqlite3.Connection] = set()
        self._connected: bool = False
        self._single_conn: Optional[sqlite3.Connection] = None
        self._memory_singleton = path == ":memory:"

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
                except Exception:
                    pass
            self._conns.clear()
        if self._single_conn is not None:
            try:
                self._single_conn.close()
            except Exception:
                pass
            self._single_conn = None
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")

    def execute(self, query: str, params: Optional[list[Any]] = None) -> sqlite3.Cursor:
        """Execute a SQL query with optional parameters and return cursor"""
        conn = self._conn()
        if conn is None:
            raise NotConnectedError("Database not connected, call connect() first")
        cursor = conn.cursor()
        if params is not None:
            if isinstance(params, list):
                params = tuple(params)
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor

    def executemany(self, query: str, param_list: Optional[list[Any]]) -> sqlite3.Cursor:
        """Execute a param query multiple times with different parameter sets"""
        conn = self._conn()
        if conn is None:
            raise NotConnectedError("Database not connected, call connect() first")
        cursor = conn.cursor()
        cursor.executemany(query, param_list or [])
        conn.commit()
        return cursor

    def executescript(self, script: str) -> sqlite3.Cursor:
        """Execute multiple statements from a script in a single action"""
        conn = self._conn()
        if conn is None:
            raise NotConnectedError("Database not connected, call connect() first")
        cursor = conn.cursor()
        cursor.executescript(script)
        conn.commit()
        return cursor

    def commit(self) -> None:
        """Commit the current transaction."""
        conn = self._conn()
        if conn is None:
            raise NotConnectedError("Database not connected, call connect() first")
        conn.commit()

    def __enter__(self):
        """Enter context manager; connect if not connected"""
        if getattr(self._local, "conn", None) is None:
            self.connect()
        return self

    def __exit__(self, exception_type, exception_value, exception_tracebak):
        """Exit context manager; commit or rollback depending on exceptions"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        if exception_type is None:
            conn.commit()
        else:
            try:
                conn.rollback()
            except Exception:
                pass

    ### factories

    @classmethod
    def in_memory(cls, shared: bool = True) -> Self:
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
            uri = "file::memory:?cache=shared"
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
