"""Connection pooling for SQLite databases.

SQLite with WAL mode supports concurrent readers with a single writer.
This pool manages read connections efficiently while ensuring write
operations are properly serialized.

Usage::

    from sqler.pool import ConnectionPool

    # Create a pool with max 10 connections
    pool = ConnectionPool("myapp.db", max_connections=10)

    # Get a connection from the pool
    with pool.connection() as conn:
        cursor = conn.execute("SELECT * FROM users")
        rows = cursor.fetchall()

    # Connection is automatically returned to pool

For SQLerDB integration::

    from sqler.pool import PooledSQLerDB

    db = PooledSQLerDB.on_disk("myapp.db", max_connections=10)

    class User(SQLerModel):
        name: str

    User.set_db(db)
    users = User.query().all()  # Uses pooled connection
"""

import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Optional

from sqler.exceptions import ConnectionPoolExhaustedError


@dataclass
class PoolStats:
    """Statistics about connection pool usage."""

    total_connections: int
    available_connections: int
    in_use_connections: int
    max_connections: int
    total_checkouts: int
    total_timeouts: int
    avg_wait_time_ms: float
    max_wait_time_ms: float
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "total_connections": self.total_connections,
            "available_connections": self.available_connections,
            "in_use_connections": self.in_use_connections,
            "max_connections": self.max_connections,
            "total_checkouts": self.total_checkouts,
            "total_timeouts": self.total_timeouts,
            "avg_wait_time_ms": self.avg_wait_time_ms,
            "max_wait_time_ms": self.max_wait_time_ms,
            "created_at": self.created_at.isoformat(),
        }


