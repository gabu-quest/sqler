"""Tests for F("_id") and F("_version") meta-column rewriting.

BUG-6: F("_id") == value silently returns no results because _id is a real
SQLite column (INTEGER PRIMARY KEY), not stored in the JSON data blob.
The query generates json_extract(data, '$._id') which always returns NULL.

The fix rewrites json_extract(data, '$._id') -> _id (and same for _version)
before executing the query.
"""

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel
from sqler.query import F


class Item(SQLerModel):
    __tablename__ = "meta_items"
    name: str = ""


class SafeItem(SQLerSafeModel):
    __tablename__ = "meta_safe_items"
    name: str = ""


class PromotedItem(SQLerModel):
    __tablename__ = "meta_promoted_items"
    __promoted__ = {"status": "TEXT DEFAULT 'active'"}
    name: str = ""
    status: str = "active"


@pytest.fixture()
def db():
    db = SQLerDB.in_memory()
    Item.set_db(db, "meta_items")
    SafeItem.set_db(db, "meta_safe_items")
    PromotedItem.set_db(db, "meta_promoted_items")
    return db


@pytest.fixture()
def saved_item(db):
    item = Item(name="alice")
    item.save()
    return item


@pytest.fixture()
def saved_safe_item(db):
    item = SafeItem(name="bob")
    item.save()
    return item


class TestFIdFilter:
    """F("_id") in WHERE clause."""

    def test_f_id_filter_returns_row(self, saved_item):
        results = Item.query().filter(F("_id") == saved_item._id).all()
        assert len(results) == 1
        assert results[0]._id == saved_item._id
        assert results[0].name == "alice"

    def test_f_id_filter_nonexistent(self, db):
        Item(name="x").save()
        results = Item.query().filter(F("_id") == 99999).all()
        assert len(results) == 0

    def test_f_id_filter_first(self, saved_item):
        result = Item.query().filter(F("_id") == saved_item._id).first()
        assert result is not None
        assert result._id == saved_item._id
        assert result.name == "alice"

    def test_f_id_gt(self, db):
        """F("_id") > value works for range queries."""
        a = Item(name="a")
        a.save()
        b = Item(name="b")
        b.save()
        c = Item(name="c")
        c.save()
        results = Item.query().filter(F("_id") > a._id).all()
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"b", "c"}


class TestFIdOrderBy:
    """order_by("_id") hits the ORDER BY rewrite path."""

    def test_order_by_id_asc_sql(self, db):
        """Verify the generated SQL uses direct _id, not json_extract."""
        Item(name="x").save()
        qs = Item.query().order_by("_id")
        sql = qs._query.sql
        assert "ORDER BY _id" in sql
        assert "json_extract" not in sql.split("ORDER BY")[1]

    def test_order_by_id_desc_sql(self, db):
        Item(name="x").save()
        qs = Item.query().order_by("-_id")
        sql = qs._query.sql
        assert "ORDER BY _id DESC" in sql

    def test_order_by_id_asc_behavior(self, db):
        # Insert c, b, a so insertion order != _id ascending name order
        c = Item(name="c")
        c.save()
        b = Item(name="b")
        b.save()
        a = Item(name="a")
        a.save()
        results = Item.query().order_by("_id").all()
        assert len(results) == 3
        # Lowest _id was inserted first (c), then b, then a
        assert results[0].name == "c"
        assert results[1].name == "b"
        assert results[2].name == "a"

    def test_order_by_id_desc_behavior(self, db):
        c = Item(name="c")
        c.save()
        b = Item(name="b")
        b.save()
        a = Item(name="a")
        a.save()
        results = Item.query().order_by("-_id").all()
        assert len(results) == 3
        # Highest _id was inserted last (a)
        assert results[0].name == "a"
        assert results[1].name == "b"
        assert results[2].name == "c"


class TestFIdAggregate:
    """F("_id") in aggregate queries (count with filter)."""

    def test_f_id_count(self, saved_item):
        count = Item.query().filter(F("_id") == saved_item._id).count()
        assert count == 1

    def test_f_id_count_nonexistent(self, db):
        Item(name="x").save()
        count = Item.query().filter(F("_id") == 99999).count()
        assert count == 0

    def test_f_id_aggregate_sql(self, db):
        """Verify aggregate WHERE clause rewrites json_extract to _id."""
        Item(name="x").save()
        q = Item.query().filter(F("_id") == 1)
        sql, _ = q._query._build_aggregate_query("COUNT")
        assert "_id = ?" in sql
        assert "json_extract" not in sql


class TestFVersionFilter:
    """F("_version") on SafeModel tables."""

    def test_f_version_filter(self, saved_safe_item):
        # Initial save starts at version 0
        results = SafeItem.query().filter(F("_version") == 0).all()
        assert len(results) == 1
        assert results[0].name == "bob"
        assert results[0]._version == 0

    def test_f_version_after_update(self, saved_safe_item):
        saved_safe_item.name = "bob2"
        saved_safe_item.save()
        # Version bumps to 1 after first update
        results = SafeItem.query().filter(F("_version") == 1).all()
        assert len(results) == 1
        assert results[0].name == "bob2"
        # Old version should return nothing
        results_v0 = SafeItem.query().filter(F("_version") == 0).all()
        assert len(results_v0) == 0


class TestFIdWithPromotedModel:
    """F("_id") does not interfere with promoted column rewriting."""

    def test_f_id_with_promoted(self, db):
        item = PromotedItem(name="promoted", status="done")
        item.save()
        results = PromotedItem.query().filter(F("_id") == item._id).all()
        assert len(results) == 1
        assert results[0].name == "promoted"
        assert results[0].status == "done"

    def test_f_id_and_promoted_filter(self, db):
        item = PromotedItem(name="test", status="active")
        item.save()
        results = (
            PromotedItem.query()
            .filter((F("_id") == item._id) & (F("status") == "active"))
            .all()
        )
        assert len(results) == 1
        assert results[0].name == "test"
