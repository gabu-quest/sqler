"""Async tests for update_n (batch update with RETURNING)."""

import asyncio

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


class AsyncPromotedItem(AsyncSQLerModel):
    __promoted__ = {
        "status": "TEXT DEFAULT 'pending'",
        "priority": "INTEGER DEFAULT 0",
    }
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
async def async_db_promoted_items():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncPromotedItem.set_db(db, "pitems")
    await AsyncPromotedItem._ensure_schema()
    await AsyncPromotedItem(name="a", status="pending", priority=1, attempts=0).save()
    await AsyncPromotedItem(name="b", status="pending", priority=5, attempts=0).save()
    await AsyncPromotedItem(name="c", status="pending", priority=10, attempts=0).save()
    await AsyncPromotedItem(name="d", status="completed", priority=3, attempts=1).save()
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
    async def test_respects_limit(self, async_db_items):
        """4 pending items exist, n=2 should only update 2."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
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
        assert names == {"c", "e"}

    async def test_empty_match_returns_empty_list(self, async_db_items):
        results = await (
            AsyncItem.query()
            .filter(F("status") == "nonexistent")
            .update_n(5, status="running")
        )
        assert results == []

    async def test_n_exceeds_available(self, async_db_items):
        """n=100 but only 4 pending -> returns 4."""
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
        """Results should be proper model instances with correct fields."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(1, status="running")
        )
        assert len(results) == 1
        inst = results[0]
        assert inst.name == "c"  # highest priority pending
        assert inst.status == "running"
        assert inst._id is not None  # guard
        assert inst.priority == 10  # proof of correct identity

    async def test_n_equals_one(self, async_db_items):
        """Degenerate batch: n=1 returns single-element list."""
        results = await (
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(1, status="running")
        )
        assert len(results) == 1
        assert results[0].name == "c"
        assert results[0].status == "running"


@pytest.mark.asyncio
class TestAsyncUpdateNPromoted:
    """Verify update_n works with promoted (real SQLite column) fields."""

    async def test_updates_promoted_column(self, async_db_promoted_items):
        results = await (
            AsyncPromotedItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"b", "c"}
        for r in results:
            assert r.status == "running"

    async def test_updates_promoted_with_f_expression(self, async_db_promoted_items):
        results = await (
            AsyncPromotedItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, priority=F("priority") + 100)
        )
        assert len(results) == 2
        for r in results:
            assert r.priority >= 105  # was 5 or 10, now +100

    async def test_filters_on_promoted_column(self, async_db_promoted_items):
        """WHERE clause uses promoted column directly."""
        results = await (
            AsyncPromotedItem.query()
            .filter((F("status") == "pending") & (F("priority") >= 5))
            .update_n(10, status="claimed")
        )
        assert len(results) == 2
        for r in results:
            assert r.status == "claimed"
            assert r.priority >= 5


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
        assert {r.name for r in results} == {"x", "y"}
        for r in results:
            assert r._version == 1  # initial save = 0, update_n bumps to 1
            assert r.status == "running"


@pytest.mark.asyncio
class TestAsyncUpdateNConcurrency:
    async def test_no_overlap_concurrent_callers(self, async_db_items):
        """Two concurrent update_n(2) calls should claim disjoint rows."""
        claimed_a, claimed_b = await asyncio.gather(
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running_a"),
            AsyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running_b"),
        )
        ids_a = {r._id for r in claimed_a}
        ids_b = {r._id for r in claimed_b}
        assert ids_a.isdisjoint(ids_b), (
            f"Overlap detected: {ids_a & ids_b}"
        )
        assert len(ids_a) + len(ids_b) == 4  # all 4 pending claimed


@pytest.mark.asyncio
class TestAsyncUpdateNValidation:
    async def test_raises_on_no_fields(self, async_db_items):
        with pytest.raises(ValueError, match="No fields"):
            await AsyncItem.query().update_n(2)

    async def test_raises_on_n_less_than_1(self, async_db_items):
        with pytest.raises(ValueError, match="n must be >= 1"):
            await AsyncItem.query().update_n(0, status="running")