class ConnectionPool:
    """Thread-safe connection pool for SQLite databases.

    With WAL mode, SQLite supports:
    - Multiple concurrent readers
    - Single writer (writes are serialized)
    - Readers don't block writers and vice versa

    This pool manages read connections efficiently. For write operations,
    a dedicated writer connection is used to ensure proper serialization.

    Args:
        path: Path to the SQLite database file.
        max_connections: Maximum number of connections in the pool.
        timeout_ms: How long to wait for a connection (milliseconds).
        pragmas: Optional list of PRAGMA statements for each connection.
        min_connections: Minimum connections to keep alive (pre-warm).

    Usage::

        pool = ConnectionPool("myapp.db", max_connections=10)

        # Get a read connection
        with pool.connection() as conn:
            cursor = conn.execute("SELECT * FROM users")
            rows = cursor.fetchall()

        # Get the writer connection (for inserts/updates/deletes)
        with pool.writer() as conn:
            conn.execute("INSERT INTO users (data) VALUES (?)", [data])
            conn.commit()

        # Check pool stats
        stats = pool.stats()
        print(f"Connections in use: {stats.in_use_connections}")

        # Close all connections
        pool.close()
    """

    def __init__(
        self,
        path: str,
        max_connections: int = 10,
        timeout_ms: int = 5000,
        pragmas: Optional[list[str]] = None,
        min_connections: int = 1,
    ):
        self.path = path
        self.max_connections = max(1, max_connections)
        self.timeout_ms = timeout_ms
        self.min_connections = min(min_connections, self.max_connections)

        # Default pragmas for WAL mode
        self.pragmas = pragmas or [
            "PRAGMA foreign_keys = ON",
            "PRAGMA busy_timeout = 5000",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA cache_size = -64000",
            "PRAGMA wal_autocheckpoint = 1000",
            "PRAGMA mmap_size = 268435456",
            "PRAGMA temp_store = MEMORY",
        ]

        # Pool state
        self._lock = threading.RLock()
        self._pool: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=self.max_connections)
        self._all_connections: list[sqlite3.Connection] = []
        self._in_use: set[sqlite3.Connection] = set()

        # Dedicated writer connection
        self._writer_conn: Optional[sqlite3.Connection] = None
        self._writer_lock = threading.RLock()

        # Statistics
        self._created_at = datetime.now()
        self._total_checkouts = 0
        self._total_timeouts = 0
        self._total_wait_time_ms = 0.0
        self._max_wait_time_ms = 0.0

        # Pre-warm minimum connections
        self._prewarm()

    def _prewarm(self) -> None:
        """Create minimum number of connections."""
        for _ in range(self.min_connections):
            try:
                conn = self._create_connection()
                self._pool.put_nowait(conn)
            except Exception:
                break

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new database connection."""
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Apply pragmas
        cursor = conn.cursor()
        for pragma in self.pragmas:
            cursor.execute(pragma)
        conn.commit()

        with self._lock:
            self._all_connections.append(conn)

        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Get a connection from the pool, creating if necessary."""
        start = time.perf_counter()

        try:
            # Try to get an existing connection
            conn = self._pool.get_nowait()
        except queue.Empty:
            # No connection available, try to create one
            with self._lock:
                if len(self._all_connections) < self.max_connections:
                    conn = self._create_connection()
                else:
                    # Pool is at max, wait for a connection
                    conn = None

            if conn is None:
                try:
                    conn = self._pool.get(timeout=self.timeout_ms / 1000.0)
                except queue.Empty:
                    with self._lock:
                        self._total_timeouts += 1
                    raise ConnectionPoolExhaustedError(
                        f"Connection pool exhausted (max={self.max_connections}, "
                        f"timeout={self.timeout_ms}ms)"
                    )

        # Track statistics
        wait_time_ms = (time.perf_counter() - start) * 1000
        with self._lock:
            self._total_checkouts += 1
            self._total_wait_time_ms += wait_time_ms
            if wait_time_ms > self._max_wait_time_ms:
                self._max_wait_time_ms = wait_time_ms
            self._in_use.add(conn)

        return conn

    def _return_connection(self, conn: sqlite3.Connection) -> None:
        """Return a connection to the pool."""
        with self._lock:
            self._in_use.discard(conn)

        try:
            # Rollback any uncommitted transaction
            conn.rollback()
            self._pool.put_nowait(conn)
        except queue.Full:
            # Pool is full, close the connection
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                if conn in self._all_connections:
                    self._all_connections.remove(conn)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Get a connection from the pool as a context manager.

        The connection is automatically returned to the pool when the
        context exits.

        Usage::

            with pool.connection() as conn:
                cursor = conn.execute("SELECT * FROM users")
                rows = cursor.fetchall()
        """
        conn = self._get_connection()
        try:
            yield conn
        finally:
            self._return_connection(conn)

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        """Get the dedicated writer connection.

        Only one writer can be active at a time. This ensures proper
        serialization of write operations.

        Usage::

            with pool.writer() as conn:
                conn.execute("INSERT INTO users (data) VALUES (?)", [data])
                conn.commit()
        """
        with self._writer_lock:
            if self._writer_conn is None:
                self._writer_conn = self._create_connection()

            try:
                yield self._writer_conn
            except Exception:
                # Rollback on error
                try:
                    self._writer_conn.rollback()
                except Exception:
                    pass
                raise

    def stats(self) -> PoolStats:
        """Get pool statistics.

        Returns:
            PoolStats with current pool state.
        """
        with self._lock:
            total = len(self._all_connections)
            in_use = len(self._in_use)
            available = self._pool.qsize()

            avg_wait = (
                self._total_wait_time_ms / self._total_checkouts
                if self._total_checkouts > 0
                else 0.0
            )

            return PoolStats(
                total_connections=total,
                available_connections=available,
                in_use_connections=in_use,
                max_connections=self.max_connections,
                total_checkouts=self._total_checkouts,
                total_timeouts=self._total_timeouts,
                avg_wait_time_ms=avg_wait,
                max_wait_time_ms=self._max_wait_time_ms,
                created_at=self._created_at,
            )

    def close(self) -> None:
        """Close all connections in the pool."""
        with self._lock:
            # Close pooled connections
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except (queue.Empty, Exception):
                    pass

            # Close in-use connections
            for conn in list(self._in_use):
                try:
                    conn.close()
                except Exception:
                    pass

            # Close writer connection
            with self._writer_lock:
                if self._writer_conn is not None:
                    try:
                        self._writer_conn.close()
                    except Exception:
                        pass
                    self._writer_conn = None

            self._all_connections.clear()
            self._in_use.clear()

    def __enter__(self):
        """Enter context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager, closing all connections."""
        self.close()
        return False


class PooledSQLiteAdapter:
    """SQLite adapter that uses a connection pool.

    Drop-in replacement for SQLiteAdapter that manages connections
    through a pool for better concurrency.
    """

    def __init__(
        self,
        path: str,
        max_connections: int = 10,
        timeout_ms: int = 5000,
        pragmas: Optional[list[str]] = None,
    ):
        self.path = path
        self._pool = ConnectionPool(
            path,
            max_connections=max_connections,
            timeout_ms=timeout_ms,
            pragmas=pragmas,
        )
        self._connected = False
        self._txn_depth = 0
        self._local = threading.local()

    def connect(self) -> None:
        """Mark adapter as connected."""
        self._connected = True

    def close(self) -> None:
        """Close the connection pool."""
        self._connected = False
        self._pool.close()

    def _get_conn(self) -> sqlite3.Connection:
        """Get connection for current operation."""
        # If in transaction, reuse the same connection
        if hasattr(self._local, "txn_conn") and self._local.txn_conn is not None:
            return self._local.txn_conn

        # Otherwise get from pool (but we need to manage lifecycle)
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._pool._get_connection()
        return self._local.conn

    def _release_conn(self) -> None:
        """Release connection back to pool."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._pool._return_connection(self._local.conn)
            self._local.conn = None

    def execute(self, query: str, params: Optional[list[Any]] = None) -> sqlite3.Cursor:
        """Execute a SQL query."""
        conn = self._get_conn()
        cursor = conn.cursor()
        if params is not None:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor

    def executemany(self, query: str, param_list: Optional[list[Any]]) -> sqlite3.Cursor:
        """Execute a query multiple times."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.executemany(query, param_list or [])
        return cursor

    def commit(self) -> None:
        """Commit current transaction."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.commit()

    def auto_commit(self) -> None:
        """Commit if not in explicit transaction."""
        if self._txn_depth == 0:
            self.commit()
            self._release_conn()

    @property
    def in_transaction(self) -> bool:
        """Return True if in explicit transaction."""
        return self._txn_depth > 0

    def begin_transaction(self) -> None:
        """Begin explicit transaction."""
        if self._txn_depth == 0:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            self._local.txn_conn = conn
        self._txn_depth += 1

    def end_transaction(self, *, commit: bool = True) -> None:
        """End explicit transaction."""
        if self._txn_depth <= 0:
            return
        self._txn_depth -= 1
        if self._txn_depth == 0:
            if hasattr(self._local, "txn_conn") and self._local.txn_conn is not None:
                if commit:
                    self._local.txn_conn.commit()
                else:
                    self._local.txn_conn.rollback()
                self._pool._return_connection(self._local.txn_conn)
                self._local.txn_conn = None

    def pool_stats(self) -> PoolStats:
        """Get connection pool statistics."""
        return self._pool.stats()

    @classmethod
    def on_disk(
        cls,
        path: str = "sqler.db",
        max_connections: int = 10,
        timeout_ms: int = 5000,
    ) -> "PooledSQLiteAdapter":
        """Create a pooled adapter for on-disk database."""
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
        return cls(path, max_connections, timeout_ms, pragmas)


