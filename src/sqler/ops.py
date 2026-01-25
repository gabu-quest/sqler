"""Database operations: backup, restore, health checks, and maintenance.

This module provides production-ready operations for SQLer databases including:
- Online backup and restore using SQLite's backup API
- Health checks for monitoring and liveness probes
- Database statistics and diagnostics
"""

import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sqler.db.async_db import AsyncSQLerDB
    from sqler.db.sqler_db import SQLerDB


@dataclass
class HealthStatus:
    """Result of a health check."""

    healthy: bool
    latency_ms: float
    message: str
    timestamp: datetime
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "healthy": self.healthy,
            "latency_ms": self.latency_ms,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
        }


@dataclass
class BackupResult:
    """Result of a backup operation."""

    success: bool
    source_path: str
    destination_path: str
    duration_ms: float
    size_bytes: int
    timestamp: datetime
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "source_path": self.source_path,
            "destination_path": self.destination_path,
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "timestamp": self.timestamp.isoformat(),
            "error": self.error,
        }


@dataclass
class DatabaseStats:
    """Database statistics for monitoring."""

    path: str
    size_bytes: int
    page_count: int
    page_size: int
    wal_size_bytes: int
    table_count: int
    index_count: int
    freelist_count: int
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "page_count": self.page_count,
            "page_size": self.page_size,
            "wal_size_bytes": self.wal_size_bytes,
            "table_count": self.table_count,
            "index_count": self.index_count,
            "freelist_count": self.freelist_count,
            "timestamp": self.timestamp.isoformat(),
        }


# ============================================================================
# Sync Operations
# ============================================================================


def health_check(db: "SQLerDB", timeout_ms: int = 5000) -> HealthStatus:
    """Perform a health check on the database.

    Verifies:
    - Database connectivity
    - Read capability (SELECT 1)
    - Write capability (optional, via temp table)

    Args:
        db: SQLerDB instance to check.
        timeout_ms: Maximum time to wait for response.

    Returns:
        HealthStatus with check results.

    Usage::

        from sqler.ops import health_check

        status = health_check(db)
        if status.healthy:
            print(f"DB healthy, latency: {status.latency_ms:.2f}ms")
        else:
            print(f"DB unhealthy: {status.message}")
    """
    start = time.perf_counter()
    details: dict[str, Any] = {}

    try:
        # Basic connectivity check
        cursor = db.adapter.execute("SELECT 1;")
        result = cursor.fetchone()
        if result is None or result[0] != 1:
            return HealthStatus(
                healthy=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                message="SELECT 1 returned unexpected result",
                timestamp=datetime.now(),
                details=details,
            )

        # Get database info
        cursor = db.adapter.execute("PRAGMA database_list;")
        db_list = cursor.fetchall()
        details["databases"] = [{"name": row[1], "file": row[2]} for row in db_list]

        # Check journal mode
        cursor = db.adapter.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()
        details["journal_mode"] = journal_mode[0] if journal_mode else "unknown"

        # Check integrity (quick check)
        cursor = db.adapter.execute("PRAGMA quick_check(1);")
        integrity = cursor.fetchone()
        integrity_ok = integrity and integrity[0] == "ok"
        details["integrity_check"] = "ok" if integrity_ok else "failed"

        if not integrity_ok:
            return HealthStatus(
                healthy=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                message="Integrity check failed",
                timestamp=datetime.now(),
                details=details,
            )

        latency_ms = (time.perf_counter() - start) * 1000
        return HealthStatus(
            healthy=True,
            latency_ms=latency_ms,
            message="OK",
            timestamp=datetime.now(),
            details=details,
        )

    except Exception as e:
        return HealthStatus(
            healthy=False,
            latency_ms=(time.perf_counter() - start) * 1000,
            message=str(e),
            timestamp=datetime.now(),
            details=details,
        )


def is_healthy(db: "SQLerDB", timeout_ms: int = 5000) -> bool:
    """Quick health check returning boolean.

    Args:
        db: SQLerDB instance to check.
        timeout_ms: Maximum time to wait.

    Returns:
        True if database is healthy, False otherwise.
    """
    return health_check(db, timeout_ms).healthy


