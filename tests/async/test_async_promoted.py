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


@pytest.mark.asyncio
class TestAsyncPromotedAggregates:
    """Regression tests for aggregate queries on promoted columns.

    Previously, _build_aggregate_query did not call _rewrite_promoted_refs,
    so WHERE clauses on promoted columns used json_extract(data, '$.field')
    instead of the direct column. Since promoted fields are stripped from the
    JSON blob on save, json_extract returned NULL and aggregates returned 0.
    """

    async def test_count_with_promoted_filter(self, async_db_promoted):
        await AsyncJob(name="a", status="pending", priority=1).save()
        await AsyncJob(name="b", status="running", priority=5).save()
        await AsyncJob(name="c", status="pending", priority=3).save()

        count = await AsyncJob.query().filter(F("status") == "pending").count()
        assert count == 2

    async def test_count_with_multiple_promoted_filters(self, async_db_promoted):
        await AsyncJob(name="a", status="pending", priority=1).save()
        await AsyncJob(name="b", status="pending", priority=10).save()
        await AsyncJob(name="c", status="running", priority=10).save()

        count = await AsyncJob.query().filter(
            (F("status") == "pending") & (F("priority") == 10)
        ).count()
        assert count == 1

    async def test_count_with_in_list_promoted(self, async_db_promoted):
        await AsyncJob(name="a", status="pending").save()
        await AsyncJob(name="b", status="running").save()
        await AsyncJob(name="c", status="failed").save()

        count = await AsyncJob.query().filter(
            F("status").in_list(["pending", "running"])
        ).count()
        assert count == 2

    async def test_sum_on_promoted_field(self, async_db_promoted):
        await AsyncJob(name="a", status="pending", priority=3).save()
        await AsyncJob(name="b", status="pending", priority=7).save()
        await AsyncJob(name="c", status="running", priority=5).save()

        total = await AsyncJob.query().filter(F("status") == "pending").sum("priority")
        assert total == 10

    async def test_min_max_on_promoted_field(self, async_db_promoted):
        await AsyncJob(name="a", priority=1).save()
        await AsyncJob(name="b", priority=10).save()
        await AsyncJob(name="c", priority=5).save()

        mn = await AsyncJob.query().min("priority")
        mx = await AsyncJob.query().max("priority")
        assert mn == 1
        assert mx == 10

    async def test_avg_on_promoted_with_filter(self, async_db_promoted):
        await AsyncJob(name="a", status="pending", priority=2).save()
        await AsyncJob(name="b", status="pending", priority=8).save()
        await AsyncJob(name="c", status="running", priority=100).save()

        avg = await AsyncJob.query().filter(F("status") == "pending").avg("priority")
        assert avg == 5.0


@pytest.mark.asyncio
class TestAsyncPromotedDelete:
    """Regression tests for delete queries on promoted columns.

    Previously, delete() did not call _rewrite_promoted_refs, so WHERE clauses
    on promoted columns used json_extract(data, '$.field') instead of the direct
    column. Since promoted fields are stripped from the JSON blob on save,
    json_extract returned NULL and deletes silently matched nothing.
    """

    async def test_delete_with_promoted_filter(self, async_db_promoted):
        await AsyncJob(name="a", status="pending").save()
        await AsyncJob(name="b", status="running").save()
        await AsyncJob(name="c", status="pending").save()

        deleted = await AsyncJob.query().filter(F("status") == "pending").delete_all()
        assert deleted == 2

        remaining = await AsyncJob.query().all()
        assert len(remaining) == 1
        assert remaining[0].name == "b"

    async def test_delete_with_in_list_promoted(self, async_db_promoted):
        await AsyncJob(name="a", status="pending").save()
        await AsyncJob(name="b", status="running").save()
        await AsyncJob(name="c", status="failed").save()

        deleted = await AsyncJob.query().filter(
            F("status").in_list(["pending", "failed"])
        ).delete_all()
        assert deleted == 2

        remaining = await AsyncJob.query().all()
        assert len(remaining) == 1
        assert remaining[0].status == "running"

    async def test_delete_with_multiple_promoted_filters(self, async_db_promoted):
        await AsyncJob(name="a", status="pending", priority=1).save()
        await AsyncJob(name="b", status="pending", priority=10).save()
        await AsyncJob(name="c", status="running", priority=10).save()

        deleted = await AsyncJob.query().filter(
            (F("status") == "pending") & (F("priority") == 10)
        ).delete_all()
        assert deleted == 1

        remaining = await AsyncJob.query().all()
        assert len(remaining) == 2
