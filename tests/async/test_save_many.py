"""Tests for AsyncSQLerModel.asave_many() and AsyncSQLerSafeModel.asave_many()."""

import sqlite3

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel

# ---- test models ----

class AItem(AsyncSQLerModel):
    __tablename__ = "items"
    name: str = ""
    value: int = 0


class APromotedItem(AsyncSQLerModel):
    __tablename__ = "promoted_items"
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


class ASafeItem(AsyncSQLerSafeModel):
    __tablename__ = "safe_items"
    name: str = ""
    value: int = 0


class ASafePromotedItem(AsyncSQLerSafeModel):
    __tablename__ = "safe_promoted_items"
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


# ---- AsyncSQLerModel.asave_many tests ----

@pytest.mark.asyncio
async def test_asave_many_basic(db):
    AItem.set_db(db)
    try:
        items = [AItem(name=f"item-{i}", value=i) for i in range(5)]
        result = await AItem.asave_many(items)

        assert result is items
        assert len(result) == 5
        ids = [item._id for item in result]
        assert all(isinstance(id_, int) for id_ in ids)
        assert len(set(ids)) == 5  # all unique
        # IDs should be sequential (multi-row INSERT with AUTOINCREMENT)
        assert ids == list(range(ids[0], ids[0] + 5))
    finally:
        AItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_empty_list(db):
    AItem.set_db(db)
    try:
        result = await AItem.asave_many([])
        assert result == []
    finally:
        AItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_rejects_existing(db):
    AItem.set_db(db)
    try:
        item = AItem(name="existing")
        await item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            await AItem.asave_many([item])
    finally:
        AItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_with_promoted(db):
    APromotedItem.set_db(db)
    try:
        items = [
            APromotedItem(name="a", status="active", priority=1),
            APromotedItem(name="b", status="inactive", priority=2),
            APromotedItem(name="c", status="archived", priority=3),
        ]
        await APromotedItem.asave_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))

        # Verify promoted columns persisted correctly via query
        found = await APromotedItem.query().order_by("priority").all()
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
        APromotedItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_roundtrip(db):
    AItem.set_db(db)
    try:
        items = [AItem(name=f"rt-{i}", value=i * 10) for i in range(3)]
        await AItem.asave_many(items)

        for item in items:
            loaded = await AItem.from_id(item._id)
            assert loaded is not None
            assert loaded.name == item.name
            assert loaded.value == item.value
            assert loaded._id == item._id
    finally:
        AItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_with_db_param(db):
    """Verify save_many works with explicit db= parameter instead of class binding."""
    table = AItem.__tablename__
    # Don't call set_db — use db= param
    items = [AItem(name=f"db-{i}", value=i) for i in range(3)]
    # Need binding for the table name resolution
    AItem._table = table
    try:
        await AItem.asave_many(items, db=db)
        assert all(item._id is not None for item in items)
        assert len(set(item._id for item in items)) == 3
    finally:
        AItem._table = None


@pytest.mark.asyncio
async def test_asave_many_atomicity(db):
    """If one row violates a CHECK constraint, no rows should be saved."""
    APromotedItem.set_db(db)
    try:
        items = [
            APromotedItem(name="good", status="active", priority=1),
            APromotedItem(name="bad", status="INVALID_STATUS", priority=2),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            await APromotedItem.asave_many(items)

        # Verify no rows were saved (transaction rolled back)
        count = await APromotedItem.count()
        assert count == 0
    finally:
        APromotedItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_chunking(db):
    """Insert enough rows to trigger chunking (>999 params)."""
    AItem.set_db(db)
    try:
        # 1 param per row (json data only), so need 1000+ items
        items = [AItem(name=f"chunk-{i}", value=i) for i in range(1005)]
        await AItem.asave_many(items)

        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert len(set(ids)) == 1005
        # Verify sequential
        assert ids == list(range(ids[0], ids[0] + 1005))
    finally:
        AItem.set_db(None)


# ---- AsyncSQLerSafeModel.asave_many tests ----

@pytest.mark.asyncio
async def test_asave_many_safe_model(db):
    ASafeItem.set_db(db)
    try:
        items = [ASafeItem(name=f"safe-{i}", value=i) for i in range(5)]
        result = await ASafeItem.asave_many(items)

        assert result is items
        assert len(result) == 5
        assert all(item._id is not None for item in items)
        assert all(item._version == 0 for item in items)
        # Snapshots should be set
        for item in items:
            assert item._snapshot is not None
            assert set(item._snapshot.keys()) == {"name", "value"}
            assert item._snapshot["name"] == item.name
            assert item._snapshot["value"] == item.value

        ids = [item._id for item in items]
        assert len(set(ids)) == 5
        assert ids == list(range(ids[0], ids[0] + 5))
    finally:
        ASafeItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_safe_model_with_promoted(db):
    ASafePromotedItem.set_db(db)
    try:
        items = [
            ASafePromotedItem(name="x", status="pending"),
            ASafePromotedItem(name="y", status="pending"),
        ]
        await ASafePromotedItem.asave_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 2))
        assert all(item._version == 0 for item in items)

        found = await ASafePromotedItem.query().order_by("_id").all()
        assert len(found) == 2
        assert found[0].name == "x"
        assert found[0].status == "pending"
        assert found[0]._version == 0
        assert found[1].name == "y"
        assert found[1].status == "pending"
        assert found[1]._version == 0
    finally:
        ASafePromotedItem.set_db(None)


@pytest.mark.asyncio
async def test_asave_many_safe_model_rejects_existing(db):
    ASafeItem.set_db(db)
    try:
        item = ASafeItem(name="existing")
        await item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            await ASafeItem.asave_many([item])
    finally:
        ASafeItem.set_db(None)
