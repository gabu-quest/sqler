"""Tests for mixin db= forwarding to save()/delete().

Verifies that every mixin that overrides save() or delete() correctly
accepts and forwards the db= parameter for per-call DB routing.
"""

from datetime import datetime, timezone
from typing import Optional

import pytest
from sqler import SQLerDB, SQLerModel
from sqler.models.mixins import (
    AuditLogMixin,
    AuditMixin,
    FullMixin,
    HooksMixin,
    SoftDeleteMixin,
    TimestampMixin,
)


# ── Model fixtures ──────────────────────────────────────────────────────────


class TSItem(TimestampMixin, SQLerModel):
    __tablename__ = "ts_items"
    name: str


class HookedItem(HooksMixin, SQLerModel):
    __tablename__ = "hooked_items"
    name: str
    hook_log: list[str] = []

    def before_save(self) -> bool:
        self.hook_log = [*self.hook_log, "before_save"]
        return True

    def after_save(self) -> None:
        self.hook_log = [*self.hook_log, "after_save"]

    def before_delete(self) -> bool:
        self.hook_log = [*self.hook_log, "before_delete"]
        return True

    def after_delete(self) -> None:
        self.hook_log = [*self.hook_log, "after_delete"]


class SoftItem(SoftDeleteMixin, SQLerModel):
    __tablename__ = "soft_items"
    name: str


class AuditItem(AuditMixin, SQLerModel):
    __tablename__ = "audit_items"
    name: str


class AuditLogItem(AuditLogMixin, SQLerModel):
    __tablename__ = "audit_log_items"
    name: str


class FullItem(FullMixin, SQLerModel):
    __tablename__ = "full_items"
    name: str


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_db_pair():
    """Create two independent in-memory databases."""
    db1 = SQLerDB.in_memory(shared=False)
    db2 = SQLerDB.in_memory(shared=False)
    return db1, db2


def setup(model_cls, db1, db2):
    """Bind model to db1 and ensure table exists in db2."""
    model_cls.set_db(db1)
    db2._ensure_table(model_cls._table)


# ══════════════════════════════════════════════════════════════════════════════
# TestMixinDbForwarding
# ══════════════════════════════════════════════════════════════════════════════