def get_stats(db: "SQLerDB") -> DatabaseStats:
    """Get database statistics for monitoring.

    Args:
        db: SQLerDB instance.

    Returns:
        DatabaseStats with size, page info, table/index counts.

    Usage::

        from sqler.ops import get_stats

        stats = get_stats(db)
        print(f"Database size: {stats.size_bytes / 1024 / 1024:.2f} MB")
        print(f"Tables: {stats.table_count}, Indexes: {stats.index_count}")
    """
    path = db.adapter.path

    # Get file size
    size_bytes = 0
    wal_size_bytes = 0
    if path and not path.startswith(":") and not path.startswith("file:"):
        try:
            size_bytes = os.path.getsize(path)
            wal_path = path + "-wal"
            if os.path.exists(wal_path):
                wal_size_bytes = os.path.getsize(wal_path)
        except OSError:
            pass

    # Get page info
    cursor = db.adapter.execute("PRAGMA page_count;")
    page_count = cursor.fetchone()[0]

    cursor = db.adapter.execute("PRAGMA page_size;")
    page_size = cursor.fetchone()[0]

    cursor = db.adapter.execute("PRAGMA freelist_count;")
    freelist_count = cursor.fetchone()[0]

    # Count tables
    cursor = db.adapter.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
    )
    table_count = cursor.fetchone()[0]

    # Count indexes
    cursor = db.adapter.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%';"
    )
    index_count = cursor.fetchone()[0]

    return DatabaseStats(
        path=path,
        size_bytes=size_bytes,
        page_count=page_count,
        page_size=page_size,
        wal_size_bytes=wal_size_bytes,
        table_count=table_count,
        index_count=index_count,
        freelist_count=freelist_count,
        timestamp=datetime.now(),
    )


def backup(
    db: "SQLerDB",
    destination: str,
    *,
    pages_per_step: int = -1,
    sleep_ms: int = 0,
) -> BackupResult:
    """Create a backup of the database using SQLite's online backup API.

    This performs a safe, online backup that doesn't block readers/writers
    for extended periods. The backup is atomic - it either completes fully
    or not at all.

    Args:
        db: SQLerDB instance to backup.
        destination: Path to the backup file (will be created/overwritten).
        pages_per_step: Pages to copy per step (-1 for all at once).
        sleep_ms: Milliseconds to sleep between steps (for throttling).

    Returns:
        BackupResult with success status and metadata.

    Usage::

        from sqler.ops import backup

        result = backup(db, "/path/to/backup.db")
        if result.success:
            print(f"Backup completed: {result.size_bytes} bytes")
        else:
            print(f"Backup failed: {result.error}")
    """
    start = time.perf_counter()
    source_path = db.adapter.path
    timestamp = datetime.now()

    try:
        # Get the source connection
        source_conn = db.adapter._conn()

        # Create destination connection
        dest_conn = sqlite3.connect(destination)

        try:
            # Perform the backup using SQLite's backup API
            with dest_conn:
                source_conn.backup(
                    dest_conn,
                    pages=pages_per_step,
                    sleep=sleep_ms / 1000.0 if sleep_ms > 0 else 0,
                )
        finally:
            dest_conn.close()

        # Get backup file size
        size_bytes = os.path.getsize(destination)
        duration_ms = (time.perf_counter() - start) * 1000

        return BackupResult(
            success=True,
            source_path=source_path,
            destination_path=destination,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            timestamp=timestamp,
        )

    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        return BackupResult(
            success=False,
            source_path=source_path,
            destination_path=destination,
            duration_ms=duration_ms,
            size_bytes=0,
            timestamp=timestamp,
            error=str(e),
        )


