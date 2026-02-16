"""Async tests for multi-field order_by support."""

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.query import F


class AsyncJob(AsyncSQLerModel):
    name: str = ""
    priority: int = 0
    eta: int = 0


@pytest_asyncio.fixture
async def async_db_with_jobs():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncJob.set_db(db, "jobs")
    await db._ensure_table("jobs")
    await AsyncJob(name="low-late", priority=1, eta=100).save()
    await AsyncJob(name="high-early", priority=10, eta=10).save()
    await AsyncJob(name="high-late", priority=10, eta=100).save()
    await AsyncJob(name="low-early", priority=1, eta=10).save()
    yield db
    await db.close()


class TestAsyncMultiOrderSQL:
    def test_multi_field_sql(self):
        from sqler.query.async_query import AsyncSQLerQuery

        q = AsyncSQLerQuery("jobs").order_by("-priority", "eta", "ulid")
        sql = q.sql
        assert "ORDER BY" in sql
        assert "json_extract(data, '$.priority') DESC" in sql
        assert "json_extract(data, '$.eta')" in sql
        assert "json_extract(data, '$.ulid')" in sql

    def test_backward_compat_sql(self):
        from sqler.query.async_query import AsyncSQLerQuery

        q = AsyncSQLerQuery("jobs").order_by("name", desc=True)
        sql = q.sql
        assert "ORDER BY json_extract(data, '$.name') DESC" in sql

    def test_clone_preserves(self):
        from sqler.query.async_query import AsyncSQLerQuery

        q = AsyncSQLerQuery("jobs").order_by("-priority", "eta")
        q2 = q.filter(F("status") == "pending")
        assert q2._order_fields == [("priority", True), ("eta", False)]


@pytest.mark.asyncio
class TestAsyncMultiOrderExecution:
    async def test_multi_field_execution(self, async_db_with_jobs):
        results = await AsyncJob.query().order_by("-priority", "eta").all()
        names = [j.name for j in results]
        assert names == ["high-early", "high-late", "low-early", "low-late"]

    async def test_single_field_desc(self, async_db_with_jobs):
        results = await AsyncJob.query().order_by("priority", desc=True).all()
        priorities = [j.priority for j in results]
        assert priorities == sorted(priorities, reverse=True)

    async def test_multi_field_both_desc(self, async_db_with_jobs):
        results = await AsyncJob.query().order_by("-priority", "-eta").all()
        names = [j.name for j in results]
        assert names == ["high-late", "high-early", "low-late", "low-early"]
