"""Tests for AsyncSQLerLiteModel.asave_many() and AsyncSQLerLiteSafeModel.asave_many()."""

import sqlite3
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel

# ---- test models ----

@dataclass
class ALItem(AsyncSQLerLiteModel):
    __tablename__ = "lite_items"
    name: str = ""
    value: int = 0


@dataclass
class ALPromotedItem(AsyncSQLerLiteModel):
    __tablename__ = "lite_promoted_items"
    __promoted__ = {
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "priority": "INTEGER DEFAULT 0",
    }
    __checks__ = {
        "valid_status": "status IN ('active', 'inactive', 'archived')",
    }
    name: str = ""
    status: str = "active"
    priority: int = 0


@dataclass
class ALSafeItem(AsyncSQLerLiteSafeModel):
    __tablename__ = "lite_safe_items"
    name: str = ""
    value: int = 0


@dataclass
class ALSafePromotedItem(AsyncSQLerLiteSafeModel):
    __tablename__ = "lite_safe_promoted_items"
    __promoted__ = {
        "status": "TEXT NOT NULL DEFAULT 'pending'",
    }
    name: str = ""
    status: str = "pending"


# ---- fixtures ----

@pytest_asyncio.fixture
async def db():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


# ---- AsyncSQLerLiteModel.asave_many tests ----

@pytest.mark.asyncio
async def test_asave_many_basic(db):
    ALItem.set_db(db)
    try:
        items = [ALItem(name=f"item-{i}", value=i) for i in range(5)]
        result = await ALItem.asave_many(items)

        assert result is items
        assert len(result) == 5
        ids = [item._id for item in result]
        assert all(isinstance(id_, int) for id_ in ids)
        assert len(set(ids)) == 5
        assert ids == list(range(ids[0], ids[0] + 5))
    finally:
        ALItem._db = None
        ALItem._table = None


@pytest.mark.asyncio
async def test_asave_many_empty_list(db):
    ALItem.set_db(db)
    try:
        result = await ALItem.asave_many([])
        assert result == []
    finally:
        ALItem._db = None
        ALItem._table = None


@pytest.mark.asyncio
async def test_asave_many_rejects_existing(db):
    ALItem.set_db(db)
    try:
        item = ALItem(name="existing")
        await item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            await ALItem.asave_many([item])
    finally:
        ALItem._db = None
        ALItem._table = None


@pytest.mark.asyncio
async def test_asave_many_with_promoted(db):
    ALPromotedItem.set_db(db)
    try:
        items = [
            ALPromotedItem(name="a", status="active", priority=1),
            ALPromotedItem(name="b", status="inactive", priority=2),
            ALPromotedItem(name="c", status="archived", priority=3),
        ]
        await ALPromotedItem.asave_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))

        found = await ALPromotedItem.query().order_by("priority").all()
        assert len(found) == 3
        assert found[0].name == "a"
        assert found[0].status == "active"
        assert found[0].priority == 1
        assert found[1].name == "b"
        assert found[1].status == "inactive"
        assert found[2].name == "c"
        assert found[2].status == "archived"
        assert found[2].priority == 3
    finally:
        ALPromotedItem._db = None
        ALPromotedItem._table = None


@pytest.mark.asyncio
async def test_asave_many_atomicity_promoted(db):
    """If one row violates a CHECK constraint, no rows should be saved."""
    ALPromotedItem.set_db(db)
    try:
        items = [
            ALPromotedItem(name="good", status="active", priority=1),
            ALPromotedItem(name="bad", status="INVALID_STATUS", priority=2),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            await ALPromotedItem.asave_many(items)

        count = await ALPromotedItem.query().count()
        assert count == 0
    finally:
        ALPromotedItem._db = None
        ALPromotedItem._table = None


@pytest.mark.asyncio
async def test_asave_many_roundtrip(db):
    ALItem.set_db(db)
    try:
        items = [ALItem(name=f"rt-{i}", value=i * 10) for i in range(3)]
        await ALItem.asave_many(items)

        for item in items:
            loaded = await ALItem.from_id(item._id)
            assert loaded is not None
            assert loaded.name == item.name
            assert loaded.value == item.value
            assert loaded._id == item._id
    finally:
        ALItem._db = None
        ALItem._table = None


@pytest.mark.asyncio
async def test_asave_many_with_db_param(db):
    """Verify asave_many works with explicit db= parameter instead of class binding."""
    table = ALItem.__tablename__
    items = [ALItem(name=f"db-{i}", value=i) for i in range(3)]
    ALItem._table = table
    try:
        await ALItem.asave_many(items, db=db)
        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))
    finally:
        ALItem._table = None


@pytest.mark.asyncio
async def test_asave_many_atomicity(db):
    """If one row violates a UNIQUE constraint, no rows should be saved."""
    ALItem.set_db(db)
    try:
        await ALItem.add_index("name", unique=True)
        existing = ALItem(name="duplicate")
        await existing.save()

        items = [
            ALItem(name="good", value=1),
            ALItem(name="duplicate", value=2),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            await ALItem.asave_many(items)

        count = await ALItem.query().count()
        assert count == 1
    finally:
        ALItem._db = None
        ALItem._table = None


@pytest.mark.asyncio
async def test_asave_many_chunking(db):
    """Insert enough rows to trigger chunking (>999 params)."""
    ALItem.set_db(db)
    try:
        items = [ALItem(name=f"chunk-{i}", value=i) for i in range(1005)]
        await ALItem.asave_many(items)

        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert len(set(ids)) == 1005
        assert ids == list(range(ids[0], ids[0] + 1005))
    finally:
        ALItem._db = None
        ALItem._table = None


# ---- AsyncSQLerLiteSafeModel.asave_many tests ----

@pytest.mark.asyncio
async def test_asave_many_safe_model(db):
    ALSafeItem.set_db(db)
    try:
        items = [ALSafeItem(name=f"safe-{i}", value=i) for i in range(5)]
        result = await ALSafeItem.asave_many(items)

        assert result is items
        assert len(result) == 5
        assert all(item._id is not None for item in items)
        assert all(item._version == 0 for item in items)
        for item in items:
            assert item._snapshot is not None
            assert set(item._snapshot.keys()) == {"name", "value"}
            assert item._snapshot["name"] == item.name
            assert item._snapshot["value"] == item.value

        ids = [item._id for item in items]
        assert len(set(ids)) == 5
        assert ids == list(range(ids[0], ids[0] + 5))
    finally:
        ALSafeItem._db = None
        ALSafeItem._table = None


@pytest.mark.asyncio
async def test_asave_many_safe_model_with_promoted(db):
    ALSafePromotedItem.set_db(db)
    try:
        items = [
            ALSafePromotedItem(name="x", status="pending"),
            ALSafePromotedItem(name="y", status="pending"),
        ]
        await ALSafePromotedItem.asave_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 2))
        assert all(item._version == 0 for item in items)

        found = await ALSafePromotedItem.query().order_by("_id").all()
        assert len(found) == 2
        assert found[0].name == "x"
        assert found[0].status == "pending"
        assert found[0]._version == 0
        assert found[1].name == "y"
        assert found[1].status == "pending"
        assert found[1]._version == 0
    finally:
        ALSafePromotedItem._db = None
        ALSafePromotedItem._table = None


@pytest.mark.asyncio
async def test_asave_many_safe_model_rejects_existing(db):
    ALSafeItem.set_db(db)
    try:
        item = ALSafeItem(name="existing")
        await item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            await ALSafeItem.asave_many([item])
    finally:
        ALSafeItem._db = None
        ALSafeItem._table = None
