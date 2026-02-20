"""Tests for AsyncSQLerLiteModel.using() — per-query DB binding without class mutation.

Mirrors the async Pydantic using() API. Async lite safe models inherit using()
from their parent so they're tested too.
"""

from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.query import F


@dataclass
class AsyncWidget(AsyncSQLerLiteModel):
    __tablename__ = "async_widgets"
    name: str
    color: str = "red"


@dataclass
class AsyncSafeWidget(AsyncSQLerLiteSafeModel):
    __tablename__ = "async_safe_widgets"
    name: str
    color: str = "blue"


@pytest_asyncio.fixture
async def adb():
    """Create an in-memory async DB with widget tables."""
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    await db._ensure_table("async_widgets")
    await db._ensure_table("async_safe_widgets")
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def adb2():
    """Second independent async DB."""
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    await db._ensure_table("async_widgets")
    await db._ensure_table("async_safe_widgets")
    try:
        yield db
    finally:
        await db.close()


async def seed_async(db, table, items):
    """Insert items into async db."""
    ids = []
    for payload in items:
        _id = await db.upsert_document(table, None, payload)
        ids.append(_id)
    return ids


@pytest.mark.asyncio
class TestAsyncLiteUsingAll:
    """using(db).all() returns correct results."""

    async def test_all_returns_seeded_items(self, adb):
        await seed_async(adb, "async_widgets", [
            {"name": "bolt", "color": "silver"},
            {"name": "nut", "color": "brass"},
        ])

        results = await AsyncWidget.using(adb).all()

        assert len(results) == 2
        names = sorted(w.name for w in results)
        assert names == ["bolt", "nut"]

    async def test_all_empty_db_returns_empty(self, adb):
        results = await AsyncWidget.using(adb).all()
        assert results == []


@pytest.mark.asyncio
class TestAsyncLiteUsingFilter:
    """using(db).filter(...) works correctly."""

    async def test_filter_by_field(self, adb):
        await seed_async(adb, "async_widgets", [
            {"name": "a", "color": "red"},
            {"name": "b", "color": "blue"},
            {"name": "c", "color": "red"},
        ])

        reds = await AsyncWidget.using(adb).filter(F("color") == "red").all()

        assert len(reds) == 2
        assert all(w.color == "red" for w in reds)

    async def test_filter_no_match(self, adb):
        await seed_async(adb, "async_widgets", [{"name": "x", "color": "red"}])

        results = await AsyncWidget.using(adb).filter(F("color") == "green").all()
        assert results == []


@pytest.mark.asyncio
class TestAsyncLiteUsingSafe:
    """AsyncSafeModel inherits using() from parent and it works."""

    async def test_safe_using_all(self, adb):
        await seed_async(adb, "async_safe_widgets", [
            {"name": "sw1", "color": "blue"},
            {"name": "sw2", "color": "green"},
        ])

        results = await AsyncSafeWidget.using(adb).all()

        assert len(results) == 2
        names = sorted(w.name for w in results)
        assert names == ["sw1", "sw2"]


@pytest.mark.asyncio
class TestAsyncLiteUsingNoClassMutation:
    """using(db) must not mutate class-level state."""

    async def test_class_db_unchanged_after_using(self, adb, adb2):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            AsyncWidget.set_db(adb)

        original_db = AsyncWidget._db

        await AsyncWidget.using(adb2).all()

        assert AsyncWidget._db is original_db
        assert AsyncWidget._db is adb


@pytest.mark.asyncio
class TestAsyncLiteUsingCrossDbIsolation:
    """Items in db1 are not visible via using(db2)."""

    async def test_items_isolated_between_dbs(self, adb, adb2):
        await seed_async(adb, "async_widgets", [
            {"name": "only_in_db1", "color": "red"},
        ])
        await seed_async(adb2, "async_widgets", [
            {"name": "only_in_db2", "color": "blue"},
            {"name": "also_in_db2", "color": "green"},
        ])

        r1 = await AsyncWidget.using(adb).all()
        r2 = await AsyncWidget.using(adb2).all()

        assert len(r1) == 1
        assert r1[0].name == "only_in_db1"
        assert len(r2) == 2
        names_db2 = sorted(w.name for w in r2)
        assert names_db2 == ["also_in_db2", "only_in_db2"]

    async def test_empty_db_returns_nothing(self, adb, adb2):
        await seed_async(adb, "async_widgets", [{"name": "exists", "color": "red"}])

        assert await AsyncWidget.using(adb2).all() == []
        assert len(await AsyncWidget.using(adb).all()) == 1


@pytest.mark.asyncio
class TestAsyncLiteUsingChaining:
    """Queryset chaining works with using()."""

    async def test_order_by(self, adb):
        await seed_async(adb, "async_widgets", [
            {"name": "c_widget", "color": "red"},
            {"name": "a_widget", "color": "blue"},
            {"name": "b_widget", "color": "green"},
        ])

        results = await AsyncWidget.using(adb).order_by("name").all()

        assert len(results) == 3
        assert [w.name for w in results] == ["a_widget", "b_widget", "c_widget"]

    async def test_limit(self, adb):
        await seed_async(adb, "async_widgets", [
            {"name": f"w{i}", "color": "red"} for i in range(10)
        ])

        results = await AsyncWidget.using(adb).limit(3).all()
        assert len(results) == 3

    async def test_count(self, adb):
        await seed_async(adb, "async_widgets", [
            {"name": f"w{i}", "color": "red"} for i in range(7)
        ])

        assert await AsyncWidget.using(adb).count() == 7

    async def test_first(self, adb):
        await seed_async(adb, "async_widgets", [{"name": "solo", "color": "red"}])

        result = await AsyncWidget.using(adb).first()
        assert result is not None
        assert result.name == "solo"