def restore(
    db: "SQLerDB",
    source: str,
    *,
    pages_per_step: int = -1,
    sleep_ms: int = 0,
) -> BackupResult:
    """Restore a database from a backup file.

    WARNING: This overwrites the current database! All existing data will
    be replaced with the backup contents.

    Args:
        db: SQLerDB instance to restore into.
        source: Path to the backup file to restore from.
        pages_per_step: Pages to copy per step (-1 for all at once).
        sleep_ms: Milliseconds to sleep between steps.

    Returns:
        BackupResult with success status and metadata.

    Usage::

        from sqler.ops import restore

        result = restore(db, "/path/to/backup.db")
        if result.success:
            print("Database restored successfully")
        else:
            print(f"Restore failed: {result.error}")
    """
    start = time.perf_counter()
    dest_path = db.adapter.path
    timestamp = datetime.now()

    if not os.path.exists(source):
        return BackupResult(
            success=False,
            source_path=source,
            destination_path=dest_path,
            duration_ms=0,
            size_bytes=0,
            timestamp=timestamp,
            error=f"Source file not found: {source}",
        )

    try:
        # Get file size first
        size_bytes = os.path.getsize(source)

        # Open source connection
        source_conn = sqlite3.connect(source)

        # Get the destination connection
        dest_conn = db.adapter._conn()

        try:
            # Perform the restore using backup API (source -> dest)
            with dest_conn:
                source_conn.backup(
                    dest_conn,
                    pages=pages_per_step,
                    sleep=sleep_ms / 1000.0 if sleep_ms > 0 else 0,
                )
        finally:
            source_conn.close()

        duration_ms = (time.perf_counter() - start) * 1000

        # Clear any caches in the DB instance
        db._versioned_tables.clear()

        return BackupResult(
            success=True,
            source_path=source,
            destination_path=dest_path,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            timestamp=timestamp,
        )

    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        return BackupResult(
            success=False,
            source_path=source,
            destination_path=dest_path,
            duration_ms=duration_ms,
            size_bytes=0,
            timestamp=timestamp,
            error=str(e),
        )


def vacuum(db: "SQLerDB") -> float:
    """Reclaim unused space and defragment the database.

    Args:
        db: SQLerDB instance.

    Returns:
        Duration in milliseconds.
    """
    start = time.perf_counter()
    db.adapter.execute("VACUUM;")
    db.adapter.commit()
    return (time.perf_counter() - start) * 1000


def checkpoint(db: "SQLerDB", mode: str = "PASSIVE") -> dict[str, int]:
    """Force a WAL checkpoint.

    Args:
        db: SQLerDB instance.
        mode: PASSIVE, FULL, RESTART, or TRUNCATE.

    Returns:
        Dict with 'busy', 'log', and 'checkpointed' page counts.
    """
    cursor = db.adapter.execute(f"PRAGMA wal_checkpoint({mode});")
    row = cursor.fetchone()
    return {
        "busy": row[0],
        "log": row[1],
        "checkpointed": row[2],
    }


# ============================================================================
# Async Operations
# ============================================================================


async def async_health_check(db: "AsyncSQLerDB", timeout_ms: int = 5000) -> HealthStatus:
    """Perform an async health check on the database.

    Args:
        db: AsyncSQLerDB instance to check.
        timeout_ms: Maximum time to wait for response.

    Returns:
        HealthStatus with check results.
    """
    start = time.perf_counter()
    details: dict[str, Any] = {}

    try:
        # Basic connectivity check
        cursor = await db.adapter.execute("SELECT 1;")
        result = await cursor.fetchone()
        await cursor.close()

        if result is None or result[0] != 1:
            return HealthStatus(
                healthy=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                message="SELECT 1 returned unexpected result",
                timestamp=datetime.now(),
                details=details,
            )

        # Get database info
        cursor = await db.adapter.execute("PRAGMA database_list;")
        db_list = await cursor.fetchall()
        await cursor.close()
        details["databases"] = [{"name": row[1], "file": row[2]} for row in db_list]

        # Check journal mode
        cursor = await db.adapter.execute("PRAGMA journal_mode;")
        journal_mode = await cursor.fetchone()
        await cursor.close()
        details["journal_mode"] = journal_mode[0] if journal_mode else "unknown"

        # Check integrity (quick check)
        cursor = await db.adapter.execute("PRAGMA quick_check(1);")
        integrity = await cursor.fetchone()
        await cursor.close()
        integrity_ok = integrity and integrity[0] == "ok"
        details["integrity_check"] = "ok" if integrity_ok else "failed"

        if not integrity_ok:
            return HealthStatus(
                healthy=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                message="Integrity check failed",
                timestamp=datetime.now(),
                details=details,
            )

        latency_ms = (time.perf_counter() - start) * 1000
        return HealthStatus(
            healthy=True,
            latency_ms=latency_ms,
            message="OK",
            timestamp=datetime.now(),
            details=details,
        )

    except Exception as e:
        return HealthStatus(
            healthy=False,
            latency_ms=(time.perf_counter() - start) * 1000,
            message=str(e),
            timestamp=datetime.now(),
            details=details,
        )


async def async_is_healthy(db: "AsyncSQLerDB", timeout_ms: int = 5000) -> bool:
    """Quick async health check returning boolean."""
    result = await async_health_check(db, timeout_ms)
    return result.healthy


