"""Tests for FastAPI example routers.

Tests the new feature demo routers:
- Articles (FTS)
- Soft Delete
- Audit
- Change Tracking
- Metrics

Also validates Pydantic v2 schema behavior with _id/_version fields.
"""

import sys
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

# Ensure repo root is importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.fastapi.app import app  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TestPydanticV2Schemas:
    """Verify _id and _version fields are properly serialized in responses."""

    @pytest.mark.anyio("asyncio")
    async def test_soft_delete_item_has_id_in_response(self):
        """ItemOut schema should include _id and _version fields."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # Create an item
                resp = await c.post(
                    "/api/items",
                    json={"name": "Test Item", "category": "test", "description": "Testing"},
                )
                assert resp.status_code == 201
                data = resp.json()

                # Verify _id and _version are in response
                assert "_id" in data, f"Expected _id in response, got: {data.keys()}"
                assert "_version" in data, f"Expected _version in response, got: {data.keys()}"
                assert data["_id"] == 1
                assert data["_version"] == 0

    @pytest.mark.anyio("asyncio")
    async def test_article_has_id_in_response(self):
        """ArticleOut schema should include _id and _version fields."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/articles",
                    json={
                        "title": "Test Article",
                        "content": "Test content",
                        "author": "Tester",
                        "tags": ["test"],
                    },
                )
                assert resp.status_code == 201
                data = resp.json()

                assert "_id" in data, f"Expected _id in response, got: {data.keys()}"
                assert "_version" in data

    @pytest.mark.anyio("asyncio")
    async def test_product_has_id_in_response(self):
        """ProductOut schema should include _id and _version fields."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/products",
                    json={"name": "Widget", "price": 9.99, "sku": "WGT-001"},
                )
                assert resp.status_code == 201
                data = resp.json()

                assert "_id" in data, f"Expected _id in response, got: {data.keys()}"
                assert "_version" in data

    @pytest.mark.anyio("asyncio")
    async def test_tracked_user_has_id_in_response(self):
        """TrackedUserOut schema should include _id and _version fields."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/tracked-users",
                    json={"name": "Alice", "email": "alice@example.com"},
                )
                assert resp.status_code == 201
                data = resp.json()

                assert "_id" in data, f"Expected _id in response, got: {data.keys()}"
                assert "_version" in data


class TestSoftDeleteRouter:
    """Test soft delete operations."""

    @pytest.mark.anyio("asyncio")
    async def test_soft_delete_and_restore_cycle(self):
        """Test complete soft delete -> restore cycle."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # Create item
                resp = await c.post(
                    "/api/items",
                    json={"name": "Restorable Item", "category": "test"},
                )
                assert resp.status_code == 201
                item_id = resp.json()["_id"]

                # Soft delete
                resp = await c.delete(f"/api/items/{item_id}")
                assert resp.status_code == 200
                assert resp.json()["success"] is True

                # Verify in deleted list
                resp = await c.get("/api/items", params={"filter": "deleted"})
                assert resp.status_code == 200
                deleted = resp.json()
                assert any(i["_id"] == item_id for i in deleted)

                # Restore
                resp = await c.post(f"/api/items/{item_id}/restore")
                assert resp.status_code == 200
                restored = resp.json()
                assert restored["_id"] == item_id
                assert restored["is_deleted"] is False

                # Verify in active list
                resp = await c.get("/api/items", params={"filter": "active"})
                assert resp.status_code == 200
                active = resp.json()
                assert any(i["_id"] == item_id for i in active)

    @pytest.mark.anyio("asyncio")
    async def test_hard_delete(self):
        """Test permanent hard delete."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # Create and soft delete
                resp = await c.post(
                    "/api/items",
                    json={"name": "Deletable", "category": "test"},
                )
                item_id = resp.json()["_id"]
                await c.delete(f"/api/items/{item_id}")

                # Hard delete
                resp = await c.delete(f"/api/items/{item_id}/hard")
                assert resp.status_code == 200

                # Verify gone from all lists
                resp = await c.get("/api/items", params={"filter": "all"})
                all_items = resp.json()
                assert not any(i["_id"] == item_id for i in all_items)

    @pytest.mark.anyio("asyncio")
    async def test_hooks_normalize_name(self):
        """Test HooksMixin before_save strips whitespace."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # Create with extra whitespace
                resp = await c.post(
                    "/api/items",
                    json={"name": "  Padded Name  ", "category": "test"},
                )
                assert resp.status_code == 201
                # Hook should have stripped whitespace
                assert resp.json()["name"] == "Padded Name"


class TestArticlesRouter:
    """Test full-text search functionality."""

    @pytest.mark.anyio("asyncio")
    async def test_fts_search(self):
        """Test FTS search returns results with scores."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # Create articles
                await c.post(
                    "/api/articles",
                    json={
                        "title": "SQLite FTS Tutorial",
                        "content": "Full-text search with SQLite is powerful",
                        "author": "Expert",
                        "tags": ["sqlite", "fts"],
                    },
                )
                await c.post(
                    "/api/articles",
                    json={
                        "title": "Python Basics",
                        "content": "Python is a great language",
                        "author": "Teacher",
                        "tags": ["python"],
                    },
                )

                # Search for sqlite
                resp = await c.get("/api/articles/search/query", params={"q": "sqlite"})
                assert resp.status_code == 200
                results = resp.json()

                # Should find the SQLite article
                assert len(results) >= 1
                assert any("sqlite" in r["article"]["title"].lower() for r in results)
                # Should have score
                assert all("score" in r for r in results)


