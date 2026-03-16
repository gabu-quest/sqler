"""Tests for mixins with SQLerMsgspecModel (msgspec Struct backend).

Verifies that all mixins work correctly when combined with the
msgspec Struct backend.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqler import SQLerDB
from sqler.models._compat import MSGSPEC_AVAILABLE
from sqler.models.mixins import (
    AuditMixin,
    FullMixin,
    HooksMixin,
    SoftDeleteMixin,
    TimestampMixin,
)
from sqler.query import F

pytestmark = pytest.mark.skipif(
    not MSGSPEC_AVAILABLE, reason="msgspec not installed"
)

if MSGSPEC_AVAILABLE:
    from sqler.models.msgspec import SQLerMsgspecModel

    class MsgTSItem(TimestampMixin, SQLerMsgspecModel, tag=False):
        __tablename__ = "msg_ts_items"
        name: str

    class MsgHookedItem(HooksMixin, SQLerMsgspecModel, tag=False):
        __tablename__ = "msg_hooked_items"
        name: str
        hook_log: list = []

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

    class MsgVetoSaveItem(HooksMixin, SQLerMsgspecModel, tag=False):
        __tablename__ = "msg_hooked_items"
        name: str

        def before_save(self) -> bool:
            return False

    class MsgSoftItem(SoftDeleteMixin, SQLerMsgspecModel, tag=False):
        __tablename__ = "msg_soft_items"
        name: str

    class MsgAuditItem(AuditMixin, SQLerMsgspecModel, tag=False):
        __tablename__ = "msg_audit_items"
        name: str

    class MsgFullItem(FullMixin, SQLerMsgspecModel, tag=False):
        __tablename__ = "msg_full_items"
        name: str


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


class TestMsgspecTimestampMixin:
    def test_save_sets_timestamps(self):
        db1, db2 = make_db_pair()
        setup(MsgTSItem, db1, db2)

        item = MsgTSItem(name="ts_test")
        item.save(db=db2)

        assert item._id > 0
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.created_at == item.updated_at

    def test_created_at_immutable_on_update(self):
        db1, db2 = make_db_pair()
        setup(MsgTSItem, db1, db2)

        item = MsgTSItem(name="original")
        item.save(db=db2)
        original_created = item.created_at

        item.name = "updated"
        item.save(db=db2)

        assert item.created_at == original_created
        assert item.updated_at > original_created


class TestMsgspecHooksMixin:
    def test_save_fires_hooks(self):
        db1, db2 = make_db_pair()
        setup(MsgHookedItem, db1, db2)

        item = MsgHookedItem(name="hooked")
        item.save(db=db2)

        assert item._id > 0
        assert item.hook_log == ["before_save", "after_save"]

    def test_delete_fires_hooks(self):
        db1, db2 = make_db_pair()
        setup(MsgHookedItem, db1, db2)

        item = MsgHookedItem(name="del_test")
        item.save(db=db2)
        saved_id = item._id

        item.delete(db=db2)

        assert item.hook_log == ["before_save", "after_save", "before_delete", "after_delete"]
        assert item._id is None
        assert db2.find_document("msg_hooked_items", saved_id) is None

    def test_before_save_veto(self):
        db1, db2 = make_db_pair()
        setup(MsgVetoSaveItem, db1, db2)

        item = MsgVetoSaveItem(name="veto")
        with pytest.raises(RuntimeError, match="before_save\\(\\) returned False"):
            item.save(db=db2)


class TestMsgspecSoftDeleteMixin:
    def test_soft_delete_and_restore(self):
        db1, db2 = make_db_pair()
        setup(MsgSoftItem, db1, db2)

        item = MsgSoftItem(name="soft")
        item.save(db=db2)

        item.soft_delete(db=db2)
        assert item.is_deleted is True
        assert_recent_utc(item.deleted_at, "deleted_at")

        item.restore(db=db2)
        assert item.is_deleted is False
        assert item.deleted_at is None

    def test_hard_delete(self):
        db1, db2 = make_db_pair()
        setup(MsgSoftItem, db1, db2)

        item = MsgSoftItem(name="hard")
        item.save(db=db2)
        saved_id = item._id

        item.hard_delete(db=db2)
        assert item._id is None
        assert db2.find_document("msg_soft_items", saved_id) is None

    def test_active_with_deleted_only_deleted(self):
        db1, db2 = make_db_pair()
        setup(MsgSoftItem, db1, db2)

        assert len(MsgSoftItem.with_deleted().all()) == 0

        MsgSoftItem(name="alpha").save()
        MsgSoftItem(name="beta").save()
        MsgSoftItem(name="gamma").save()

        beta = MsgSoftItem.query().filter(F("name") == "beta").first()
        beta.soft_delete()

        active = MsgSoftItem.active().all()
        assert len(active) == 2
        assert sorted(i.name for i in active) == ["alpha", "gamma"]

        with_del = MsgSoftItem.with_deleted().all()
        assert len(with_del) == 3

        only_del = MsgSoftItem.only_deleted().all()
        assert len(only_del) == 1
        assert only_del[0].name == "beta"


class TestMsgspecAuditMixin:
    def test_created_by_updated_by(self):
        db1, db2 = make_db_pair()
        setup(MsgAuditItem, db1, db2)

        AuditMixin.set_current_user("test_user")
        try:
            item = MsgAuditItem(name="audit")
            item.save(db=db2)

            assert item.created_by == "test_user"
            assert item.updated_by == "test_user"
            assert_recent_utc(item.created_at, "created_at")
            assert_recent_utc(item.updated_at, "updated_at")
        finally:
            AuditMixin.set_current_user(None)

    def test_created_by_immutable_on_update(self):
        db1, db2 = make_db_pair()
        setup(MsgAuditItem, db1, db2)

        AuditMixin.set_current_user("creator")
        try:
            item = MsgAuditItem(name="original")
            item.save(db=db2)

            AuditMixin.set_current_user("updater")
            item.name = "updated"
            item.save(db=db2)

            assert item.created_by == "creator"
            assert item.updated_by == "updater"
        finally:
            AuditMixin.set_current_user(None)


class TestMsgspecFullMixin:
    def test_full_mixin_save(self):
        db1, db2 = make_db_pair()
        setup(MsgFullItem, db1, db2)

        item = MsgFullItem(name="full")
        item.save(db=db2)

        assert item._id > 0
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.deleted_at is None
        assert item.is_deleted is False
