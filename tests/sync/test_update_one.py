"""Tests for update_one with RETURNING."""

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel, F


# ---- Basic update_one tests ----


class Task(SQLerModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0
    attempts: int = 0


@pytest.fixture
def db_with_tasks():
    db = SQLerDB.in_memory(shared=False)
    Task.set_db(db, "tasks")
    Task(name="low", status="pending", priority=1, attempts=0).save()
    Task(name="high", status="pending", priority=10, attempts=0).save()
    Task(name="done", status="completed", priority=5, attempts=1).save()
    yield db
    db.close()


class TestUpdateOneBasic:
    def test_claims_one_row(self, db_with_tasks):
        result = (
            Task.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running", attempts=F("attempts") + 1)
        )
        assert result is not None
        assert result.name == "high"  # highest priority pending
        assert result.status == "running"
        assert result.attempts == 1

    def test_returns_none_when_no_match(self, db_with_tasks):
        result = (
            Task.query()
            .filter(F("status") == "nonexistent")
            .update_one(status="running")
        )
        assert result is None

    def test_only_updates_one_row(self, db_with_tasks):
        Task.query().filter(F("status") == "pending").order_by("-priority").update_one(
            status="running"
        )
        # Only the high-priority task should be claimed
        pending = Task.query().filter(F("status") == "pending").all()
        assert len(pending) == 1
        assert pending[0].name == "low"

    def test_with_ordering(self, db_with_tasks):
        # Claim lowest priority first
        result = (
            Task.query()
            .filter(F("status") == "pending")
            .order_by("priority")
            .update_one(status="running")
        )
        assert result.name == "low"

    def test_f_expression_in_update_one(self, db_with_tasks):
        result = (
            Task.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(
                status="running",
                attempts=F("attempts") + 1,
            )
        )
        assert result.attempts == 1
        assert result.status == "running"

    def test_literal_value_update(self, db_with_tasks):
        result = (
            Task.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="cancelled", priority=0)
        )
        assert result.status == "cancelled"
        assert result.priority == 0


# ---- SafeModel versioning tests ----


class VersionedTask(SQLerSafeModel):
    name: str = ""
    status: str = "pending"
    priority: int = 0


@pytest.fixture
def db_with_versioned():
    db = SQLerDB.in_memory(shared=False)
    VersionedTask.set_db(db, "vtasks")
    VersionedTask(name="a", status="pending", priority=5).save()
    VersionedTask(name="b", status="pending", priority=10).save()
    yield db
    db.close()


class TestUpdateOneVersioned:
    def test_auto_bumps_version(self, db_with_versioned):
        result = (
            VersionedTask.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        assert result is not None
        assert result.name == "b"
        assert result.status == "running"
        assert result._version == 1  # bumped from 0

    def test_version_persisted(self, db_with_versioned):
        VersionedTask.query().filter(F("status") == "pending").order_by(
            "-priority"
        ).update_one(status="running")
        loaded = VersionedTask.query().filter(F("name") == "b").first()
        assert loaded._version == 1


# ---- Promoted column tests ----


class PromotedTask(SQLerModel):
    __promoted__ = {
        "status": "TEXT DEFAULT 'pending'",
        "priority": "INTEGER DEFAULT 0",
    }
    name: str = ""
    status: str = "pending"
    priority: int = 0
    attempts: int = 0


@pytest.fixture
def db_with_promoted_tasks():
    db = SQLerDB.in_memory(shared=False)
    PromotedTask.set_db(db, "ptasks")
    PromotedTask(name="low", status="pending", priority=1, attempts=0).save()
    PromotedTask(name="high", status="pending", priority=10, attempts=0).save()
    yield db
    db.close()


class TestUpdateOnePromoted:
    def test_claims_with_promoted(self, db_with_promoted_tasks):
        result = (
            PromotedTask.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(
                status="running",
                attempts=F("attempts") + 1,
            )
        )
        assert result is not None
        assert result.name == "high"
        assert result.status == "running"
        assert result.priority == 10
        assert result.attempts == 1

    def test_promoted_column_update(self, db_with_promoted_tasks):
        result = (
            PromotedTask.query()
            .filter(F("status") == "pending")
            .order_by("priority")
            .update_one(status="running", priority=99)
        )
        assert result.status == "running"
        assert result.priority == 99


# ---- Concurrency simulation ----


class TestUpdateOneConcurrency:
    def test_two_workers_claim_different_rows(self, db_with_tasks):
        """Simulate two workers claiming from the same queue."""
        # Worker 1 claims
        w1 = (
            Task.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        # Worker 2 claims — should get a different row
        w2 = (
            Task.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        assert w1 is not None
        assert w2 is not None
        assert w1._id != w2._id
        assert w1.name == "high"
        assert w2.name == "low"

    def test_third_worker_gets_none(self, db_with_tasks):
        """After all pending rows are claimed, next claim returns None."""
        Task.query().filter(F("status") == "pending").order_by("-priority").update_one(
            status="running"
        )
        Task.query().filter(F("status") == "pending").order_by("-priority").update_one(
            status="running"
        )
        result = (
            Task.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .update_one(status="running")
        )
        assert result is None
