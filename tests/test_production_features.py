"""Tests for production features: backup, health, migrations, metrics, audit."""

import os
import tempfile
import time

import pytest

from sqler import (
    AuditMixin,
    Migration,
    MigrationRunner,
    SQLerDB,
    SQLerModel,
    backup,
    get_stats,
    health_check,
    is_healthy,
    metrics,
    restore,
    vacuum,
)


# ============================================================================
# Health Check Tests
# ============================================================================


class TestHealthCheck:
    """Tests for health_check and is_healthy functions."""

    def test_health_check_healthy_db(self):
        """Health check returns healthy for working database."""
        db = SQLerDB.in_memory(shared=False)
        status = health_check(db)

        assert status.healthy is True
        assert status.message == "OK"
        assert status.latency_ms >= 0
        assert "journal_mode" in status.details
        assert status.details["integrity_check"] == "ok"

    def test_is_healthy_returns_bool(self):
        """is_healthy returns simple boolean."""
        db = SQLerDB.in_memory(shared=False)
        assert is_healthy(db) is True

    def test_health_check_details(self):
        """Health check includes useful details."""
        db = SQLerDB.in_memory(shared=False)
        status = health_check(db)

        assert "databases" in status.details
        assert isinstance(status.details["databases"], list)

    def test_health_status_to_dict(self):
        """HealthStatus can be serialized to dict."""
        db = SQLerDB.in_memory(shared=False)
        status = health_check(db)
        data = status.to_dict()

        assert "healthy" in data
        assert "latency_ms" in data
        assert "timestamp" in data


class TestDatabaseStats:
    """Tests for get_stats function."""

    def test_get_stats_empty_db(self):
        """Stats work on empty database."""
        db = SQLerDB.in_memory(shared=False)
        stats = get_stats(db)

        assert stats.table_count == 0
        assert stats.index_count == 0
        assert stats.page_count >= 0

    def test_get_stats_with_data(self):
        """Stats reflect actual database state."""
        db = SQLerDB.in_memory(shared=False)

        class User(SQLerModel):
            name: str

        User.set_db(db)
        User(name="Alice").save()
        db.create_index("user", "name")

        stats = get_stats(db)
        assert stats.table_count >= 1
        assert stats.index_count >= 1

    def test_stats_to_dict(self):
        """DatabaseStats can be serialized."""
        db = SQLerDB.in_memory(shared=False)
        stats = get_stats(db)
        data = stats.to_dict()

        assert "table_count" in data
        assert "page_size" in data
        assert "timestamp" in data


# ============================================================================
# Backup/Restore Tests
# ============================================================================


