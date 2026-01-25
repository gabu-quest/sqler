"""Schema migration system for SQLer databases.

This module provides a simple but powerful migration system for evolving
database schemas over time. Migrations are Python functions that can modify
schema (DDL) or transform data.

Usage::

    from sqler import SQLerDB
    from sqler.migrations import Migration, MigrationRunner

    # Define migrations
    migrations = [
        Migration(
            version=1,
            name="add_users_email_index",
            up=lambda db: db.create_index("users", "email", unique=True),
            down=lambda db: db.drop_index("idx_users_email"),
        ),
        Migration(
            version=2,
            name="add_posts_table",
            up=lambda db: db.adapter.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    _id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data JSON NOT NULL
                )
            '''),
            down=lambda db: db.adapter.execute("DROP TABLE IF EXISTS posts"),
        ),
    ]

    # Run migrations
    db = SQLerDB.on_disk("myapp.db")
    runner = MigrationRunner(db, migrations)
    runner.migrate()  # Apply all pending migrations
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from sqler.db.async_db import AsyncSQLerDB
    from sqler.db.sqler_db import SQLerDB


MIGRATIONS_TABLE = "_sqler_migrations"


@dataclass
class Migration:
    """A single migration step.

    Attributes:
        version: Unique integer version number (must be sequential).
        name: Human-readable name for the migration.
        up: Function to apply the migration.
        down: Function to rollback the migration (optional but recommended).
        description: Optional longer description.
    """

    version: int
    name: str
    up: Callable[["SQLerDB"], None]
    down: Optional[Callable[["SQLerDB"], None]] = None
    description: str = ""

    def __post_init__(self):
        if self.version < 1:
            raise ValueError("Migration version must be >= 1")


@dataclass
class AsyncMigration:
    """An async migration step.

    Attributes:
        version: Unique integer version number.
        name: Human-readable name.
        up: Async function to apply the migration.
        down: Async function to rollback (optional).
        description: Optional longer description.
    """

    version: int
    name: str
    up: Callable[["AsyncSQLerDB"], Any]  # Returns Awaitable
    down: Optional[Callable[["AsyncSQLerDB"], Any]] = None
    description: str = ""

    def __post_init__(self):
        if self.version < 1:
            raise ValueError("Migration version must be >= 1")


@dataclass
class MigrationRecord:
    """Record of an applied migration."""

    version: int
    name: str
    applied_at: datetime
    duration_ms: float
    checksum: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "applied_at": self.applied_at.isoformat(),
            "duration_ms": self.duration_ms,
            "checksum": self.checksum,
        }


@dataclass
class MigrationResult:
    """Result of a migration run."""

    success: bool
    applied: list[MigrationRecord]
    rolled_back: list[MigrationRecord]
    current_version: int
    target_version: int
    error: Optional[str] = None
    error_at_version: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "applied": [r.to_dict() for r in self.applied],
            "rolled_back": [r.to_dict() for r in self.rolled_back],
            "current_version": self.current_version,
            "target_version": self.target_version,
            "error": self.error,
            "error_at_version": self.error_at_version,
        }


class MigrationRunner:
    """Runs migrations against a SQLerDB.

    Usage::

        runner = MigrationRunner(db, migrations)

        # Check current status
        print(f"Current version: {runner.current_version()}")
        print(f"Pending: {runner.pending_migrations()}")

        # Apply all pending
        result = runner.migrate()

        # Rollback to specific version
        result = runner.rollback(target_version=1)

        # Reset to version 0 (rollback all)
        result = runner.reset()
    """

    def __init__(self, db: "SQLerDB", migrations: list[Migration]):
        """Initialize the migration runner.

        Args:
            db: SQLerDB instance.
            migrations: List of migrations in version order.
        """
        self.db = db
        self.migrations = sorted(migrations, key=lambda m: m.version)
        self._ensure_migrations_table()
        self._validate_migrations()

    def _ensure_migrations_table(self) -> None:
        """Create the migrations tracking table if it doesn't exist."""
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            duration_ms REAL NOT NULL,
            checksum TEXT
        );
        """
        self.db.adapter.execute(ddl)
        self.db.adapter.commit()

    def _validate_migrations(self) -> None:
        """Validate migration versions are sequential and unique."""
        versions = [m.version for m in self.migrations]
        if len(versions) != len(set(versions)):
            raise ValueError("Duplicate migration versions detected")

        for i, v in enumerate(versions):
            if v != i + 1:
                raise ValueError(
                    f"Migration versions must be sequential starting from 1. "
                    f"Expected {i + 1}, got {v}"
                )

    def current_version(self) -> int:
        """Get the current database schema version.

        Returns:
            The highest applied migration version, or 0 if none applied.
        """
        cursor = self.db.adapter.execute(f"SELECT MAX(version) FROM {MIGRATIONS_TABLE};")
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

    def applied_migrations(self) -> list[MigrationRecord]:
        """Get list of all applied migrations.

        Returns:
            List of MigrationRecord in version order.
        """
        cursor = self.db.adapter.execute(
            f"SELECT version, name, applied_at, duration_ms, checksum "
            f"FROM {MIGRATIONS_TABLE} ORDER BY version;"
        )
        records = []
        for row in cursor.fetchall():
            records.append(
                MigrationRecord(
                    version=row[0],
                    name=row[1],
                    applied_at=datetime.fromisoformat(row[2]),
                    duration_ms=row[3],
                    checksum=row[4] or "",
                )
            )
        return records

    def pending_migrations(self) -> list[Migration]:
        """Get list of migrations not yet applied.

        Returns:
            List of Migration objects that need to be applied.
        """
        current = self.current_version()
        return [m for m in self.migrations if m.version > current]

    def migrate(self, target_version: Optional[int] = None) -> MigrationResult:
        """Apply pending migrations up to target version.

        Args:
            target_version: Version to migrate to (None = latest).

        Returns:
            MigrationResult with details of applied migrations.
        """
        current = self.current_version()
        target = (
            target_version
            if target_version is not None
            else (self.migrations[-1].version if self.migrations else 0)
        )

        if target < current:
            # This is a rollback, not a migrate
            return self.rollback(target)

        pending = [m for m in self.migrations if current < m.version <= target]
        applied: list[MigrationRecord] = []

        for migration in pending:
            start = time.perf_counter()
            try:
                # Run migration in a transaction
                with self.db.transaction():
                    migration.up(self.db)

                    # Record the migration
                    duration_ms = (time.perf_counter() - start) * 1000
                    self.db.adapter.execute(
                        f"INSERT INTO {MIGRATIONS_TABLE} "
                        f"(version, name, applied_at, duration_ms, checksum) "
                        f"VALUES (?, ?, ?, ?, ?);",
                        [
                            migration.version,
                            migration.name,
                            datetime.now().isoformat(),
                            duration_ms,
                            "",
                        ],
                    )

                record = MigrationRecord(
                    version=migration.version,
                    name=migration.name,
                    applied_at=datetime.now(),
                    duration_ms=duration_ms,
                )
                applied.append(record)

            except Exception as e:
                return MigrationResult(
                    success=False,
                    applied=applied,
                    rolled_back=[],
                    current_version=self.current_version(),
                    target_version=target,
                    error=str(e),
                    error_at_version=migration.version,
                )

        return MigrationResult(
            success=True,
            applied=applied,
            rolled_back=[],
            current_version=self.current_version(),
            target_version=target,
        )

    def rollback(self, target_version: int = 0) -> MigrationResult:
        """Rollback migrations to target version.

        Args:
            target_version: Version to rollback to (0 = rollback all).

        Returns:
            MigrationResult with details of rolled back migrations.
        """
        current = self.current_version()

        if target_version >= current:
            return MigrationResult(
                success=True,
                applied=[],
                rolled_back=[],
                current_version=current,
                target_version=target_version,
            )

        # Get migrations to rollback in reverse order
        to_rollback = [
            m for m in reversed(self.migrations) if target_version < m.version <= current
        ]

        rolled_back: list[MigrationRecord] = []

        for migration in to_rollback:
            if migration.down is None:
                return MigrationResult(
                    success=False,
                    applied=[],
                    rolled_back=rolled_back,
                    current_version=self.current_version(),
                    target_version=target_version,
                    error=f"Migration {migration.version} has no down() function",
                    error_at_version=migration.version,
                )

            start = time.perf_counter()
            try:
                # Run rollback in a transaction
                with self.db.transaction():
                    migration.down(self.db)

                    # Remove the migration record
                    self.db.adapter.execute(
                        f"DELETE FROM {MIGRATIONS_TABLE} WHERE version = ?;",
                        [migration.version],
                    )

                duration_ms = (time.perf_counter() - start) * 1000
                record = MigrationRecord(
                    version=migration.version,
                    name=migration.name,
                    applied_at=datetime.now(),
                    duration_ms=duration_ms,
                )
                rolled_back.append(record)

            except Exception as e:
                return MigrationResult(
                    success=False,
                    applied=[],
                    rolled_back=rolled_back,
                    current_version=self.current_version(),
                    target_version=target_version,
                    error=str(e),
                    error_at_version=migration.version,
                )

        return MigrationResult(
            success=True,
            applied=[],
            rolled_back=rolled_back,
            current_version=self.current_version(),
            target_version=target_version,
        )

    def reset(self) -> MigrationResult:
        """Rollback all migrations (reset to version 0).

        Returns:
            MigrationResult with details.
        """
        return self.rollback(target_version=0)

    def status(self) -> dict[str, Any]:
        """Get migration status summary.

        Returns:
            Dict with current version, applied count, pending count, etc.
        """
        applied = self.applied_migrations()
        pending = self.pending_migrations()
        return {
            "current_version": self.current_version(),
            "latest_version": self.migrations[-1].version if self.migrations else 0,
            "applied_count": len(applied),
            "pending_count": len(pending),
            "applied": [r.to_dict() for r in applied],
            "pending": [{"version": m.version, "name": m.name} for m in pending],
        }


class AsyncMigrationRunner:
    """Async migration runner for AsyncSQLerDB.

    Usage::

        runner = AsyncMigrationRunner(db, migrations)
        await runner.migrate()
    """

    def __init__(self, db: "AsyncSQLerDB", migrations: list[AsyncMigration]):
        self.db = db
        self.migrations = sorted(migrations, key=lambda m: m.version)
        self._initialized = False

    async def _ensure_migrations_table(self) -> None:
        """Create the migrations tracking table if it doesn't exist."""
        if self._initialized:
            return
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            duration_ms REAL NOT NULL,
            checksum TEXT
        );
        """
        cursor = await self.db.adapter.execute(ddl)
        await cursor.close()
        await self.db.adapter.commit()
        self._initialized = True
        self._validate_migrations()

    def _validate_migrations(self) -> None:
        """Validate migration versions are sequential and unique."""
        versions = [m.version for m in self.migrations]
        if len(versions) != len(set(versions)):
            raise ValueError("Duplicate migration versions detected")

        for i, v in enumerate(versions):
            if v != i + 1:
                raise ValueError(
                    f"Migration versions must be sequential starting from 1. "
                    f"Expected {i + 1}, got {v}"
                )

    async def current_version(self) -> int:
        """Get the current database schema version."""
        await self._ensure_migrations_table()
        cursor = await self.db.adapter.execute(f"SELECT MAX(version) FROM {MIGRATIONS_TABLE};")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row and row[0] is not None else 0

    async def pending_migrations(self) -> list[AsyncMigration]:
        """Get list of migrations not yet applied."""
        current = await self.current_version()
        return [m for m in self.migrations if m.version > current]

    async def migrate(self, target_version: Optional[int] = None) -> MigrationResult:
        """Apply pending migrations up to target version."""
        await self._ensure_migrations_table()
        current = await self.current_version()
        target = (
            target_version
            if target_version is not None
            else (self.migrations[-1].version if self.migrations else 0)
        )

        if target < current:
            return await self.rollback(target)

        pending = [m for m in self.migrations if current < m.version <= target]
        applied: list[MigrationRecord] = []

        for migration in pending:
            start = time.perf_counter()
            try:
                # Run migration
                await migration.up(self.db)

                # Record the migration
                duration_ms = (time.perf_counter() - start) * 1000
                cursor = await self.db.adapter.execute(
                    f"INSERT INTO {MIGRATIONS_TABLE} "
                    f"(version, name, applied_at, duration_ms, checksum) "
                    f"VALUES (?, ?, ?, ?, ?);",
                    [
                        migration.version,
                        migration.name,
                        datetime.now().isoformat(),
                        duration_ms,
                        "",
                    ],
                )
                await cursor.close()
                await self.db.adapter.commit()

                record = MigrationRecord(
                    version=migration.version,
                    name=migration.name,
                    applied_at=datetime.now(),
                    duration_ms=duration_ms,
                )
                applied.append(record)

            except Exception as e:
                return MigrationResult(
                    success=False,
                    applied=applied,
                    rolled_back=[],
                    current_version=await self.current_version(),
                    target_version=target,
                    error=str(e),
                    error_at_version=migration.version,
                )

        return MigrationResult(
            success=True,
            applied=applied,
            rolled_back=[],
            current_version=await self.current_version(),
            target_version=target,
        )

    async def rollback(self, target_version: int = 0) -> MigrationResult:
        """Rollback migrations to target version."""
        await self._ensure_migrations_table()
        current = await self.current_version()

        if target_version >= current:
            return MigrationResult(
                success=True,
                applied=[],
                rolled_back=[],
                current_version=current,
                target_version=target_version,
            )

        to_rollback = [
            m for m in reversed(self.migrations) if target_version < m.version <= current
        ]

        rolled_back: list[MigrationRecord] = []

        for migration in to_rollback:
            if migration.down is None:
                return MigrationResult(
                    success=False,
                    applied=[],
                    rolled_back=rolled_back,
                    current_version=await self.current_version(),
                    target_version=target_version,
                    error=f"Migration {migration.version} has no down() function",
                    error_at_version=migration.version,
                )

            start = time.perf_counter()
            try:
                await migration.down(self.db)

                cursor = await self.db.adapter.execute(
                    f"DELETE FROM {MIGRATIONS_TABLE} WHERE version = ?;",
                    [migration.version],
                )
                await cursor.close()
                await self.db.adapter.commit()

                duration_ms = (time.perf_counter() - start) * 1000
                record = MigrationRecord(
                    version=migration.version,
                    name=migration.name,
                    applied_at=datetime.now(),
                    duration_ms=duration_ms,
                )
                rolled_back.append(record)

            except Exception as e:
                return MigrationResult(
                    success=False,
                    applied=[],
                    rolled_back=rolled_back,
                    current_version=await self.current_version(),
                    target_version=target_version,
                    error=str(e),
                    error_at_version=migration.version,
                )

        return MigrationResult(
            success=True,
            applied=[],
            rolled_back=rolled_back,
            current_version=await self.current_version(),
            target_version=target_version,
        )

    async def status(self) -> dict[str, Any]:
        """Get migration status summary."""
        await self._ensure_migrations_table()
        current = await self.current_version()
        pending = await self.pending_migrations()
        return {
            "current_version": current,
            "latest_version": self.migrations[-1].version if self.migrations else 0,
            "pending_count": len(pending),
            "pending": [{"version": m.version, "name": m.name} for m in pending],
        }
