"""health_check, is_healthy, get_stats and vacuum."""

import re

from sqler import SQLerDB, SQLerModel, get_stats, health_check, is_healthy, vacuum


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

        assert isinstance(status.details["databases"], list)
        assert len(status.details["databases"]) == 1
        assert any(db["name"] == "main" for db in status.details["databases"])

    def test_health_status_to_dict(self):
        """HealthStatus can be serialized to dict with correct values."""
        db = SQLerDB.in_memory(shared=False)
        status = health_check(db)
        data = status.to_dict()

        assert data["healthy"] is True
        assert data["latency_ms"] >= 0
        assert data["message"] == "OK"
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", data["timestamp"])


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
        assert stats.table_count == 2
        assert stats.index_count == 1

    def test_stats_to_dict(self):
        """DatabaseStats can be serialized with correct values."""
        db = SQLerDB.in_memory(shared=False)
        stats = get_stats(db)
        data = stats.to_dict()

        assert data["table_count"] == 0
        assert data["page_size"] == 4096
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", data["timestamp"])
        assert data["page_count"] >= 0


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
