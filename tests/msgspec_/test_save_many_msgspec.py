"""Tests for SQLerMsgspecModel.save_many()."""

import sqlite3

import pytest
from sqler import SQLerDB
from sqler.models.msgspec.model import SQLerMsgspecModel

# ---- test models ----

class MItem(SQLerMsgspecModel):
    __tablename__ = "msgspec_items"
    name: str = ""
    value: int = 0


class MPromotedItem(SQLerMsgspecModel):
    __tablename__ = "msgspec_promoted_items"
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


# ---- fixtures ----

@pytest.fixture
def db():
    db = SQLerDB.in_memory(shared=False)
    yield db
    db.close()


# ---- SQLerMsgspecModel.save_many tests ----

def test_save_many_basic(db):
    MItem.set_db(db)
    try:
        items = [MItem(name=f"item-{i}", value=i) for i in range(5)]
        result = MItem.save_many(items)

        assert result is items
        assert len(result) == 5
        ids = [item._id for item in result]
        assert all(isinstance(id_, int) for id_ in ids)
        assert len(set(ids)) == 5
        assert ids == list(range(ids[0], ids[0] + 5))
    finally:
        MItem._db = None
        MItem._table = None


def test_save_many_empty_list(db):
    MItem.set_db(db)
    try:
        result = MItem.save_many([])
        assert result == []
    finally:
        MItem._db = None
        MItem._table = None


def test_save_many_rejects_existing(db):
    MItem.set_db(db)
    try:
        item = MItem(name="existing")
        item.save()
        assert item._id is not None

        with pytest.raises(ValueError, match="only accepts new instances"):
            MItem.save_many([item])
    finally:
        MItem._db = None
        MItem._table = None


def test_save_many_with_promoted(db):
    MPromotedItem.set_db(db)
    try:
        items = [
            MPromotedItem(name="a", status="active", priority=1),
            MPromotedItem(name="b", status="inactive", priority=2),
            MPromotedItem(name="c", status="archived", priority=3),
        ]
        MPromotedItem.save_many(items)

        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))

        found = MPromotedItem.query().order_by("priority").all()
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
        MPromotedItem._db = None
        MPromotedItem._table = None


def test_save_many_atomicity_promoted(db):
    """If one row violates a CHECK constraint, no rows should be saved."""
    MPromotedItem.set_db(db)
    try:
        items = [
            MPromotedItem(name="good", status="active", priority=1),
            MPromotedItem(name="bad", status="INVALID_STATUS", priority=2),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            MPromotedItem.save_many(items)

        count = MPromotedItem.query().count()
        assert count == 0
    finally:
        MPromotedItem._db = None
        MPromotedItem._table = None


def test_save_many_roundtrip(db):
    MItem.set_db(db)
    try:
        items = [MItem(name=f"rt-{i}", value=i * 10) for i in range(3)]
        MItem.save_many(items)

        for item in items:
            loaded = MItem.from_id(item._id)
            assert loaded is not None
            assert loaded.name == item.name
            assert loaded.value == item.value
            assert loaded._id == item._id
    finally:
        MItem._db = None
        MItem._table = None


def test_save_many_with_db_param(db):
    """Verify save_many works with explicit db= parameter instead of class binding."""
    table = MItem.__tablename__
    items = [MItem(name=f"db-{i}", value=i) for i in range(3)]
    MItem._table = table
    try:
        MItem.save_many(items, db=db)
        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert ids == list(range(ids[0], ids[0] + 3))
    finally:
        MItem._table = None


def test_save_many_atomicity(db):
    """If one row violates a UNIQUE constraint, no rows should be saved."""
    MItem.set_db(db)
    try:
        MItem.add_index("name", unique=True)
        existing = MItem(name="duplicate")
        existing.save()

        items = [
            MItem(name="good", value=1),
            MItem(name="duplicate", value=2),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            MItem.save_many(items)

        count = MItem.query().count()
        assert count == 1
    finally:
        MItem._db = None
        MItem._table = None


def test_save_many_chunking(db):
    """Insert enough rows to trigger chunking (>999 params)."""
    MItem.set_db(db)
    try:
        items = [MItem(name=f"chunk-{i}", value=i) for i in range(1005)]
        MItem.save_many(items)

        assert all(item._id is not None for item in items)
        ids = [item._id for item in items]
        assert len(set(ids)) == 1005
        assert ids == list(range(ids[0], ids[0] + 1005))
    finally:
        MItem._db = None
        MItem._table = None