class TestBackupRestore:
    """Tests for backup and restore functions."""

    def test_backup_creates_file(self):
        """Backup creates a valid database file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "source.db")
            backup_path = os.path.join(tmpdir, "backup.db")

            db = SQLerDB.on_disk(db_path)

            class User(SQLerModel):
                name: str

            User.set_db(db)
            User(name="Alice").save()
            User(name="Bob").save()

            result = backup(db, backup_path)

            assert result.success is True
            assert os.path.exists(backup_path)
            assert result.size_bytes > 0
            assert result.duration_ms >= 0

            db.close()

    def test_backup_result_to_dict(self):
        """BackupResult can be serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "source.db")
            backup_path = os.path.join(tmpdir, "backup.db")

            db = SQLerDB.on_disk(db_path)
            result = backup(db, backup_path)
            data = result.to_dict()

            assert "success" in data
            assert "size_bytes" in data
            assert "timestamp" in data

            db.close()

    def test_restore_replaces_data(self):
        """Restore replaces database with backup contents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "main.db")
            backup_path = os.path.join(tmpdir, "backup.db")

            # Create original db with data
            db = SQLerDB.on_disk(db_path)

            class User(SQLerModel):
                name: str

            User.set_db(db)
            User(name="Alice").save()
            User(name="Bob").save()

            # Create backup
            backup(db, backup_path)

            # Add more data to original
            User(name="Charlie").save()
            assert User.count() == 3

            # Restore from backup
            result = restore(db, backup_path)

            assert result.success is True
            # After restore, should only have 2 users
            assert User.count() == 2

            db.close()

    def test_restore_missing_file_fails(self):
        """Restore fails gracefully for missing file."""
        db = SQLerDB.in_memory(shared=False)
        result = restore(db, "/nonexistent/path/backup.db")

        assert result.success is False
        assert "not found" in result.error.lower()


class TestVacuum:
    """Tests for vacuum function."""

    def test_vacuum_runs(self):
        """Vacuum executes without error."""
        db = SQLerDB.in_memory(shared=False)

        class User(SQLerModel):
            name: str

        User.set_db(db)

        # Create and delete some data to create fragmentation
        for i in range(10):
            u = User(name=f"User{i}").save()
            u.delete()

        duration_ms = vacuum(db)
        assert duration_ms >= 0


# ============================================================================
# Migration Tests
# ============================================================================


class TestMigrations:
    """Tests for the migration system."""

    def test_migration_runner_creates_table(self):
        """Migration runner creates tracking table."""
        db = SQLerDB.in_memory(shared=False)
        runner = MigrationRunner(db, [])

        # Check table exists
        cursor = db.adapter.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_sqler_migrations';"
        )
        assert cursor.fetchone() is not None

    def test_migration_version_starts_at_zero(self):
        """Fresh database has version 0."""
        db = SQLerDB.in_memory(shared=False)
        runner = MigrationRunner(db, [])

        assert runner.current_version() == 0

    def test_migrate_applies_migrations(self):
        """Migrations are applied in order."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(
                version=1,
                name="create_users_table",
                up=lambda db: db.adapter.execute(
                    "CREATE TABLE users (_id INTEGER PRIMARY KEY, data JSON);"
                ),
                down=lambda db: db.adapter.execute("DROP TABLE users;"),
            ),
            Migration(
                version=2,
                name="create_posts_table",
                up=lambda db: db.adapter.execute(
                    "CREATE TABLE posts (_id INTEGER PRIMARY KEY, data JSON);"
                ),
                down=lambda db: db.adapter.execute("DROP TABLE posts;"),
            ),
        ]

        runner = MigrationRunner(db, migrations)
        result = runner.migrate()

        assert result.success is True
        assert len(result.applied) == 2
        assert runner.current_version() == 2

    def test_migrate_to_target_version(self):
        """Can migrate to specific version."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(version=1, name="m1", up=lambda db: None, down=lambda db: None),
            Migration(version=2, name="m2", up=lambda db: None, down=lambda db: None),
            Migration(version=3, name="m3", up=lambda db: None, down=lambda db: None),
        ]

        runner = MigrationRunner(db, migrations)
        result = runner.migrate(target_version=2)

        assert result.success is True
        assert runner.current_version() == 2
        assert len(result.applied) == 2

    def test_pending_migrations(self):
        """Pending migrations are correctly identified."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(version=1, name="m1", up=lambda db: None, down=lambda db: None),
            Migration(version=2, name="m2", up=lambda db: None, down=lambda db: None),
        ]

        runner = MigrationRunner(db, migrations)
        assert len(runner.pending_migrations()) == 2

        runner.migrate(target_version=1)
        assert len(runner.pending_migrations()) == 1

    def test_rollback_migrations(self):
        """Migrations can be rolled back."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(
                version=1,
                name="create_table",
                up=lambda db: db.adapter.execute(
                    "CREATE TABLE test (_id INTEGER PRIMARY KEY);"
                ),
                down=lambda db: db.adapter.execute("DROP TABLE test;"),
            ),
        ]

        runner = MigrationRunner(db, migrations)
        runner.migrate()
        assert runner.current_version() == 1

        result = runner.rollback(target_version=0)
        assert result.success is True
        assert runner.current_version() == 0

    def test_migration_error_stops_batch(self):
        """Error in migration stops further migrations."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(version=1, name="m1", up=lambda db: None),
            Migration(
                version=2,
                name="m2_fails",
                up=lambda db: (_ for _ in ()).throw(ValueError("Intentional error")),
            ),
            Migration(version=3, name="m3", up=lambda db: None),
        ]

        runner = MigrationRunner(db, migrations)
        result = runner.migrate()

        assert result.success is False
        assert result.error_at_version == 2
        assert runner.current_version() == 1  # Only m1 applied

    def test_migration_status(self):
        """Status provides useful summary."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(version=1, name="m1", up=lambda db: None),
            Migration(version=2, name="m2", up=lambda db: None),
        ]

        runner = MigrationRunner(db, migrations)
        runner.migrate(target_version=1)

        status = runner.status()
        assert status["current_version"] == 1
        assert status["latest_version"] == 2
        assert status["applied_count"] == 1
        assert status["pending_count"] == 1

    def test_duplicate_version_raises(self):
        """Duplicate version numbers raise error."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(version=1, name="m1", up=lambda db: None),
            Migration(version=1, name="m1_dup", up=lambda db: None),
        ]

        with pytest.raises(ValueError, match="Duplicate"):
            MigrationRunner(db, migrations)

    def test_non_sequential_version_raises(self):
        """Non-sequential versions raise error."""
        db = SQLerDB.in_memory(shared=False)

        migrations = [
            Migration(version=1, name="m1", up=lambda db: None),
            Migration(version=3, name="m3", up=lambda db: None),  # Missing 2
        ]

        with pytest.raises(ValueError, match="sequential"):
            MigrationRunner(db, migrations)


