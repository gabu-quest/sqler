"""Async tests for F-expression support in update()."""

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.query import F


class AsyncCounter(AsyncSQLerModel):
    count: int = 0
    name: str = ""


@pytest_asyncio.fixture
async def async_db_counters():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncCounter.set_db(db, "counters")
    await db._ensure_table("counters")
    await AsyncCounter(name="a", count=10).save()
    await AsyncCounter(name="b", count=20).save()
    yield db
    await db.close()


@pytest.mark.asyncio
class TestAsyncFExpressionExecution:
    async def test_increment(self, async_db_counters):
        await AsyncCounter.query().filter(F("name") == "a").update(count=F("count") + 1)
        a = await AsyncCounter.query().filter(F("name") == "a").first()
        assert a.count == 11

    async def test_decrement(self, async_db_counters):
        await AsyncCounter.query().filter(F("name") == "b").update(count=F("count") - 5)
        b = await AsyncCounter.query().filter(F("name") == "b").first()
        assert b.count == 15

    async def test_multiply(self, async_db_counters):
        await AsyncCounter.query().filter(F("name") == "a").update(count=F("count") * 3)
        a = await AsyncCounter.query().filter(F("name") == "a").first()
        assert a.count == 30

    async def test_compound(self, async_db_counters):
        await AsyncCounter.query().filter(F("name") == "b").update(count=F("count") * 2 + 1)
        b = await AsyncCounter.query().filter(F("name") == "b").first()
        assert b.count == 41

    async def test_mixed_literal_and_fexpr(self, async_db_counters):
        await AsyncCounter.query().filter(F("name") == "a").update(
            count=F("count") + 1, name="updated"
        )
        a = await AsyncCounter.query().filter(F("name") == "updated").first()
        assert a is not None
        assert a.count == 11

    async def test_bulk_increment(self, async_db_counters):
        rows = await AsyncCounter.query().update(count=F("count") + 100)
        assert rows == 2
        results = await AsyncCounter.query().order_by("name").all()
        assert [c.count for c in results] == [110, 120]
