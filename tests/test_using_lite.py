"""Tests for SQLerLiteModel.using() — per-query DB binding without class mutation.

Mirrors the sync Pydantic using() API. Lite safe models inherit using()
from their parent so they're tested too.
"""

from dataclasses import dataclass

from sqler import SQLerDB
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.query import F


@dataclass
class Widget(SQLerLiteModel):
    __tablename__ = "widgets"
    name: str
    color: str = "red"


@dataclass
class SafeWidget(SQLerLiteSafeModel):
    __tablename__ = "safe_widgets"
    name: str
    color: str = "blue"


def make_db():
    """Create an in-memory DB and ensure widget tables exist."""
    db = SQLerDB.in_memory(shared=False)
    db._ensure_table("widgets")
    db._ensure_table("safe_widgets")
    db._ensure_versioned_table("safe_widgets")
    return db


def seed(db, model_cls, items):
    """Insert items into db via upsert_document, return list of _ids."""
    table = model_cls.__tablename__
    ids = []
    for payload in items:
        _id = db.upsert_document(table, None, payload)
        ids.append(_id)
    return ids


class TestLiteUsingAll:
    """using(db).all() returns correct results."""

    def test_all_returns_seeded_items(self):
        db = make_db()
        seed(db, Widget, [
            {"name": "bolt", "color": "silver"},
            {"name": "nut", "color": "brass"},
        ])

        results = Widget.using(db).all()

        assert len(results) == 2
        names = sorted(w.name for w in results)
        assert names == ["bolt", "nut"]

    def test_all_empty_db_returns_empty(self):
        db = make_db()
        results = Widget.using(db).all()
        assert results == []


class TestLiteUsingFilter:
    """using(db).filter(...) works correctly."""

    def test_filter_by_field(self):
        db = make_db()
        seed(db, Widget, [
            {"name": "a", "color": "red"},
            {"name": "b", "color": "blue"},
            {"name": "c", "color": "red"},
        ])

        reds = Widget.using(db).filter(F("color") == "red").all()

        assert len(reds) == 2
        assert all(w.color == "red" for w in reds)

    def test_filter_no_match(self):
        db = make_db()
        seed(db, Widget, [{"name": "x", "color": "red"}])

        results = Widget.using(db).filter(F("color") == "green").all()
        assert results == []


class TestLiteUsingSafe:
    """SafeModel inherits using() from parent and it works."""

    def test_safe_using_all(self):
        db = make_db()
        # Seed safe_widgets directly
        db.upsert_document("safe_widgets", None, {"name": "sw1", "color": "blue"})
        db.upsert_document("safe_widgets", None, {"name": "sw2", "color": "green"})

        results = SafeWidget.using(db).all()

        assert len(results) == 2
        names = sorted(w.name for w in results)
        assert names == ["sw1", "sw2"]


class TestLiteUsingNoClassMutation:
    """using(db) must not mutate class-level state."""

    def test_class_db_unchanged_after_using(self):
        db1 = make_db()
        db2 = make_db()

        # Bind to db1 (suppressing deprecation)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            Widget.set_db(db1)

        original_db = Widget._db

        # Query via db2
        Widget.using(db2).all()

        # Class binding must still point to db1
        assert Widget._db is original_db
        assert Widget._db is db1


class TestLiteUsingCrossDbIsolation:
    """Items in db1 are not visible via using(db2)."""

    def test_items_isolated_between_dbs(self):
        db1 = make_db()
        db2 = make_db()

        seed(db1, Widget, [
            {"name": "only_in_db1", "color": "red"},
        ])
        seed(db2, Widget, [
            {"name": "only_in_db2", "color": "blue"},
            {"name": "also_in_db2", "color": "green"},
        ])

        r1 = Widget.using(db1).all()
        r2 = Widget.using(db2).all()

        assert len(r1) == 1
        assert r1[0].name == "only_in_db1"
        assert len(r2) == 2
        names_db2 = sorted(w.name for w in r2)
        assert names_db2 == ["also_in_db2", "only_in_db2"]

    def test_empty_db_returns_nothing(self):
        db1 = make_db()
        db2 = make_db()

        seed(db1, Widget, [{"name": "exists", "color": "red"}])

        # db2 has no data
        assert Widget.using(db2).all() == []
        # db1 has the data
        assert len(Widget.using(db1).all()) == 1


class TestLiteUsingChaining:
    """Queryset chaining works with using()."""

    def test_order_by(self):
        db = make_db()
        seed(db, Widget, [
            {"name": "c_widget", "color": "red"},
            {"name": "a_widget", "color": "blue"},
            {"name": "b_widget", "color": "green"},
        ])

        results = Widget.using(db).order_by("name").all()

        assert len(results) == 3
        assert [w.name for w in results] == ["a_widget", "b_widget", "c_widget"]

    def test_limit(self):
        db = make_db()
        seed(db, Widget, [
            {"name": f"w{i}", "color": "red"} for i in range(10)
        ])

        results = Widget.using(db).limit(3).all()
        assert len(results) == 3

    def test_count(self):
        db = make_db()
        seed(db, Widget, [
            {"name": f"w{i}", "color": "red"} for i in range(7)
        ])

        assert Widget.using(db).count() == 7

    def test_first(self):
        db = make_db()
        seed(db, Widget, [{"name": "solo", "color": "red"}])

        result = Widget.using(db).first()
        assert result is not None
        assert result.name == "solo"