class TestMixinDbForwarding:
    """Verify every mixin correctly forwards db= through its save/delete chain."""

    def test_timestamp_mixin_save_with_db(self):
        """TimestampMixin.save(db=db2) writes to db2 with timestamps set."""
        db1, db2 = make_db_pair()
        setup(TSItem, db1, db2)

        item = TSItem(name="ts_test")
        item.save(db=db2)

        assert item._id is not None
        assert item.created_at is not None
        assert item.updated_at is not None

        doc = db2.find_document("ts_items", item._id)
        assert doc is not None
        assert doc["name"] == "ts_test"

        assert db1.find_document("ts_items", item._id) is None

    def test_hooks_mixin_save_with_db(self):
        """HooksMixin.save(db=db2) fires hooks and writes to db2."""
        db1, db2 = make_db_pair()
        setup(HookedItem, db1, db2)

        item = HookedItem(name="hooked_test")
        item.save(db=db2)

        assert item._id is not None
        assert "before_save" in item.hook_log
        assert "after_save" in item.hook_log

        doc = db2.find_document("hooked_items", item._id)
        assert doc is not None
        assert doc["name"] == "hooked_test"

        assert db1.find_document("hooked_items", item._id) is None

    def test_hooks_mixin_delete_with_db(self):
        """HooksMixin.delete(db=db2) fires hooks and deletes from db2."""
        db1, db2 = make_db_pair()
        setup(HookedItem, db1, db2)

        item = HookedItem(name="delete_test")
        item.save(db=db2)
        saved_id = item._id

        item.delete(db=db2)

        assert "before_delete" in item.hook_log
        assert "after_delete" in item.hook_log
        assert item._id is None
        assert db2.find_document("hooked_items", saved_id) is None

    def test_audit_mixin_save_with_db(self):
        """AuditMixin.save(db=db2) sets created_by/updated_by in db2."""
        db1, db2 = make_db_pair()
        setup(AuditItem, db1, db2)

        AuditMixin.set_current_user("test_user")
        try:
            item = AuditItem(name="audit_test")
            item.save(db=db2)

            assert item._id is not None
            assert item.created_by == "test_user"
            assert item.updated_by == "test_user"
            assert item.created_at is not None
            assert item.updated_at is not None

            doc = db2.find_document("audit_items", item._id)
            assert doc is not None
            assert doc["name"] == "audit_test"

            assert db1.find_document("audit_items", item._id) is None
        finally:
            AuditMixin.set_current_user(None)

    def test_soft_delete_with_db(self):
        """SoftDeleteMixin.soft_delete(db=db2) sets deleted_at in db2."""
        db1, db2 = make_db_pair()
        setup(SoftItem, db1, db2)

        item = SoftItem(name="soft_test")
        item.save(db=db2)

        item.soft_delete(db=db2)

        assert item.is_deleted
        assert item.deleted_at is not None

        doc = db2.find_document("soft_items", item._id)
        assert doc is not None
        assert doc["deleted_at"] is not None

    def test_soft_delete_restore_with_db(self):
        """SoftDeleteMixin.restore(db=db2) clears deleted_at in db2."""
        db1, db2 = make_db_pair()
        setup(SoftItem, db1, db2)

        item = SoftItem(name="restore_test")
        item.save(db=db2)
        item.soft_delete(db=db2)

        assert item.is_deleted

        item.restore(db=db2)

        assert not item.is_deleted
        assert item.deleted_at is None

        doc = db2.find_document("soft_items", item._id)
        assert doc is not None
        assert doc["deleted_at"] is None

    def test_soft_delete_hard_delete_with_db(self):
        """SoftDeleteMixin.hard_delete(db=db2) permanently removes from db2."""
        db1, db2 = make_db_pair()
        setup(SoftItem, db1, db2)

        item = SoftItem(name="hard_test")
        item.save(db=db2)
        saved_id = item._id

        item.hard_delete(db=db2)

        assert item._id is None
        assert db2.find_document("soft_items", saved_id) is None

    def test_audit_log_mixin_save_with_db(self):
        """AuditLogMixin.save(db=db2) writes audit entries to db2."""
        db1, db2 = make_db_pair()
        setup(AuditLogItem, db1, db2)

        item = AuditLogItem(name="log_test")
        item.save(db=db2)

        assert item._id is not None

        # Verify "create" audit entry
        logs = item.get_audit_log(db=db2)
        assert len(logs) == 1
        assert logs[0]["action"] == "create"

        # Update and check "update" audit entry
        item.name = "log_test_updated"
        item.save(db=db2)

        logs = item.get_audit_log(db=db2)
        assert len(logs) == 2
        assert logs[0]["action"] == "update"
        assert logs[0]["changes"]["name"]["old"] == "log_test"
        assert logs[0]["changes"]["name"]["new"] == "log_test_updated"
        assert logs[1]["action"] == "create"

        # Verify no data in db1
        assert db1.find_document("audit_log_items", item._id) is None

    def test_audit_log_mixin_delete_with_db(self):
        """AuditLogMixin.delete(db=db2) writes 'delete' audit entry to db2."""
        db1, db2 = make_db_pair()
        setup(AuditLogItem, db1, db2)

        item = AuditLogItem(name="del_log_test")
        item.save(db=db2)
        saved_id = item._id

        item.delete(db=db2)

        # Read the audit log directly from db2 since item._id is now None
        import json

        cursor = db2.adapter.execute(
            "SELECT action FROM audit_log_items_audit WHERE record_id = ? ORDER BY _id DESC;",
            [saved_id],
        )
        rows = cursor.fetchall()
        actions = [r[0] for r in rows]
        assert len(actions) == 2
        assert actions[0] == "delete"
        assert actions[1] == "create"

    def test_full_mixin_save_with_db(self):
        """FullMixin (Timestamp+SoftDelete+Hooks) save(db=db2) works end-to-end."""
        db1, db2 = make_db_pair()
        setup(FullItem, db1, db2)

        item = FullItem(name="full_test")
        item.save(db=db2)

        assert item._id is not None
        assert item.created_at is not None
        assert item.updated_at is not None

        doc = db2.find_document("full_items", item._id)
        assert doc is not None
        assert doc["name"] == "full_test"

        assert db1.find_document("full_items", item._id) is None

    def test_mixin_save_default_unchanged(self):
        """save() without db= still uses class-level binding (backward compat)."""
        db1, db2 = make_db_pair()
        setup(TSItem, db1, db2)

        item = TSItem(name="default_test")
        item.save()

        assert item._id is not None

        doc = db1.find_document("ts_items", item._id)
        assert doc is not None
        assert doc["name"] == "default_test"

        assert db2.find_document("ts_items", item._id) is None
