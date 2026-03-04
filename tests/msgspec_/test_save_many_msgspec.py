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
