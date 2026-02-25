"""Sync tests for update_n (batch update with RETURNING)."""

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel, F


class SyncItem(SQLerModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0
    attempts: int = 0


class SyncPromotedItem(SQLerModel):
    __promoted__ = {
        "status": "TEXT DEFAULT 'pending'",
        "priority": "INTEGER DEFAULT 0",
    }
    name: str = ""
    status: str = "pending"
    priority: int = 0
    attempts: int = 0


class SyncVersionedItem(SQLerSafeModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0


@pytest.fixture
def db_with_items():
    db = SQLerDB.in_memory(shared=False)
    SyncItem.set_db(db, "items")
    SyncItem(name="a", status="pending", priority=1, attempts=0).save()
    SyncItem(name="b", status="pending", priority=5, attempts=0).save()
    SyncItem(name="c", status="pending", priority=10, attempts=0).save()
    SyncItem(name="d", status="completed", priority=3, attempts=1).save()
    SyncItem(name="e", status="pending", priority=7, attempts=0).save()
    yield db
    db.close()


@pytest.fixture
def db_promoted_items():
    db = SQLerDB.in_memory(shared=False)
    SyncPromotedItem.set_db(db, "pitems")  # auto-creates promoted schema
    SyncPromotedItem(name="a", status="pending", priority=1, attempts=0).save()
    SyncPromotedItem(name="b", status="pending", priority=5, attempts=0).save()
    SyncPromotedItem(name="c", status="pending", priority=10, attempts=0).save()
    SyncPromotedItem(name="d", status="completed", priority=3, attempts=1).save()
    yield db
    db.close()


@pytest.fixture
def db_versioned_items():
    db = SQLerDB.in_memory(shared=False)
    SyncVersionedItem.set_db(db, "vitems")
    db._ensure_table("vitems")
    db._ensure_versioned_table("vitems")
    SyncVersionedItem(name="x", status="pending", priority=5).save()
    SyncVersionedItem(name="y", status="pending", priority=10).save()
    SyncVersionedItem(name="z", status="pending", priority=1).save()
    yield db
    db.close()


class TestSyncUpdateNBasic:
    def test_respects_limit(self, db_with_items):
        results = (
            SyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        remaining = SyncItem.query().filter(F("status") == "pending").count()
        assert remaining == 2

    def test_ordering_respected(self, db_with_items):
        results = (
            SyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"c", "e"}

    def test_empty_match_returns_empty_list(self, db_with_items):
        results = (
            SyncItem.query()
            .filter(F("status") == "nonexistent")
            .update_n(5, status="running")
        )
        assert results == []

    def test_n_exceeds_available(self, db_with_items):
        results = (
            SyncItem.query()
            .filter(F("status") == "pending")
            .update_n(100, status="running")
        )
        assert len(results) == 4

    def test_f_expression_in_update(self, db_with_items):
        results = (
            SyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running", attempts=F("attempts") + 1)
        )
        assert len(results) == 2
        for r in results:
            assert r.status == "running"
            assert r.attempts == 1

    def test_returns_model_instances(self, db_with_items):
        results = (
            SyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(1, status="running")
        )
        assert len(results) == 1
        inst = results[0]
        assert inst.name == "c"  # highest priority pending
        assert inst.status == "running"
        assert inst.priority == 10

    def test_n_equals_one(self, db_with_items):
        results = (
            SyncItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(1, status="running")
        )
        assert len(results) == 1
        assert results[0].name == "c"
        assert results[0].status == "running"


class TestSyncUpdateNPromoted:
    def test_updates_promoted_column(self, db_promoted_items):
        results = (
            SyncPromotedItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"b", "c"}
        for r in results:
            assert r.status == "running"

    def test_updates_promoted_with_f_expression(self, db_promoted_items):
        results = (
            SyncPromotedItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, priority=F("priority") + 100)
        )
        assert len(results) == 2
        for r in results:
            assert r.priority >= 105


class TestSyncUpdateNVersioned:
    def test_bumps_version(self, db_versioned_items):
        results = (
            SyncVersionedItem.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_n(2, status="running")
        )
        assert len(results) == 2
        assert {r.name for r in results} == {"x", "y"}
        for r in results:
            assert r._version == 1  # initial save = 0, update_n bumps to 1
            assert r.status == "running"


class TestSyncUpdateNValidation:
    def test_raises_on_no_fields(self, db_with_items):
        with pytest.raises(ValueError, match="No fields"):
            SyncItem.query().update_n(2)

    def test_raises_on_n_less_than_1(self, db_with_items):
        with pytest.raises(ValueError, match="n must be >= 1"):
            SyncItem.query().update_n(0, status="running")
