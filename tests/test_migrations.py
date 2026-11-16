"""MigrationRunner: applying, targeting, rolling back and validating migrations."""

import pytest

from sqler import Migration, MigrationRunner, SQLerDB


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
                up=lambda db: db.adapter.execute("CREATE TABLE test (_id INTEGER PRIMARY KEY);"),
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
