"""Tests for Database management endpoints.

Tests health, stats, vacuum, checkpoint.
"""

from examples.fastapi.db import get_db
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


class TestSeed:
    """Seeding resets the tables, whatever they hold."""

    def test_seed_twice_gives_the_same_counts(self, client: TestClient):
        """A seed on a database that already holds rows ends with the rows of a seed on an empty one."""
        seeded = {"countries": 5, "cities": 10, "writers": 8, "articles": 20}
        assert client.post("/api/db/seed").json()["counts"] == seeded
        assert client.post("/api/db/seed").json()["counts"] == seeded
        assert len(client.get("/api/articles?limit=100").json()) == 20
        assert len(client.get("/api/writers").json()) == 8
        assert len(client.get("/api/locations/countries").json()) == 5
        # the search index follows the reset: one seeded article speaks of memory, found once
        found = client.get("/api/articles/search/query?q=memory").json()
        assert len(found) == 1

    def test_schema_lists_the_model_tables_and_their_indexes(self, client: TestClient):
        """The schema view reads tables, columns and indexes through public calls."""
        data = client.get("/api/db/schema").json()
        tables = {t["name"]: t for t in data["tables"]}
        assert {"articles", "writers", "cities", "countries"} <= set(tables)
        assert {"_id", "data"} <= {c["name"] for c in tables["countries"]["columns"]}
        assert any(i["table"] == "countries" and i["columns"] == ["code"] for i in data["indices"])

    def test_schema_leaves_out_the_tables_sqler_keeps_for_itself(self, client: TestClient):
        """A table with a promoted column has sqler record it in ``_sqler_promoted``; the view
        lists the table and not the record."""
        db = get_db()
        db.ensure_table("schema_promoted", promoted={"status": "TEXT"})
        made = db.execute_sql(
            "SELECT name FROM sqlite_master WHERE name IN ('schema_promoted', '_sqler_promoted') ORDER BY name"
        )
        assert [r["name"] for r in made] == ["_sqler_promoted", "schema_promoted"]
        names = [t["name"] for t in client.get("/api/db/schema").json()["tables"]]
        assert [n for n in names if n in ("schema_promoted", "_sqler_promoted")] == [
            "schema_promoted"
        ]
