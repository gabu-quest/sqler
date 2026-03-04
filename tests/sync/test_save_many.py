"""Tests for SQLerModel.save_many() and SQLerSafeModel.save_many()."""

import sqlite3

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel

# ---- test models ----

class SItem(SQLerModel):
    __tablename__ = "items"
    name: str = ""
    value: int = 0


class SPromotedItem(SQLerModel):
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


class SSafeItem(SQLerSafeModel):
    __tablename__ = "safe_items"
    name: str = ""
    value: int = 0


class SSafePromotedItem(SQLerSafeModel):
    __tablename__ = "safe_promoted_items"
    __promoted__ = {
        "status": "TEXT NOT NULL DEFAULT 'pending'",
    }
    name: str = ""
    status: str = "pending"


# ---- fixtures ----

@pytest.fixture
def db():
    db = SQLerDB.in_memory(shared=False)
    yield db
    db.close()


# ---- SQLerModel.save_many tests ----

def test_save_many_basic(db):
    SItem.set_db(db)
    try:
        items = [SItem(name=f"item-{i}", value=i) for i in range(5)]
        result = SItem.save_many(items)

        assert result is items
        assert len(result) == 5
        ids = [item._id for item in result]
        assert all(isinstance(id_, int) for id_ in ids)
        assert len(set(ids)) == 5
        assert ids == list(range(ids[0], ids[0] + 5))
    finally:
        SItem._db = None
        SItem._table = None


def test_save_many_empty_list(db):
    SItem.set_db(db)
    try:
        result = SItem.save_many([])
        assert result == []
    finally:
        SItem._db = None
        SItem._table = None


def test_save_many_rejects_existing(db):
    SItem.set_db(db)
    try:
        item = SItem(name="existing")
        item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            SItem.save_many([item])
    finally:
        SItem._db = None
        SItem._table = None


def test_save_many_with_promoted(db):
    SPromotedItem.set_db(db)
    try:
        items = [
            SPromotedItem(name="a", status="active", priority=1),
            SPromotedItem(name="b", status="inactive", priority=2),
            SPromotedItem(name="c", status="archived", priority=3),
        ]
        SPromotedItem.save_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))

        found = SPromotedItem.query().order_by("priority").all()
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
        SPromotedItem._db = None
        SPromotedItem._table = None


def test_save_many_roundtrip(db):
    SItem.set_db(db)
    try:
        items = [SItem(name=f"rt-{i}", value=i * 10) for i in range(3)]
        SItem.save_many(items)

        for item in items:
            loaded = SItem.from_id(item._id)
            assert loaded is not None
            assert loaded.name == item.name
            assert loaded.value == item.value
            assert loaded._id == item._id
    finally:
        SItem._db = None
        SItem._table = None


def test_save_many_with_db_param(db):
    """Verify save_many works with explicit db= parameter instead of class binding."""
    table = SItem.__tablename__
    items = [SItem(name=f"db-{i}", value=i) for i in range(3)]
    SItem._table = table
    try:
        SItem.save_many(items, db=db)
        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))
    finally:
        SItem._table = None


def test_save_many_atomicity(db):
    """If one row violates a CHECK constraint, no rows should be saved."""
    SPromotedItem.set_db(db)
    try:
        items = [
            SPromotedItem(name="good", status="active", priority=1),
            SPromotedItem(name="bad", status="INVALID_STATUS", priority=2),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            SPromotedItem.save_many(items)

        count = SPromotedItem.query().count()
        assert count == 0
    finally:
        SPromotedItem._db = None
        SPromotedItem._table = None


def test_save_many_chunking(db):
    """Insert enough rows to trigger chunking (>999 params)."""
    SItem.set_db(db)
    try:
        items = [SItem(name=f"chunk-{i}", value=i) for i in range(1005)]
        SItem.save_many(items)

        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert len(set(ids)) == 1005
        assert ids == list(range(ids[0], ids[0] + 1005))
    finally:
        SItem._db = None
        SItem._table = None


# ---- SQLerSafeModel.save_many tests ----

def test_save_many_safe_model(db):
    SSafeItem.set_db(db)
    try:
        items = [SSafeItem(name=f"safe-{i}", value=i) for i in range(5)]
        result = SSafeItem.save_many(items)

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
        SSafeItem._db = None
        SSafeItem._table = None


def test_save_many_safe_model_with_promoted(db):
    SSafePromotedItem.set_db(db)
    try:
        items = [
            SSafePromotedItem(name="x", status="pending"),
            SSafePromotedItem(name="y", status="pending"),
        ]
        SSafePromotedItem.save_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 2))
        assert all(item._version == 0 for item in items)

        found = SSafePromotedItem.query().order_by("_id").all()
        assert len(found) == 2
        assert found[0].name == "x"
        assert found[0].status == "pending"
        assert found[0]._version == 0
        assert found[1].name == "y"
        assert found[1].status == "pending"
        assert found[1]._version == 0
    finally:
        SSafePromotedItem._db = None
        SSafePromotedItem._table = None


def test_save_many_safe_model_rejects_existing(db):
    SSafeItem.set_db(db)
    try:
        item = SSafeItem(name="existing")
        item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            SSafeItem.save_many([item])
    finally:
        SSafeItem._db = None
        SSafeItem._table = None