class PooledSQLerDB:
    """SQLerDB with connection pooling.

    Drop-in replacement for SQLerDB that uses a connection pool for
    better read concurrency with WAL mode.

    Usage::

        db = PooledSQLerDB.on_disk("myapp.db", max_connections=10)

        class User(SQLerModel):
            name: str

        User.set_db(db)

        # Queries use pooled connections
        users = User.query().all()

        # Check pool stats
        print(db.pool_stats())
    """

    def __init__(self, adapter: PooledSQLiteAdapter):
        self.adapter = adapter
        self.adapter.connect()
        self._versioned_tables: set[str] = set()

    @classmethod
    def on_disk(
        cls,
        path: str = "sqler.db",
        max_connections: int = 10,
        timeout_ms: int = 5000,
    ) -> "PooledSQLerDB":
        """Create a pooled database for on-disk file.

        Args:
            path: Path to the SQLite database file.
            max_connections: Maximum pooled connections.
            timeout_ms: Timeout waiting for connection.

        Returns:
            PooledSQLerDB: Connected pooled database.
        """
        adapter = PooledSQLiteAdapter.on_disk(path, max_connections, timeout_ms)
        return cls(adapter)

    def pool_stats(self) -> PoolStats:
        """Get connection pool statistics."""
        return self.adapter.pool_stats()

    def close(self) -> None:
        """Close the database and all pooled connections."""
        self.adapter.close()

    # Delegate all SQLerDB methods to work with pooled adapter
    # These are simplified versions - full implementation would
    # mirror SQLerDB exactly

    def _ensure_table(self, table: str) -> None:
        """Create table if it doesn't exist."""
        from sqler.utils import validate_table_name

        table = validate_table_name(table)
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            data JSON NOT NULL
        );
        """
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        """Insert a document."""
        import json

        self._ensure_table(table)
        payload = json.dumps(doc)
        cursor = self.adapter.execute(f"INSERT INTO {table} (data) VALUES (json(?));", [payload])
        self.adapter.auto_commit()
        return cursor.lastrowid  # type: ignore

    def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        """Find a document by ID."""
        import json

        self._ensure_table(table)
        cur = self.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [_id])
        row = cur.fetchone()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        return obj

    def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        """Insert or update a document."""
        import json

        self._ensure_table(table)
        payload = json.dumps(doc)
        if _id is None:
            return self.insert_document(table, doc)
        self.adapter.execute(f"UPDATE {table} SET data = json(?) WHERE _id = ?;", [payload, _id])
        self.adapter.auto_commit()
        return _id

    def delete_document(self, table: str, _id: int) -> None:
        """Delete a document."""
        self._ensure_table(table)
        self.adapter.execute(f"DELETE FROM {table} WHERE _id = ?;", [_id])
        self.adapter.auto_commit()

    def query(self, table: str):
        """Return a SQLerQuery bound to this adapter."""
        from sqler.query import SQLerQuery

        self._ensure_table(table)
        return SQLerQuery(table=table, adapter=self.adapter)

    def transaction(self):
        """Return a transaction context manager."""
        from sqler.db.sqler_db import Transaction

        return Transaction(self.adapter)

    def create_index(
        self,
        table: str,
        field: str,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Create an index."""
        self._ensure_table(table)
        idx_name = name or f"idx_{table}_{field.replace('.', '_')}"
        unique_sql = "UNIQUE" if unique else ""
        expr = f"json_extract(data, '$.{field}')" if not field.startswith("_") else field
        where_sql = f"WHERE {where}" if where else ""
        ddl = f"CREATE {unique_sql} INDEX IF NOT EXISTS {idx_name} ON {table} ({expr}) {where_sql};"
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def __enter__(self):
        """Enter context manager."""
        self.adapter.begin_transaction()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager."""
        self.adapter.end_transaction(commit=(exc_type is None))
        return False
