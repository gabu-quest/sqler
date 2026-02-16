"""Tests for Writers API.

Tests CRUD, city relationships, and audit logging.
"""

from fastapi.testclient import TestClient


class TestWritersCRUD:
    """Writer CRUD tests."""

    def test_create_writer_with_city(self, client: TestClient, city_tokyo: dict):
        """Create a writer with city reference."""
        resp = client.post(
            "/api/writers",
            json={
                "name": "Yuki Yamamoto",
                "bio": "Technology journalist",
                "city_id": city_tokyo["_id"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Yuki Yamamoto"
        assert data["bio"] == "Technology journalist"
        assert data["city"]["_id"] == city_tokyo["_id"]
        assert data["city"]["name"] == "Tokyo"
        assert "_id" in data
        assert "_version" in data

    def test_create_writer_without_city(self, client: TestClient):
        """Create a writer without city (optional field)."""
        resp = client.post(
            "/api/writers",
            json={
                "name": "Anonymous Writer",
                "bio": "Mystery author",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Anonymous Writer"
        assert data["city"] is None

    def test_create_writer_invalid_city(self, client: TestClient):
        """Create writer with non-existent city fails."""
        resp = client.post(
            "/api/writers",
            json={
                "name": "Ghost Writer",
                "bio": "Unknown",
                "city_id": 99999,
            },
        )
        assert resp.status_code == 404
        assert "city not found" in resp.json()["detail"].lower()

    def test_list_writers(self, client: TestClient, writer_haruki: dict):
        """List all writers."""
        resp = client.get("/api/writers")
        assert resp.status_code == 200
        writers = resp.json()
        assert len(writers) >= 1
        names = [w["name"] for w in writers]
        assert "Haruki Tanaka" in names

    def test_get_writer_by_id(self, client: TestClient, writer_haruki: dict):
        """Get a specific writer by ID."""
        resp = client.get(f"/api/writers/{writer_haruki['_id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Haruki Tanaka"
        assert data["city"]["name"] == "Kyoto"

    def test_get_writer_not_found(self, client: TestClient):
        """Get non-existent writer returns 404."""
        resp = client.get("/api/writers/99999")
        assert resp.status_code == 404

    def test_patch_writer_name(self, client: TestClient, writer_haruki: dict):
        """Update writer's name."""
        resp = client.patch(
            f"/api/writers/{writer_haruki['_id']}",
            json={"name": "Haruki T."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Haruki T."
        # Bio and city should be unchanged
        assert data["bio"] == "Award-winning novelist"
        assert data["city"]["name"] == "Kyoto"

    def test_patch_writer_city(self, client: TestClient, writer_haruki: dict, city_tokyo: dict):
        """Update writer's city."""
        resp = client.patch(
            f"/api/writers/{writer_haruki['_id']}",
            json={"city_id": city_tokyo["_id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["city"]["_id"] == city_tokyo["_id"]
        assert data["city"]["name"] == "Tokyo"

    def test_patch_writer_remove_city(self, client: TestClient, city_tokyo: dict):
        """Remove writer's city by setting to null."""
        # Create writer with city
        create_resp = client.post(
            "/api/writers",
            json={"name": "Temp Writer", "bio": "Test", "city_id": city_tokyo["_id"]},
        )
        writer_id = create_resp.json()["_id"]

        # Remove city
        resp = client.patch(f"/api/writers/{writer_id}", json={"city_id": None})
        assert resp.status_code == 200
        assert resp.json()["city"] is None

    def test_delete_writer_no_articles(self, client: TestClient, city_tokyo: dict):
        """Delete a writer with no articles."""
        # Create writer with no articles
        create_resp = client.post(
            "/api/writers",
            json={"name": "Deletable Writer", "bio": "Will be deleted", "city_id": city_tokyo["_id"]},
        )
        writer_id = create_resp.json()["_id"]

        # Delete should succeed
        del_resp = client.delete(f"/api/writers/{writer_id}")
        assert del_resp.status_code == 200

        # Verify deleted
        get_resp = client.get(f"/api/writers/{writer_id}")
        assert get_resp.status_code == 404

    def test_delete_writer_with_articles_fails(self, client: TestClient, article_silence: dict):
        """Cannot delete a writer that has articles (dependency check)."""
        writer_id = article_silence["writer"]["_id"]
        resp = client.delete(f"/api/writers/{writer_id}")
        assert resp.status_code == 409
        # Accept either custom message or SQLer's built-in message
        detail = resp.json()["detail"].lower()
        assert "articles depend" in detail or "referenced by" in detail


class TestWriterAuditLog:
    """Writer audit log tests."""

    def test_audit_log_on_create(self, client: TestClient, city_tokyo: dict):
        """Audit log records creation."""
        # Create writer
        create_resp = client.post(
            "/api/writers",
            json={"name": "Audit Test Writer", "bio": "Testing audit", "city_id": city_tokyo["_id"]},
        )
        writer_id = create_resp.json()["_id"]

        # Check audit log
        log_resp = client.get(f"/api/writers/{writer_id}/audit-log")
        assert log_resp.status_code == 200
        log = log_resp.json()
        assert len(log) >= 1
        assert log[0]["action"] == "create"

    def test_audit_log_on_update(self, client: TestClient, city_tokyo: dict):
        """Audit log records updates with changes."""
        # Create writer
        create_resp = client.post(
            "/api/writers",
            json={"name": "Update Test", "bio": "Original bio", "city_id": city_tokyo["_id"]},
        )
        writer_id = create_resp.json()["_id"]

        # Update writer
        client.patch(f"/api/writers/{writer_id}", json={"bio": "Updated bio"})

        # Check audit log
        log_resp = client.get(f"/api/writers/{writer_id}/audit-log")
        log = log_resp.json()
        assert len(log) >= 2

        # Find the update entry
        update_entries = [e for e in log if e["action"] == "update"]
        assert len(update_entries) >= 1
        update = update_entries[-1]
        assert "changes" in update
        assert "bio" in update["changes"]
        assert update["changes"]["bio"]["old"] == "Original bio"
        assert update["changes"]["bio"]["new"] == "Updated bio"


class TestWriterArticles:
    """Writer's articles endpoint tests."""

    def test_get_writer_articles(self, client: TestClient, writer_haruki: dict, article_silence: dict):
        """Get articles written by a specific writer."""
        resp = client.get(f"/api/writers/{writer_haruki['_id']}/articles")
        assert resp.status_code == 200
        articles = resp.json()
        assert len(articles) >= 1
        assert articles[0]["title"] == "The Art of Silence"

    def test_get_writer_articles_empty(self, client: TestClient, city_tokyo: dict):
        """Get articles for writer with no articles."""
        # Create writer with no articles
        create_resp = client.post(
            "/api/writers",
            json={"name": "No Articles Writer", "bio": "Has no articles", "city_id": city_tokyo["_id"]},
        )
        writer_id = create_resp.json()["_id"]

        resp = client.get(f"/api/writers/{writer_id}/articles")
        assert resp.status_code == 200
        assert resp.json() == []
