"""Async tests for update_n (batch update with RETURNING)."""

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB, F
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel


class AsyncItem(AsyncSQLerModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0
    attempts: int = 0


class AsyncVersionedItem(AsyncSQLerSafeModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0


@pytest_asyncio.fixture
async def async_db_items():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncItem.set_db(db, "items")
    await db._ensure_table("items")
    await AsyncItem(name="a", status="pending", priority=1, attempts=0).save()
    await AsyncItem(name="b", status="pending", priority=5, attempts=0).save()
    await AsyncItem(name="c", status="pending", priority=10, attempts=0).save()
    await AsyncItem(name="d", status="completed", priority=3, attempts=1).save()
    await AsyncItem(name="e", status="pending", priority=7, attempts=0).save()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def async_db_versioned_items():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncVersionedItem.set_db(db, "vitems")
    await db._ensure_table("vitems")
    await db._ensure_versioned_table("vitems")
    await AsyncVersionedItem(name="x", status="pending", priority=5).save()
    await AsyncVersionedItem(name="y", status="pending", priority=10).save()
    await AsyncVersionedItem(name="z", status="pending", priority=1).save()
    yield db
    await db.close()


@pytest.mark.asyncio
class TestAsyncUpdateNBasic:
    async def test_returns_list(self, async_db_items):
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert isinstance(results, list)
        assert len(results) == 2

    async def test_respects_limit(self, async_db_items):
        """4 pending items exist, n=2 should only update 2."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        # Remaining pending count should be 2 (4 - 2)
        remaining = await AsyncItem.query().filter(F("status") == "pending").count()
        assert remaining == 2

    async def test_ordering_respected(self, async_db_items):
        """Should update highest priority first."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        names = {r.name for r in results}
        # c (priority=10) and e (priority=7) should be claimed
        assert names == {"c", "e"}

    async def test_empty_match_returns_empty_list(self, async_db_items):
        results = await (
            AsyncItem.query()
            .filter(F("status") == "nonexistent")
            .update_n(5, status="running")
        )
        assert results == []

    async def test_n_exceeds_available(self, async_db_items):
        """n=100 but only 4 pending → returns 4."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .update_n(100, status="running")
        )
        assert len(results) == 4

    async def test_f_expression_in_update(self, async_db_items):
        """F-expressions work in batch updates."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running", attempts=F("attempts") + 1)
        )
        assert len(results) == 2
        for r in results:
            assert r.status == "running"
            assert r.attempts == 1

    async def test_returns_model_instances(self, async_db_items):
        """Results should be proper model instances with _id."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .update_n(1, status="running")
        )
        assert len(results) == 1
        inst = results[0]
        assert isinstance(inst, AsyncItem)
        assert inst._id is not None
        assert inst.status == "running"


@pytest.mark.asyncio
class TestAsyncUpdateNVersioned:
    async def test_bumps_version(self, async_db_versioned_items):
        results = await (
            AsyncVersionedItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        for r in results:
            assert r._version == 1  # initial save = 0, update_n bumps to 1


@pytest.mark.asyncio
class TestAsyncUpdateNValidation:
    async def test_raises_on_no_fields(self, async_db_items):
        with pytest.raises(ValueError, match="No fields"):
            await AsyncItem.query().update_n(2)

    async def test_raises_on_n_less_than_1(self, async_db_items):
        with pytest.raises(ValueError, match="n must be >= 1"):
            await AsyncItem.query().update_n(0, status="running")
