"""Async tests for F("_id") and F("_version") meta-column rewriting.

BUG-6: F("_id") == value silently returns no results because _id is a real
SQLite column, not stored in the JSON data blob. See test_f_meta_columns.py
for sync counterpart.
"""

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.query import F


class AsyncItem(AsyncSQLerModel):
    __tablename__ = "async_meta_items"
    name: str = ""


class AsyncSafeItem(AsyncSQLerSafeModel):
    __tablename__ = "async_meta_safe_items"
    name: str = ""


class AsyncPromotedItem(AsyncSQLerModel):
    __tablename__ = "async_meta_promoted_items"
    __promoted__ = {"status": "TEXT DEFAULT 'active'"}
    name: str = ""
    status: str = "active"


@pytest_asyncio.fixture
async def async_db():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AsyncItem.set_db(db, "async_meta_items")
    AsyncSafeItem.set_db(db, "async_meta_safe_items")
    AsyncPromotedItem.set_db(db, "async_meta_promoted_items")
    yield db
    await db.close()


@pytest_asyncio.fixture
async def saved_item(async_db):
    item = AsyncItem(name="alice")
    await item.save()
    return item


@pytest_asyncio.fixture
async def saved_safe_item(async_db):
    item = AsyncSafeItem(name="bob")
    await item.save()
    return item


@pytest.mark.asyncio
class TestAsyncFIdFilter:
    async def test_f_id_filter_returns_row(self, saved_item):
        results = await AsyncItem.query().filter(F("_id") == saved_item._id).all()
        assert len(results) == 1
        assert results[0]._id == saved_item._id
        assert results[0].name == "alice"

    async def test_f_id_filter_nonexistent(self, async_db):
        await AsyncItem(name="x").save()
        results = await AsyncItem.query().filter(F("_id") == 99999).all()
        assert len(results) == 0

    async def test_f_id_gt(self, async_db):
        a = AsyncItem(name="a")
        await a.save()
        b = AsyncItem(name="b")
        await b.save()
        c = AsyncItem(name="c")
        await c.save()
        results = await AsyncItem.query().filter(F("_id") > a._id).all()
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"b", "c"}


@pytest.mark.asyncio
class TestAsyncFIdOrderBy:
    async def test_order_by_id_asc_sql(self, async_db):
        """Verify the generated SQL uses direct _id, not json_extract."""
        await AsyncItem(name="x").save()
        qs = AsyncItem.query().order_by("_id")
        sql = qs._query.sql
        assert "ORDER BY _id" in sql
        assert "json_extract" not in sql.split("ORDER BY")[1]

    async def test_order_by_id_desc_sql(self, async_db):
        await AsyncItem(name="x").save()
        qs = AsyncItem.query().order_by("-_id")
        sql = qs._query.sql
        assert "ORDER BY _id DESC" in sql

    async def test_order_by_id_asc_behavior(self, async_db):
        # Insert c, b, a so insertion order != _id ascending name order
        c = AsyncItem(name="c")
        await c.save()
        b = AsyncItem(name="b")
        await b.save()
        a = AsyncItem(name="a")
        await a.save()
        results = await AsyncItem.query().order_by("_id").all()
        assert len(results) == 3
        assert results[0].name == "c"
        assert results[1].name == "b"
        assert results[2].name == "a"

    async def test_order_by_id_desc_behavior(self, async_db):
        c = AsyncItem(name="c")
        await c.save()
        b = AsyncItem(name="b")
        await b.save()
        a = AsyncItem(name="a")
        await a.save()
        results = await AsyncItem.query().order_by("-_id").all()
        assert len(results) == 3
        assert results[0].name == "a"
        assert results[1].name == "b"
        assert results[2].name == "c"


@pytest.mark.asyncio
class TestAsyncFIdAggregate:
    async def test_f_id_count(self, saved_item):
        count = await AsyncItem.query().filter(F("_id") == saved_item._id).count()
        assert count == 1

    async def test_f_id_count_nonexistent(self, async_db):
        await AsyncItem(name="x").save()
        count = await AsyncItem.query().filter(F("_id") == 99999).count()
        assert count == 0

    async def test_f_id_aggregate_sql(self, async_db):
        """Verify aggregate WHERE clause rewrites json_extract to _id."""
        await AsyncItem(name="x").save()
        q = AsyncItem.query().filter(F("_id") == 1)
        sql, _ = q._query._build_aggregate_query("COUNT")
        assert "_id = ?" in sql
        assert "json_extract" not in sql


@pytest.mark.asyncio
class TestAsyncFVersionFilter:
    async def test_f_version_filter(self, saved_safe_item):
        # Initial save starts at version 0
        results = await AsyncSafeItem.query().filter(F("_version") == 0).all()
        assert len(results) == 1
        assert results[0].name == "bob"
        assert results[0]._version == 0

    async def test_f_version_after_update(self, saved_safe_item):
        saved_safe_item.name = "bob2"
        await saved_safe_item.save()
        # Version bumps to 1 after first update
        results = await AsyncSafeItem.query().filter(F("_version") == 1).all()
        assert len(results) == 1
        assert results[0].name == "bob2"
        results_v0 = await AsyncSafeItem.query().filter(F("_version") == 0).all()
        assert len(results_v0) == 0


@pytest.mark.asyncio
class TestAsyncFIdWithPromotedModel:
    async def test_f_id_with_promoted(self, async_db):
        item = AsyncPromotedItem(name="promoted", status="done")
        await item.save()
        results = await AsyncPromotedItem.query().filter(F("_id") == item._id).all()
        assert len(results) == 1
        assert results[0].name == "promoted"
        assert results[0].status == "done"

    async def test_f_id_and_promoted_filter(self, async_db):
        item = AsyncPromotedItem(name="test", status="active")
        await item.save()
        results = await (
            AsyncPromotedItem.query()
            .filter((F("_id") == item._id) & (F("status") == "active"))
            .all()
        )
        assert len(results) == 1
        assert results[0].name == "test"
