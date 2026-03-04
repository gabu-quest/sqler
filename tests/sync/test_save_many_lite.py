"""Tests for SQLerLiteModel.save_many() and SQLerLiteSafeModel.save_many()."""

import sqlite3
from dataclasses import dataclass

import pytest
from sqler import SQLerDB
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel


# ---- test models ----

@dataclass
class LItem(SQLerLiteModel):
    __tablename__ = "lite_items"
    name: str = ""
    value: int = 0


@dataclass
class LPromotedItem(SQLerLiteModel):
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
class LSafeItem(SQLerLiteSafeModel):
    __tablename__ = "lite_safe_items"
    name: str = ""
    value: int = 0


@dataclass
class LSafePromotedItem(SQLerLiteSafeModel):
    __tablename__ = "lite_safe_promoted_items"
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


# ---- SQLerLiteModel.save_many tests ----

def test_save_many_basic(db):
    LItem.set_db(db)
    try:
        items = [LItem(name=f"item-{i}", value=i) for i in range(5)]
        result = LItem.save_many(items)

        assert result is items
        assert len(result) == 5
        ids = [item._id for item in result]
        assert all(isinstance(id_, int) for id_ in ids)
        assert len(set(ids)) == 5
        assert ids == list(range(ids[0], ids[0] + 5))
    finally:
        LItem._db = None
        LItem._table = None


def test_save_many_empty_list(db):
    LItem.set_db(db)
    try:
        result = LItem.save_many([])
        assert result == []
    finally:
        LItem._db = None
        LItem._table = None


def test_save_many_rejects_existing(db):
    LItem.set_db(db)
    try:
        item = LItem(name="existing")
        item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            LItem.save_many([item])
    finally:
        LItem._db = None
        LItem._table = None


def test_save_many_with_promoted(db):
    LPromotedItem.set_db(db)
    try:
        items = [
            LPromotedItem(name="a", status="active", priority=1),
            LPromotedItem(name="b", status="inactive", priority=2),
            LPromotedItem(name="c", status="archived", priority=3),
        ]
        LPromotedItem.save_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))

        found = LPromotedItem.query().order_by("priority").all()
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
        LPromotedItem._db = None
        LPromotedItem._table = None


def test_save_many_atomicity_promoted(db):
    """If one row violates a CHECK constraint, no rows should be saved."""
    LPromotedItem.set_db(db)
    try:
        items = [
            LPromotedItem(name="good", status="active", priority=1),
            LPromotedItem(name="bad", status="INVALID_STATUS", priority=2),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            LPromotedItem.save_many(items)

        count = LPromotedItem.query().count()
        assert count == 0
    finally:
        LPromotedItem._db = None
        LPromotedItem._table = None


def test_save_many_roundtrip(db):
    LItem.set_db(db)
    try:
        items = [LItem(name=f"rt-{i}", value=i * 10) for i in range(3)]
        LItem.save_many(items)

        for item in items:
            loaded = LItem.from_id(item._id)
            assert loaded is not None
            assert loaded.name == item.name
            assert loaded.value == item.value
            assert loaded._id == item._id
    finally:
        LItem._db = None
        LItem._table = None


def test_save_many_with_db_param(db):
    """Verify save_many works with explicit db= parameter instead of class binding."""
    table = LItem.__tablename__
    items = [LItem(name=f"db-{i}", value=i) for i in range(3)]
    LItem._table = table
    try:
        LItem.save_many(items, db=db)
        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))
    finally:
        LItem._table = None


def test_save_many_atomicity(db):
    """If one row violates a UNIQUE constraint, no rows should be saved."""
    LItem.set_db(db)
    try:
        LItem.add_index("name", unique=True)
        # Pre-insert a row so the batch hits a duplicate
        existing = LItem(name="duplicate")
        existing.save()

        items = [
            LItem(name="good", value=1),
            LItem(name="duplicate", value=2),  # conflicts with existing
        ]
        with pytest.raises(sqlite3.IntegrityError):
            LItem.save_many(items)

        # Only the pre-existing row should remain
        count = LItem.query().count()
        assert count == 1
    finally:
        LItem._db = None
        LItem._table = None


def test_save_many_chunking(db):
    """Insert enough rows to trigger chunking (>999 params)."""
    LItem.set_db(db)
    try:
        items = [LItem(name=f"chunk-{i}", value=i) for i in range(1005)]
        LItem.save_many(items)

        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert len(set(ids)) == 1005
        assert ids == list(range(ids[0], ids[0] + 1005))
    finally:
        LItem._db = None
        LItem._table = None


# ---- SQLerLiteSafeModel.save_many tests ----

def test_save_many_safe_model(db):
    LSafeItem.set_db(db)
    try:
        items = [LSafeItem(name=f"safe-{i}", value=i) for i in range(5)]
        result = LSafeItem.save_many(items)

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
        LSafeItem._db = None
        LSafeItem._table = None


def test_save_many_safe_model_with_promoted(db):
    LSafePromotedItem.set_db(db)
    try:
        items = [
            LSafePromotedItem(name="x", status="pending"),
            LSafePromotedItem(name="y", status="pending"),
        ]
        LSafePromotedItem.save_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 2))
        assert all(item._version == 0 for item in items)

        found = LSafePromotedItem.query().order_by("_id").all()
        assert len(found) == 2
        assert found[0].name == "x"
        assert found[0].status == "pending"
        assert found[0]._version == 0
        assert found[1].name == "y"
        assert found[1].status == "pending"
        assert found[1]._version == 0
    finally:
        LSafePromotedItem._db = None
        LSafePromotedItem._table = None


def test_save_many_safe_model_rejects_existing(db):
    LSafeItem.set_db(db)
    try:
        item = LSafeItem(name="existing")
        item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            LSafeItem.save_many([item])
    finally:
        LSafeItem._db = None
        LSafeItem._table = None