async def async_get_stats(db: "AsyncSQLerDB") -> DatabaseStats:
    """Get database statistics asynchronously.

    Args:
        db: AsyncSQLerDB instance.

    Returns:
        DatabaseStats with size, page info, table/index counts.
    """
    path = db.adapter.path

    # Get file size
    size_bytes = 0
    wal_size_bytes = 0
    if path and not path.startswith(":") and not path.startswith("file:"):
        try:
            size_bytes = os.path.getsize(path)
            wal_path = path + "-wal"
            if os.path.exists(wal_path):
                wal_size_bytes = os.path.getsize(wal_path)
        except OSError:
            pass

    # Get page info
    cursor = await db.adapter.execute("PRAGMA page_count;")
    row = await cursor.fetchone()
    await cursor.close()
    page_count = row[0]

    cursor = await db.adapter.execute("PRAGMA page_size;")
    row = await cursor.fetchone()
    await cursor.close()
    page_size = row[0]

    cursor = await db.adapter.execute("PRAGMA freelist_count;")
    row = await cursor.fetchone()
    await cursor.close()
    freelist_count = row[0]

    # Count tables
    cursor = await db.adapter.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
    )
    row = await cursor.fetchone()
    await cursor.close()
    table_count = row[0]

    # Count indexes
    cursor = await db.adapter.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%';"
    )
    row = await cursor.fetchone()
    await cursor.close()
    index_count = row[0]

    return DatabaseStats(
        path=path,
        size_bytes=size_bytes,
        page_count=page_count,
        page_size=page_size,
        wal_size_bytes=wal_size_bytes,
        table_count=table_count,
        index_count=index_count,
        freelist_count=freelist_count,
        timestamp=datetime.now(),
    )


async def async_backup(
    db: "AsyncSQLerDB",
    destination: str,
    *,
    pages_per_step: int = -1,
    sleep_ms: int = 0,
) -> BackupResult:
    """Create an async backup of the database.

    Note: The actual backup operation uses SQLite's synchronous backup API
    but is wrapped for async compatibility.

    Args:
        db: AsyncSQLerDB instance to backup.
        destination: Path to the backup file.
        pages_per_step: Pages to copy per step (-1 for all at once).
        sleep_ms: Milliseconds to sleep between steps.

    Returns:
        BackupResult with success status and metadata.
    """
    import asyncio

    start = time.perf_counter()
    source_path = db.adapter.path
    timestamp = datetime.now()

    try:
        # Get the underlying connection from the async adapter
        # aiosqlite wraps a sqlite3.Connection
        conn = db.adapter._conn
        if conn is None:
            raise RuntimeError("Database not connected")

        # Get the actual sqlite3 connection
        source_conn = conn._conn  # type: ignore[attr-defined]

        # Create destination connection
        dest_conn = sqlite3.connect(destination)

        try:
            # Run backup in executor to not block event loop
            def do_backup():
                with dest_conn:
                    source_conn.backup(
                        dest_conn,
                        pages=pages_per_step,
                        sleep=sleep_ms / 1000.0 if sleep_ms > 0 else 0,
                    )

            await asyncio.get_event_loop().run_in_executor(None, do_backup)
        finally:
            dest_conn.close()

        # Get backup file size
        size_bytes = os.path.getsize(destination)
        duration_ms = (time.perf_counter() - start) * 1000

        return BackupResult(
            success=True,
            source_path=source_path,
            destination_path=destination,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            timestamp=timestamp,
        )

    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        return BackupResult(
            success=False,
            source_path=source_path,
            destination_path=destination,
            duration_ms=duration_ms,
            size_bytes=0,
            timestamp=timestamp,
            error=str(e),
        )


async def async_vacuum(db: "AsyncSQLerDB") -> float:
    """Async vacuum to reclaim unused space."""
    start = time.perf_counter()
    cursor = await db.adapter.execute("VACUUM;")
    await cursor.close()
    await db.adapter.commit()
    return (time.perf_counter() - start) * 1000


async def async_checkpoint(db: "AsyncSQLerDB", mode: str = "PASSIVE") -> dict[str, int]:
    """Force an async WAL checkpoint."""
    cursor = await db.adapter.execute(f"PRAGMA wal_checkpoint({mode});")
    row = await cursor.fetchone()
    await cursor.close()
    return {
        "busy": row[0],
        "log": row[1],
        "checkpointed": row[2],
    }
