"""Tests for mixins with SQLerLiteModel (dataclass backend).

Verifies that all mixins work correctly when combined with the
Pydantic-free dataclass backend.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqler import SQLerDB, SQLerLiteModel
from sqler.models.mixins import (
    AuditLogMixin,
    AuditMixin,
    FullMixin,
    HooksMixin,
    SoftDeleteMixin,
    TimestampMixin,
)
from sqler.query import F

# ── Model fixtures ──────────────────────────────────────────────────────────


@dataclass
class LiteTSItem(TimestampMixin, SQLerLiteModel):
    __tablename__ = "lite_ts_items"
    name: str = ""


@dataclass
class LiteHookedItem(HooksMixin, SQLerLiteModel):
    __tablename__ = "lite_hooked_items"
    name: str = ""
    hook_log: list = field(default_factory=list)

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


@dataclass
class LiteVetoSaveItem(HooksMixin, SQLerLiteModel):
    __tablename__ = "lite_hooked_items"
    name: str = ""

    def before_save(self) -> bool:
        return False


@dataclass
class LiteSoftItem(SoftDeleteMixin, SQLerLiteModel):
    __tablename__ = "lite_soft_items"
    name: str = ""


@dataclass
class LiteAuditItem(AuditMixin, SQLerLiteModel):
    __tablename__ = "lite_audit_items"
    name: str = ""


@dataclass
class LiteAuditLogItem(AuditLogMixin, SQLerLiteModel):
    __tablename__ = "lite_audit_log_items"
    name: str = ""


@dataclass
class LiteFullItem(FullMixin, SQLerLiteModel):
    __tablename__ = "lite_full_items"
    name: str = ""


@dataclass
class LiteDisabledHooksItem(HooksMixin, SQLerLiteModel):
    __tablename__ = "lite_hooked_items"
    _hooks_enabled = False
    name: str = ""
    hook_log: list = field(default_factory=list)

    def before_save(self) -> bool:
        self.hook_log = [*self.hook_log, "before_save"]
        return False

    def after_save(self) -> None:
        self.hook_log = [*self.hook_log, "after_save"]


# ── Helpers ──────────────────────────────────────────────────────────────────

MAX_DELTA = timedelta(seconds=5)


def make_db_pair():
    db1 = SQLerDB.in_memory(shared=False)
    db2 = SQLerDB.in_memory(shared=False)
    return db1, db2


def setup(model_cls, db1, db2):
    with pytest.warns(DeprecationWarning):
        model_cls.set_db(db1)
    db2._ensure_table(model_cls._table)


def assert_recent_utc(dt, label="timestamp"):
    assert dt is not None, f"{label} should not be None"
    assert dt.tzinfo is not None, f"{label} should be timezone-aware"
    assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < MAX_DELTA.total_seconds(), (
        f"{label} should be within {MAX_DELTA} of now, got {dt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestLiteTimestampMixin:
    def test_save_sets_timestamps(self):
        db1, db2 = make_db_pair()
        setup(LiteTSItem, db1, db2)

        item = LiteTSItem(name="ts_test")
        item.save(db=db2)

        assert item._id > 0
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.created_at == item.updated_at

        doc = db2.find_document("lite_ts_items", item._id)
        assert doc is not None
        assert doc["name"] == "ts_test"
        assert db1.find_document("lite_ts_items", item._id) is None

    def test_created_at_immutable_on_update(self):
        db1, db2 = make_db_pair()
        setup(LiteTSItem, db1, db2)

        item = LiteTSItem(name="original")
        item.save(db=db2)
        original_created = item.created_at

        item.name = "updated"
        item.save(db=db2)

        assert item.created_at == original_created
        assert item.updated_at > original_created


class TestLiteHooksMixin:
    def test_save_fires_hooks(self):
        db1, db2 = make_db_pair()
        setup(LiteHookedItem, db1, db2)

        item = LiteHookedItem(name="hooked")
        item.save(db=db2)

        assert item._id > 0
        assert item.hook_log == ["before_save", "after_save"]

    def test_delete_fires_hooks(self):
        db1, db2 = make_db_pair()
        setup(LiteHookedItem, db1, db2)

        item = LiteHookedItem(name="del_test")
        item.save(db=db2)
        saved_id = item._id

        item.delete(db=db2)

        assert item.hook_log == ["before_save", "after_save", "before_delete", "after_delete"]
        assert item._id is None
        assert db2.find_document("lite_hooked_items", saved_id) is None

    def test_before_save_veto(self):
        db1, db2 = make_db_pair()
        setup(LiteVetoSaveItem, db1, db2)

        item = LiteVetoSaveItem(name="veto")
        with pytest.raises(RuntimeError, match="before_save\\(\\) returned False"):
            item.save(db=db2)

    def test_hooks_disabled_bypasses(self):
        db1, db2 = make_db_pair()
        setup(LiteDisabledHooksItem, db1, db2)

        item = LiteDisabledHooksItem(name="no_hooks")
        item.save(db=db2)

        assert item._id > 0
        assert item.hook_log == []


class TestLiteSoftDeleteMixin:
    def test_soft_delete_and_restore(self):
        db1, db2 = make_db_pair()
        setup(LiteSoftItem, db1, db2)

        item = LiteSoftItem(name="soft")
        item.save(db=db2)

        item.soft_delete(db=db2)
        assert item.is_deleted is True
        assert_recent_utc(item.deleted_at, "deleted_at")

        item.restore(db=db2)
        assert item.is_deleted is False
        assert item.deleted_at is None

    def test_hard_delete(self):
        db1, db2 = make_db_pair()
        setup(LiteSoftItem, db1, db2)

        item = LiteSoftItem(name="hard")
        item.save(db=db2)
        saved_id = item._id

        item.hard_delete(db=db2)
        assert item._id is None
        assert db2.find_document("lite_soft_items", saved_id) is None

    def test_active_with_deleted_only_deleted(self):
        db1, db2 = make_db_pair()
        setup(LiteSoftItem, db1, db2)

        assert len(LiteSoftItem.with_deleted().all()) == 0

        LiteSoftItem(name="alpha").save()
        LiteSoftItem(name="beta").save()
        LiteSoftItem(name="gamma").save()

        beta = LiteSoftItem.query().filter(F("name") == "beta").first()
        beta.soft_delete()

        active = LiteSoftItem.active().all()
        assert len(active) == 2
        assert sorted(i.name for i in active) == ["alpha", "gamma"]

        with_del = LiteSoftItem.with_deleted().all()
        assert len(with_del) == 3

        only_del = LiteSoftItem.only_deleted().all()
        assert len(only_del) == 1
        assert only_del[0].name == "beta"


class TestLiteAuditMixin:
    def test_created_by_updated_by(self):
        db1, db2 = make_db_pair()
        setup(LiteAuditItem, db1, db2)

        AuditMixin.set_current_user("test_user")
        try:
            item = LiteAuditItem(name="audit")
            item.save(db=db2)

            assert item.created_by == "test_user"
            assert item.updated_by == "test_user"
            assert_recent_utc(item.created_at, "created_at")
            assert_recent_utc(item.updated_at, "updated_at")
        finally:
            AuditMixin.set_current_user(None)

    def test_created_by_immutable_on_update(self):
        db1, db2 = make_db_pair()
        setup(LiteAuditItem, db1, db2)

        AuditMixin.set_current_user("creator")
        try:
            item = LiteAuditItem(name="original")
            item.save(db=db2)
            assert item.created_by == "creator"

            AuditMixin.set_current_user("updater")
            item.name = "updated"
            item.save(db=db2)

            assert item.created_by == "creator"
            assert item.updated_by == "updater"
        finally:
            AuditMixin.set_current_user(None)

    def test_custom_user_getter(self):
        db1, db2 = make_db_pair()
        setup(LiteAuditItem, db1, db2)

        AuditMixin.set_current_user_getter(lambda: "getter_user")
        try:
            item = LiteAuditItem(name="getter_test")
            item.save(db=db2)

            assert item.created_by == "getter_user"
            assert item.updated_by == "getter_user"
        finally:
            AuditMixin.set_current_user_getter(None)


class TestLiteAuditLogMixin:
    def test_create_logged(self):
        db1, db2 = make_db_pair()
        setup(LiteAuditLogItem, db1, db2)

        item = LiteAuditLogItem(name="log_test")
        item.save(db=db2)

        logs = item.get_audit_log(db=db2)
        assert len(logs) == 1
        assert logs[0]["action"] == "create"
        assert logs[0]["snapshot"]["name"] == "log_test"

    def test_update_logged_with_changes(self):
        db1, db2 = make_db_pair()
        setup(LiteAuditLogItem, db1, db2)

        item = LiteAuditLogItem(name="original")
        item.save(db=db2)

        item.name = "updated"
        item.save(db=db2)

        logs = item.get_audit_log(db=db2)
        assert len(logs) == 2
        assert logs[0]["action"] == "update"
        assert logs[0]["changes"]["name"]["old"] == "original"
        assert logs[0]["changes"]["name"]["new"] == "updated"
        assert logs[1]["action"] == "create"

    def test_delete_logged(self):
        db1, db2 = make_db_pair()
        setup(LiteAuditLogItem, db1, db2)

        item = LiteAuditLogItem(name="del_log")
        item.save(db=db2)
        saved_id = item._id

        item.delete(db=db2)

        cursor = db2.adapter.execute(
            "SELECT action FROM lite_audit_log_items_audit WHERE record_id = ? ORDER BY _id DESC;",
            [saved_id],
        )
        rows = cursor.fetchall()
        actions = [r[0] for r in rows]
        assert len(actions) == 2
        assert actions[0] == "delete"
        assert actions[1] == "create"

    def test_no_entry_on_unchanged_save(self):
        db1, db2 = make_db_pair()
        setup(LiteAuditLogItem, db1, db2)

        item = LiteAuditLogItem(name="unchanged")
        item.save(db=db2)

        item.save(db=db2)

        logs = item.get_audit_log(db=db2)
        assert len(logs) == 1
        assert logs[0]["action"] == "create"


class TestLiteFullMixin:
    def test_full_mixin_save(self):
        db1, db2 = make_db_pair()
        setup(LiteFullItem, db1, db2)

        item = LiteFullItem(name="full")
        item.save(db=db2)

        assert item._id > 0
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.deleted_at is None
        assert item.is_deleted is False

        doc = db2.find_document("lite_full_items", item._id)
        assert doc is not None
        assert doc["name"] == "full"
