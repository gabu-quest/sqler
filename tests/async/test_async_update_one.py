"""Async tests for update_one with RETURNING."""

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.query import F


class AsyncTask(AsyncSQLerModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0
    attempts: int = 0


class AsyncVersionedTask(AsyncSQLerSafeModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0


@pytest_asyncio.fixture
async def async_db_tasks():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncTask.set_db(db, "tasks")
    await db._ensure_table("tasks")
    await AsyncTask(name="low", status="pending", priority=1, attempts=0).save()
    await AsyncTask(name="high", status="pending", priority=10, attempts=0).save()
    await AsyncTask(name="done", status="completed", priority=5, attempts=1).save()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def async_db_versioned():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncVersionedTask.set_db(db, "vtasks")
    await db._ensure_table("vtasks")
    await db._ensure_versioned_table("vtasks")
    await AsyncVersionedTask(name="a", status="pending", priority=5).save()
    await AsyncVersionedTask(name="b", status="pending", priority=10).save()
    yield db
    await db.close()


@pytest.mark.asyncio
class TestAsyncUpdateOneBasic:
    async def test_claims_one_row(self, async_db_tasks):
        result = await (
            AsyncTask.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running", attempts=F("attempts") + 1)
        )
        assert result is not None
        assert result.name == "high"
        assert result.status == "running"
        assert result.attempts == 1

    async def test_returns_none_no_match(self, async_db_tasks):
        result = await (
            AsyncTask.query()
            .filter(F("status") == "nonexistent")
            .update_one(status="running")
        )
        assert result is None

    async def test_only_updates_one(self, async_db_tasks):
        await (
            AsyncTask.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        pending = await AsyncTask.query().filter(F("status") == "pending").all()
        assert len(pending) == 1
        assert pending[0].name == "low"

    async def test_two_workers(self, async_db_tasks):
        w1 = await (
            AsyncTask.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        w2 = await (
            AsyncTask.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        assert w1 is not None and w2 is not None
        assert w1._id != w2._id


@pytest.mark.asyncio
class TestAsyncUpdateOneVersioned:
    async def test_auto_bumps_version(self, async_db_versioned):
        result = await (
            AsyncVersionedTask.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        assert result is not None
        assert result.name == "b"
        assert result.status == "running"
        assert result._version == 1
