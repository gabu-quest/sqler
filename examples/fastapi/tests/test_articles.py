"""Tests for Articles API.

Tests CRUD, FTS search, and audit logging.
"""

from fastapi.testclient import TestClient


class TestArticlesCRUD:
    """Article CRUD tests."""

    def test_create_article_with_writer(self, client: TestClient, writer_haruki: dict):
        """Create an article with writer reference."""
        resp = client.post(
            "/api/articles",
            json={
                "title": "Memory and the Written Word",
                "content": "Writing is an act of remembering.",
                "tags": ["writing", "philosophy"],
                "writer_id": writer_haruki["_id"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Memory and the Written Word"
        assert data["content"] == "Writing is an act of remembering."
        assert data["tags"] == ["writing", "philosophy"]
        assert data["writer"]["_id"] == writer_haruki["_id"]
        assert data["writer"]["name"] == "Haruki Tanaka"
        assert "_id" in data
        assert "_version" in data

    def test_create_article_without_writer(self, client: TestClient):
        """Create an article without writer (optional field)."""
        resp = client.post(
            "/api/articles",
            json={
                "title": "Anonymous Article",
                "content": "Content without author.",
                "tags": ["misc"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Anonymous Article"
        assert data["writer"] is None

    def test_create_article_invalid_writer(self, client: TestClient):
        """Create article with non-existent writer fails."""
        resp = client.post(
            "/api/articles",
            json={
                "title": "Ghost Article",
                "content": "By nobody",
                "tags": [],
                "writer_id": 99999,
            },
        )
        assert resp.status_code == 404
        assert "writer not found" in resp.json()["detail"].lower()

    def test_list_articles(self, client: TestClient, article_silence: dict):
        """List all articles."""
        resp = client.get("/api/articles")
        assert resp.status_code == 200
        articles = resp.json()
        assert len(articles) >= 1
        titles = [a["title"] for a in articles]
        assert "The Art of Silence" in titles

    def test_list_articles_filter_by_writer(self, client: TestClient, writer_haruki: dict, article_silence: dict):
        """List articles filtered by writer."""
        resp = client.get(f"/api/articles?writer_id={writer_haruki['_id']}")
        assert resp.status_code == 200
        articles = resp.json()
        assert len(articles) >= 1
        for article in articles:
            assert article["writer"]["_id"] == writer_haruki["_id"]

    def test_get_article_by_id(self, client: TestClient, article_silence: dict):
        """Get a specific article by ID."""
        resp = client.get(f"/api/articles/{article_silence['_id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "The Art of Silence"
        assert data["writer"]["name"] == "Haruki Tanaka"

    def test_get_article_not_found(self, client: TestClient):
        """Get non-existent article returns 404."""
        resp = client.get("/api/articles/99999")
        assert resp.status_code == 404

    def test_patch_article_title(self, client: TestClient, article_silence: dict):
        """Update article's title."""
        resp = client.patch(
            f"/api/articles/{article_silence['_id']}",
            json={"title": "The Art of Silence (Updated)"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "The Art of Silence (Updated)"
        # Content and writer should be unchanged
        assert "negative space" in data["content"]
        assert data["writer"]["name"] == "Haruki Tanaka"

    def test_patch_article_writer(self, client: TestClient, article_silence: dict, city_tokyo: dict):
        """Update article's writer."""
        # Create another writer
        new_writer_resp = client.post(
            "/api/writers",
            json={"name": "New Writer", "bio": "Test", "city_id": city_tokyo["_id"]},
        )
        new_writer_id = new_writer_resp.json()["_id"]

        resp = client.patch(
            f"/api/articles/{article_silence['_id']}",
            json={"writer_id": new_writer_id},
        )
        assert resp.status_code == 200
        assert resp.json()["writer"]["_id"] == new_writer_id

    def test_patch_article_tags(self, client: TestClient, article_silence: dict):
        """Update article's tags."""
        resp = client.patch(
            f"/api/articles/{article_silence['_id']}",
            json={"tags": ["updated", "tags", "list"]},
        )
        assert resp.status_code == 200
        assert resp.json()["tags"] == ["updated", "tags", "list"]

    def test_delete_article(self, client: TestClient, writer_haruki: dict):
        """Delete an article."""
        # Create article
        create_resp = client.post(
            "/api/articles",
            json={
                "title": "Deletable Article",
                "content": "Will be deleted",
                "tags": [],
                "writer_id": writer_haruki["_id"],
            },
        )
        article_id = create_resp.json()["_id"]

        # Delete
        del_resp = client.delete(f"/api/articles/{article_id}")
        assert del_resp.status_code == 200

        # Verify deleted
        get_resp = client.get(f"/api/articles/{article_id}")
        assert get_resp.status_code == 404


class TestArticleFTS:
    """Full-text search tests."""

    def test_search_by_title(self, client: TestClient, article_silence: dict):
        """Search articles by title content."""
        resp = client.get("/api/articles/search/query?q=silence")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        # Should find the silence article
        titles = [r["article"]["title"] for r in results]
        assert any("Silence" in t for t in titles)

    def test_search_by_content(self, client: TestClient, article_silence: dict):
        """Search articles by content."""
        resp = client.get("/api/articles/search/query?q=negative+space")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        # Results should have scores
        for r in results:
            assert "score" in r
            assert r["score"] is not None or r["score"] == 0

    def test_search_with_highlights(self, client: TestClient, article_silence: dict):
        """Search returns highlights."""
        resp = client.get("/api/articles/search/query?q=Japanese&highlight=true")
        assert resp.status_code == 200
        results = resp.json()
        if len(results) > 0:
            # Highlights should be present
            for r in results:
                assert "highlights" in r

    def test_search_no_results(self, client: TestClient):
        """Search with no matches returns empty list."""
        resp = client.get("/api/articles/search/query?q=xyznonexistent123")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_count(self, client: TestClient, article_silence: dict):
        """Count search results."""
        resp = client.get("/api/articles/search/count?q=silence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "silence"
        assert data["count"] >= 1

    def test_rebuild_fts_index(self, client: TestClient):
        """Rebuild FTS index."""
        resp = client.post("/api/articles/fts/rebuild")
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestArticleAuditLog:
    """Article audit log tests."""

    def test_audit_log_on_create(self, client: TestClient, writer_haruki: dict):
        """Audit log records creation."""
        # Create article
        create_resp = client.post(
            "/api/articles",
            json={
                "title": "Audit Test Article",
                "content": "Testing audit",
                "tags": [],
                "writer_id": writer_haruki["_id"],
            },
        )
        article_id = create_resp.json()["_id"]

        # Check audit log
        log_resp = client.get(f"/api/articles/{article_id}/audit-log")
        assert log_resp.status_code == 200
        log = log_resp.json()
        assert len(log) >= 1
        assert log[0]["action"] == "create"

    def test_audit_log_on_update(self, client: TestClient, writer_haruki: dict):
        """Audit log records updates with changes."""
        # Create article
        create_resp = client.post(
            "/api/articles",
            json={
                "title": "Original Title",
                "content": "Original content",
                "tags": [],
                "writer_id": writer_haruki["_id"],
            },
        )
        article_id = create_resp.json()["_id"]

        # Update article
        client.patch(f"/api/articles/{article_id}", json={"title": "Updated Title"})

        # Check audit log
        log_resp = client.get(f"/api/articles/{article_id}/audit-log")
        log = log_resp.json()
        assert len(log) >= 2

        # Find the update entry
        update_entries = [e for e in log if e["action"] == "update"]
        assert len(update_entries) >= 1
        update = update_entries[-1]
        assert "changes" in update
        assert "title" in update["changes"]
        assert update["changes"]["title"]["old"] == "Original Title"
        assert update["changes"]["title"]["new"] == "Updated Title"