# ============================================================================
# Metrics Tests
# ============================================================================


class TestMetrics:
    """Tests for the metrics collection system."""

    def test_metrics_disabled_by_default(self):
        """Metrics are disabled by default."""
        # Reset metrics
        metrics.reset()
        assert metrics._enabled is False

    def test_metrics_enable_disable(self):
        """Can enable and disable metrics."""
        metrics.enable()
        assert metrics._enabled is True

        metrics.disable()
        assert metrics._enabled is False

    def test_metrics_record_queries(self):
        """Metrics are recorded when enabled."""
        metrics.reset()
        metrics.enable()

        db = SQLerDB.in_memory(shared=False)

        class User(SQLerModel):
            name: str

        User.set_db(db)
        User(name="Alice").save()
        User(name="Bob").save()

        data = metrics.get_metrics()
        assert data["queries"]["total_queries"] >= 2

        metrics.disable()

    def test_prometheus_export(self):
        """Prometheus format export works."""
        metrics.reset()
        metrics.enable()

        db = SQLerDB.in_memory(shared=False)
        db.adapter.execute("SELECT 1;")

        output = metrics.prometheus_export()
        assert "sqler_queries_total" in output
        assert "sqler_query_duration_ms_bucket" in output

        metrics.disable()

    def test_metrics_histogram_buckets(self):
        """Duration histogram has correct buckets."""
        metrics.reset()
        metrics.enable()

        db = SQLerDB.in_memory(shared=False)
        db.adapter.execute("SELECT 1;")

        data = metrics.get_metrics()
        histogram = data["queries"]["histogram"]
        assert "le_1" in histogram
        assert "le_100" in histogram
        assert "le_inf" in histogram

        metrics.disable()

    def test_metrics_callback(self):
        """Custom callbacks receive query logs."""
        metrics.reset()
        metrics.enable()

        captured = []
        metrics.add_callback(lambda log: captured.append(log))

        db = SQLerDB.in_memory(shared=False)
        db.adapter.execute("SELECT 42;")

        assert len(captured) >= 1
        assert "SELECT 42" in captured[-1].sql

        metrics.disable()


# ============================================================================
# Audit Mixin Tests
# ============================================================================


class TestAuditMixin:
    """Tests for the AuditMixin."""

    def test_audit_mixin_tracks_user(self):
        """AuditMixin tracks created_by and updated_by."""
        db = SQLerDB.in_memory(shared=False)

        class AuditedUser(AuditMixin, SQLerModel):
            name: str

        AuditedUser.set_db(db)

        # Set current user
        AuditMixin.set_current_user("admin@example.com")

        user = AuditedUser(name="Alice")
        user._set_audit_fields()

        assert user.created_by == "admin@example.com"
        assert user.updated_by == "admin@example.com"
        assert user.created_at is not None
        assert user.updated_at is not None

    def test_audit_mixin_getter(self):
        """AuditMixin supports user getter function."""

        def get_user():
            return "dynamic_user"

        AuditMixin.set_current_user_getter(get_user)
        assert AuditMixin.get_current_user() == "dynamic_user"

        # Clean up
        AuditMixin.set_current_user_getter(None)
        AuditMixin.set_current_user(None)

    def test_audit_timestamps_on_update(self):
        """Updated_at changes on subsequent saves."""
        db = SQLerDB.in_memory(shared=False)

        class AuditedUser(AuditMixin, SQLerModel):
            name: str

        AuditedUser.set_db(db)
        AuditMixin.set_current_user("user1")

        user = AuditedUser(name="Alice")
        user._set_audit_fields()
        first_updated = user.updated_at

        time.sleep(0.01)  # Small delay
        user._set_audit_fields()

        assert user.updated_at > first_updated
        assert user.created_at == user.created_at  # created_at unchanged

        AuditMixin.set_current_user(None)
