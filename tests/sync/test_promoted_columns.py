"""Tests for promoted columns support."""

import json
import sqlite3

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel, F


# ---- Schema tests ----


class TestPromotedSchema:
    def test_creates_table_with_promoted_columns(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {
                "status": "TEXT NOT NULL DEFAULT 'pending'",
                "priority": "INTEGER NOT NULL DEFAULT 0",
            }
            name: str = ""
            status: str = "pending"
            priority: int = 0

        Job.set_db(db, "jobs")
        # Verify columns exist
        cur = db.adapter.execute('PRAGMA table_info("jobs");')
        cols = {row[1] for row in cur.fetchall()}
        assert "status" in cols
        assert "priority" in cols
        assert "_id" in cols
        assert "data" in cols
        db.close()

    def test_creates_table_with_check_constraints(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'pending'"}
            __checks__ = {"status": "status IN ('pending','running','done')"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")

        # Valid insert should work
        j = Job(name="ok", status="pending")
        j.save()
        assert j._id is not None

        # Invalid status should fail via CHECK constraint
        with pytest.raises(Exception):
            db.adapter.execute(
                "INSERT INTO jobs (data, status) VALUES (json(?), ?);",
                ['{"name":"bad"}', "INVALID"],
            )
        db.close()

    def test_alter_adds_columns_to_existing_table(self):
        db = SQLerDB.in_memory(shared=False)
        # Create table without promoted first
        db._ensure_table("jobs")

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        cur = db.adapter.execute('PRAGMA table_info("jobs");')
        cols = {row[1] for row in cur.fetchall()}
        assert "status" in cols
        db.close()

    def test_promoted_columns_tracked_on_db(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT", "priority": "INTEGER"}
            name: str = ""
            status: str = "pending"
            priority: int = 0

        Job.set_db(db, "jobs")
        assert db._promoted_columns["jobs"] == ["status", "priority"]
        db.close()


# ---- Save/Load split tests ----


class TestPromotedSaveLoad:
    def test_save_splits_promoted_from_json(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        j = Job(name="test", status="running")
        j.save()

        # Check raw SQL: status should be in column, not in JSON
        cur = db.adapter.execute("SELECT _id, data, status FROM jobs WHERE _id = ?;", [j._id])
        row = cur.fetchone()
        assert row is not None
        data = json.loads(row[1])
        assert "status" not in data  # NOT in JSON
        assert data["name"] == "test"
        assert row[2] == "running"  # in column

        db.close()

    def test_load_merges_promoted_back(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        j = Job(name="test", status="running")
        j.save()

        loaded = Job.from_id(j._id)
        assert loaded is not None
        assert loaded.name == "test"
        assert loaded.status == "running"
        db.close()

    def test_update_promoted_field(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        j = Job(name="test", status="pending")
        j.save()

        j.status = "running"
        j.save()

        # Verify column updated
        cur = db.adapter.execute("SELECT status FROM jobs WHERE _id = ?;", [j._id])
        assert cur.fetchone()[0] == "running"
        db.close()


# ---- Filter rewriting tests ----


class TestPromotedFilterRewriting:
    def test_filter_uses_column_not_json_extract(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        Job(name="a", status="pending").save()
        Job(name="b", status="running").save()

        qs = Job.query().filter(F("status") == "pending")
        # Check SQL uses direct column
        sql = qs.sql()
        assert "status = ?" in sql or "status =" in sql
        assert "json_extract" not in sql.lower() or "status" not in sql.split("json_extract")[0]

        results = qs.all()
        assert len(results) == 1
        assert results[0].name == "a"
        db.close()

    def test_order_by_promoted_field_uses_column(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"priority": "INTEGER DEFAULT 0"}
            name: str = ""
            priority: int = 0

        Job.set_db(db, "jobs")
        Job(name="low", priority=1).save()
        Job(name="high", priority=10).save()

        qs = Job.query().order_by("-priority")
        sql = qs.sql()
        # The ORDER BY should reference the column directly
        assert "priority DESC" in sql

        results = qs.all()
        assert results[0].name == "high"
        assert results[1].name == "low"
        db.close()

    def test_non_promoted_field_still_uses_json_extract(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        Job(name="alice", status="pending").save()

        qs = Job.query().filter(F("name") == "alice")
        sql = qs.sql()
        assert "json_extract(data, '$.name')" in sql.lower()
        db.close()


# ---- SafeModel compat ----


class TestPromotedSafeModel:
    def test_safe_model_with_promoted(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerSafeModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        j = Job(name="test", status="pending")
        j.save()
        assert j._id is not None
        assert j._version == 0

        j.status = "running"
        j.save()
        assert j._version == 1

        loaded = Job.from_id(j._id)
        assert loaded.status == "running"
        assert loaded._version == 1
        db.close()

    def test_safe_model_query_with_promoted(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerSafeModel):
            __promoted__ = {
                "status": "TEXT DEFAULT 'pending'",
                "priority": "INTEGER DEFAULT 0",
            }
            name: str = ""
            status: str = "pending"
            priority: int = 0

        Job.set_db(db, "jobs")
        Job(name="a", status="pending", priority=1).save()
        Job(name="b", status="running", priority=10).save()

        results = (
            Job.query()
            .filter(F("status") == "pending")
            .order_by("-priority")
            .all()
        )
        assert len(results) == 1
        assert results[0].name == "a"
        db.close()


# ---- Multiple promoted fields ----


class TestMultiplePromoted:
    def test_three_promoted_fields(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {
                "ulid": "TEXT",
                "status": "TEXT DEFAULT 'pending'",
                "priority": "INTEGER DEFAULT 0",
            }
            ulid: str = ""
            status: str = "pending"
            priority: int = 0
            payload: str = ""

        Job.set_db(db, "jobs")
        j = Job(ulid="abc123", status="running", priority=5, payload="data")
        j.save()

        # Verify storage split
        cur = db.adapter.execute(
            "SELECT data, ulid, status, priority FROM jobs WHERE _id = ?;", [j._id]
        )
        row = cur.fetchone()
        data = json.loads(row[0])
        assert "ulid" not in data
        assert "status" not in data
        assert "priority" not in data
        assert data["payload"] == "data"
        assert row[1] == "abc123"
        assert row[2] == "running"
        assert row[3] == 5

        # Verify load merges back
        loaded = Job.from_id(j._id)
        assert loaded.ulid == "abc123"
        assert loaded.status == "running"
        assert loaded.priority == 5
        assert loaded.payload == "data"
        db.close()


# ---- Aggregate queries on promoted columns ----


class TestPromotedAggregates:
    """Regression tests for aggregate queries on promoted columns.

    Previously, _build_aggregate_query did not call _rewrite_promoted_refs,
    so WHERE clauses on promoted columns used json_extract(data, '$.field')
    instead of the direct column. Since promoted fields are stripped from the
    JSON blob on save, json_extract returned NULL and aggregates returned 0.
    """

    def test_count_with_promoted_filter(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {
                "status": "TEXT DEFAULT 'pending'",
                "priority": "INTEGER DEFAULT 0",
            }
            name: str = ""
            status: str = "pending"
            priority: int = 0

        Job.set_db(db, "jobs")
        Job(name="a", status="pending", priority=1).save()
        Job(name="b", status="running", priority=5).save()
        Job(name="c", status="pending", priority=3).save()

        count = Job.query().filter(F("status") == "pending").count()
        assert count == 2
        db.close()

    def test_count_with_multiple_promoted_filters(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {
                "status": "TEXT DEFAULT 'pending'",
                "priority": "INTEGER DEFAULT 0",
            }
            name: str = ""
            status: str = "pending"
            priority: int = 0

        Job.set_db(db, "jobs")
        Job(name="a", status="pending", priority=1).save()
        Job(name="b", status="pending", priority=10).save()
        Job(name="c", status="running", priority=10).save()

        count = Job.query().filter(
            (F("status") == "pending") & (F("priority") == 10)
        ).count()
        assert count == 1
        db.close()

    def test_count_with_in_list_promoted(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {"status": "TEXT DEFAULT 'pending'"}
            name: str = ""
            status: str = "pending"

        Job.set_db(db, "jobs")
        Job(name="a", status="pending").save()
        Job(name="b", status="running").save()
        Job(name="c", status="failed").save()

        count = Job.query().filter(
            F("status").in_list(["pending", "running"])
        ).count()
        assert count == 2
        db.close()

    def test_sum_on_promoted_field(self):
        db = SQLerDB.in_memory(shared=False)

        class Job(SQLerModel):
            __promoted__ = {
                "status": "TEXT DEFAULT 'pending'",
                "priority": "INTEGER DEFAULT 0",
            }
            name: str = ""
            status: str = "pending"
            priority: int = 0

        Job.set_db(db, "jobs")
        Job(name="a", status="pending", priority=3).save()
        Job(name="b", status="pending", priority=7).save()
        Job(name="c", status="running", priority=5).save()

        total = Job.query().filter(F("status") == "pending").sum("priority")
        assert total == 10
        db.close()
