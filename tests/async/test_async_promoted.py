"""Async tests for promoted columns support."""

import json

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.query import F


class AsyncJob(AsyncSQLerModel):
    __promoted__ = {
        "status": "TEXT DEFAULT 'pending'",
        "priority": "INTEGER DEFAULT 0",
    }
    name: str = ""
    status: str = "pending"
    priority: int = 0


class AsyncSafeJob(AsyncSQLerSafeModel):
    __promoted__ = {
        "status": "TEXT DEFAULT 'pending'",
        "priority": "INTEGER DEFAULT 0",
    }
    name: str = ""
    status: str = "pending"
    priority: int = 0


@pytest_asyncio.fixture
async def async_db_promoted():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncJob.set_db(db, "jobs")
    await AsyncJob._ensure_schema()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def async_db_safe_promoted():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncSafeJob.set_db(db, "safe_jobs")
    await AsyncSafeJob._ensure_schema()
    await db._ensure_versioned_table("safe_jobs")
    yield db
    await db.close()


@pytest.mark.asyncio
class TestAsyncPromotedSchema:
    async def test_promoted_columns_created(self, async_db_promoted):
        db = async_db_promoted
        cur = await db.adapter.execute('PRAGMA table_info("jobs");')
        rows = await cur.fetchall()
        await cur.close()
        cols = {r[1] for r in rows}
        assert "status" in cols
        assert "priority" in cols

    async def test_promoted_tracked_on_db(self, async_db_promoted):
        assert async_db_promoted._promoted_columns["jobs"] == ["status", "priority"]


@pytest.mark.asyncio
class TestAsyncPromotedSaveLoad:
    async def test_save_splits_promoted(self, async_db_promoted):
        db = async_db_promoted
        j = AsyncJob(name="test", status="running", priority=5)
        await j.save()

        cur = await db.adapter.execute(
            "SELECT data, status, priority FROM jobs WHERE _id = ?;", [j._id]
        )
        row = await cur.fetchone()
        await cur.close()
        data = json.loads(row[0])
        assert "status" not in data
        assert "priority" not in data
        assert row[1] == "running"
        assert row[2] == 5

    async def test_load_merges_back(self, async_db_promoted):
        j = AsyncJob(name="test", status="running", priority=5)
        await j.save()

        loaded = await AsyncJob.from_id(j._id)
        assert loaded.name == "test"
        assert loaded.status == "running"
        assert loaded.priority == 5


@pytest.mark.asyncio
class TestAsyncPromotedFilter:
    async def test_filter_on_promoted(self, async_db_promoted):
        await AsyncJob(name="a", status="pending", priority=1).save()
        await AsyncJob(name="b", status="running", priority=10).save()

        results = await AsyncJob.query().filter(F("status") == "pending").all()
        assert len(results) == 1
        assert results[0].name == "a"

    async def test_order_by_promoted(self, async_db_promoted):
        await AsyncJob(name="low", priority=1).save()
        await AsyncJob(name="high", priority=10).save()

        results = await AsyncJob.query().order_by("-priority").all()
        assert results[0].name == "high"


@pytest.mark.asyncio
class TestAsyncPromotedSafeModel:
    async def test_safe_model_save_load(self, async_db_safe_promoted):
        j = AsyncSafeJob(name="test", status="pending")
        await j.save()
        assert j._version == 0

        j.status = "running"
        await j.save()
        assert j._version == 1

        loaded = await AsyncSafeJob.from_id(j._id)
        assert loaded.status == "running"
        assert loaded._version == 1
