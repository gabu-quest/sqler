"""Tests for Database management endpoints.

Tests health, stats, vacuum, checkpoint.
"""

from fastapi.testclient import TestClient


class TestDatabaseHealth:
    """Database health endpoint tests."""

    def test_health_check(self, client: TestClient):
        """Health endpoint returns status."""
        resp = client.get("/api/db/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["healthy"] is True
        assert "latency_ms" in data
        assert isinstance(data["latency_ms"], (int, float))
        assert data["latency_ms"] >= 0
        assert "journal_mode" in data
        assert "wal_mode" in data

    def test_health_latency_reasonable(self, client: TestClient):
        """Health check latency is reasonable (< 100ms)."""
        resp = client.get("/api/db/health")
        data = resp.json()
        # Latency should be under 100ms for a simple health check
        assert data["latency_ms"] < 100


class TestDatabaseStats:
    """Database statistics endpoint tests."""

    def test_stats(self, client: TestClient):
        """Stats endpoint returns database info."""
        resp = client.get("/api/db/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "size_bytes" in data
        assert isinstance(data["size_bytes"], int)
        assert data["size_bytes"] >= 0
        assert "table_count" in data
        assert "index_count" in data


class TestDatabaseMaintenance:
    """Database maintenance endpoint tests."""

    def test_vacuum(self, client: TestClient):
        """Vacuum endpoint runs without error."""
        resp = client.post("/api/db/vacuum")
        assert resp.status_code == 200

    def test_checkpoint(self, client: TestClient):
        """Checkpoint endpoint runs without error."""
        resp = client.post("/api/db/checkpoint")
        assert resp.status_code == 200