class TestMetricsRouter:
    """Test metrics and monitoring endpoints."""

    @pytest.mark.anyio("asyncio")
    async def test_db_health(self):
        """Test health check endpoint."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/api/db/health")
                assert resp.status_code == 200
                data = resp.json()
                assert data["healthy"] is True
                assert "latency_ms" in data

    @pytest.mark.anyio("asyncio")
    async def test_db_stats(self):
        """Test database stats endpoint."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/api/db/stats")
                assert resp.status_code == 200
                data = resp.json()
                assert "table_count" in data
                assert "index_count" in data

    @pytest.mark.anyio("asyncio")
    async def test_metrics_endpoint(self):
        """Test metrics collector endpoint."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/api/metrics")
                assert resp.status_code == 200
                data = resp.json()
                assert "queries" in data
                assert "tables" in data

    @pytest.mark.anyio("asyncio")
    async def test_cache_stats(self):
        """Test cache stats endpoint."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/api/metrics/cache/stats")
                assert resp.status_code == 200
                data = resp.json()
                assert "size" in data
                assert "hits" in data
                assert "misses" in data


class TestTrackedUsersRouter:
    """Test change tracking functionality."""

    @pytest.mark.anyio("asyncio")
    async def test_partial_update(self):
        """Test partial update only changes specified fields."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # Create user
                resp = await c.post(
                    "/api/tracked-users",
                    json={
                        "name": "Bob",
                        "email": "bob@example.com",
                        "bio": "Original bio",
                        "role": "user",
                    },
                )
                user_id = resp.json()["_id"]

                # Partial update - only bio
                resp = await c.patch(
                    f"/api/tracked-users/{user_id}",
                    json={"bio": "Updated bio"},
                )
                assert resp.status_code == 200
                updated = resp.json()

                # Bio changed, others unchanged
                assert updated["bio"] == "Updated bio"
                assert updated["name"] == "Bob"
                assert updated["email"] == "bob@example.com"

    @pytest.mark.anyio("asyncio")
    async def test_diff_users(self):
        """Test diff between two users."""
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # Create two users with differences
                resp1 = await c.post(
                    "/api/tracked-users",
                    json={"name": "User1", "email": "u1@example.com", "role": "admin"},
                )
                id1 = resp1.json()["_id"]

                resp2 = await c.post(
                    "/api/tracked-users",
                    json={"name": "User2", "email": "u2@example.com", "role": "user"},
                )
                id2 = resp2.json()["_id"]

                # Get diff
                resp = await c.get(f"/api/tracked-users/{id1}/diff/{id2}")
                assert resp.status_code == 200
                diff = resp.json()

                assert diff["is_equal"] is False
                assert "differences" in diff
