"""Async tests for mixin db= forwarding to save()/delete().

Verifies that every async mixin correctly accepts and forwards the db=
parameter for per-call DB routing.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.query import F
from sqler.models.mixins import (
    AsyncAuditLogMixin,
    AsyncAuditMixin,
    AsyncFullMixin,
    AsyncHooksMixin,
    AsyncSoftDeleteMixin,
    TimestampMixin,
)


# ── Model fixtures ──────────────────────────────────────────────────────────


class ATSItem(TimestampMixin, AsyncSQLerModel):
    __tablename__ = "ats_items"
    name: str


class AHookedItem(AsyncHooksMixin, AsyncSQLerModel):
    __tablename__ = "ahooked_items"
    name: str
    hook_log: list[str] = []

    async def before_save(self) -> bool:
        self.hook_log = [*self.hook_log, "before_save"]
        return True

    async def after_save(self) -> None:
        self.hook_log = [*self.hook_log, "after_save"]

    async def before_delete(self) -> bool:
        self.hook_log = [*self.hook_log, "before_delete"]
        return True

    async def after_delete(self) -> None:
        self.hook_log = [*self.hook_log, "after_delete"]


class AVetoSaveItem(AsyncHooksMixin, AsyncSQLerModel):
    __tablename__ = "ahooked_items"
    name: str

    async def before_save(self) -> bool:
        return False


class AVetoDeleteItem(AsyncHooksMixin, AsyncSQLerModel):
    __tablename__ = "ahooked_items"
    name: str

    async def before_delete(self) -> bool:
        return False


class ASoftItem(AsyncSoftDeleteMixin, AsyncSQLerModel):
    __tablename__ = "asoft_items"
    name: str


class AAuditItem(AsyncAuditMixin, AsyncSQLerModel):
    __tablename__ = "aaudit_items"
    name: str


class AAuditLogItem(AsyncAuditLogMixin, AsyncSQLerModel):
    __tablename__ = "aaudit_log_items"
    name: str


class AFullItem(AsyncFullMixin, AsyncSQLerModel):
    __tablename__ = "afull_items"
    name: str


class ADisabledHooksVetoItem(AsyncHooksMixin, AsyncSQLerModel):
    """AsyncHooksMixin with hooks disabled and a vetoing before_save."""

    __tablename__ = "ahooked_items"
    _hooks_enabled = False
    name: str

    async def before_save(self) -> bool:
        return False


class ATrackedFullItem(AsyncFullMixin, AsyncSQLerModel):
    """AsyncFullMixin with hook tracking for delete path tests."""

    __tablename__ = "atracked_full_items"
    name: str
    hook_log: list[str] = []

    async def before_save(self) -> bool:
        self.hook_log = [*self.hook_log, "before_save"]
        return True

    async def after_save(self) -> None:
        self.hook_log = [*self.hook_log, "after_save"]

    async def before_delete(self) -> bool:
        self.hook_log = [*self.hook_log, "before_delete"]
        return True

    async def after_delete(self) -> None:
        self.hook_log = [*self.hook_log, "after_delete"]


# ── Helpers ──────────────────────────────────────────────────────────────────

MAX_DELTA = timedelta(seconds=5)


@pytest_asyncio.fixture
async def db_pair():
    """Create two independent async in-memory databases with cleanup."""
    db1 = AsyncSQLerDB.in_memory(shared=True, name="mixin_db1")
    db2 = AsyncSQLerDB.in_memory(shared=True, name="mixin_db2")
    await db1.connect()
    await db2.connect()
    try:
        yield db1, db2
    finally:
        await db1.close()
        await db2.close()


async def setup(model_cls, db1, db2):
    """Bind model to db1 and ensure table in both."""
    model_cls.set_db(db1)
    await db1._ensure_table(model_cls._table)
    await db2._ensure_table(model_cls._table)


def assert_recent_utc(dt, label="timestamp"):
    """Assert dt is a UTC-aware datetime within MAX_DELTA of now."""
    assert dt is not None, f"{label} should not be None"
    assert dt.tzinfo is not None, f"{label} should be timezone-aware"
    assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < MAX_DELTA.total_seconds(), (
        f"{label} should be within {MAX_DELTA} of now, got {dt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TestAsyncMixinDbForwarding
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncMixinDbForwarding:
    """Verify every async mixin correctly forwards db= through its save/delete chain."""

    @pytest.mark.asyncio
    async def test_timestamp_mixin_save_with_db(self, db_pair):
        """TimestampMixin.save(db=db2) writes to db2 with UTC timestamps."""
        db1, db2 = db_pair
        await setup(ATSItem, db1, db2)

        item = ATSItem(name="ts_test")
        await item.save(db=db2)

        assert item._id > 0
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.created_at == item.updated_at

        doc = await db2.find_document("ats_items", item._id)
        assert doc is not None
        assert doc["name"] == "ts_test"

        assert (await db1.find_document("ats_items", item._id)) is None

    @pytest.mark.asyncio
    async def test_hooks_mixin_save_with_db(self, db_pair):
        """AsyncHooksMixin.save(db=db2) fires hooks and writes to db2."""
        db1, db2 = db_pair
        await setup(AHookedItem, db1, db2)

        item = AHookedItem(name="hooked_test")
        await item.save(db=db2)

        assert item._id > 0
        assert item.hook_log == ["before_save", "after_save"]

        doc = await db2.find_document("ahooked_items", item._id)
        assert doc is not None
        assert doc["name"] == "hooked_test"

        assert (await db1.find_document("ahooked_items", item._id)) is None

    @pytest.mark.asyncio
    async def test_hooks_mixin_delete_with_db(self, db_pair):
        """AsyncHooksMixin.delete(db=db2) fires hooks and deletes from db2."""
        db1, db2 = db_pair
        await setup(AHookedItem, db1, db2)

        item = AHookedItem(name="delete_test")
        await item.save(db=db2)
        saved_id = item._id

        await item.delete(db=db2)

        assert item.hook_log == ["before_save", "after_save", "before_delete", "after_delete"]
        assert item._id is None
        assert (await db2.find_document("ahooked_items", saved_id)) is None

    @pytest.mark.asyncio
    async def test_hooks_save_veto_with_db(self, db_pair):
        """before_save() returning False raises RuntimeError with db=."""
        db1, db2 = db_pair
        await setup(AVetoSaveItem, db1, db2)

        item = AVetoSaveItem(name="veto")
        with pytest.raises(RuntimeError, match="before_save\\(\\) returned False"):
            await item.save(db=db2)

        # Nothing written to either db
        all_db1 = await AVetoSaveItem.using(db1).all()
        all_db2 = await AVetoSaveItem.using(db2).all()
        assert len(all_db1) == 0
        assert len(all_db2) == 0

    @pytest.mark.asyncio
    async def test_hooks_delete_veto_with_db(self, db_pair):
        """before_delete() returning False raises RuntimeError with db=."""
        db1, db2 = db_pair
        await setup(AVetoDeleteItem, db1, db2)

        # Save via base class to bypass delete hook only
        item = AVetoDeleteItem(name="veto_del")
        await AsyncHooksMixin.save(item, db=db2)
        saved_id = item._id

        with pytest.raises(RuntimeError, match="before_delete\\(\\) returned False"):
            await item.delete(db=db2)

        # Record still exists
        assert (await db2.find_document("ahooked_items", saved_id)) is not None

    @pytest.mark.asyncio
    async def test_audit_mixin_save_with_db(self, db_pair):
        """AsyncAuditMixin.save(db=db2) sets created_by/updated_by in db2."""
        db1, db2 = db_pair
        await setup(AAuditItem, db1, db2)

        AsyncAuditMixin.set_current_user("async_test_user")
        try:
            item = AAuditItem(name="audit_test")
            await item.save(db=db2)

            assert item._id > 0
            assert item.created_by == "async_test_user"
            assert item.updated_by == "async_test_user"
            assert_recent_utc(item.created_at, "created_at")
            assert_recent_utc(item.updated_at, "updated_at")
            assert item.created_at == item.updated_at

            doc = await db2.find_document("aaudit_items", item._id)
            assert doc is not None
            assert doc["name"] == "audit_test"

            assert (await db1.find_document("aaudit_items", item._id)) is None
        finally:
            AsyncAuditMixin.set_current_user(None)

    @pytest.mark.asyncio
    async def test_audit_mixin_update_preserves_created_by_with_db(self, db_pair):
        """created_by is immutable across updates when using db=."""
        db1, db2 = db_pair
        await setup(AAuditItem, db1, db2)

        AsyncAuditMixin.set_current_user("creator")
        try:
            item = AAuditItem(name="original")
            await item.save(db=db2)
            assert item.created_by == "creator"

            AsyncAuditMixin.set_current_user("updater")
            item.name = "updated"
            await item.save(db=db2)

            assert item.created_by == "creator"  # must not change
            assert item.updated_by == "updater"  # must change
        finally:
            AsyncAuditMixin.set_current_user(None)

    @pytest.mark.asyncio
    async def test_audit_mixin_custom_getter_with_db(self, db_pair):
        """AsyncAuditMixin respects set_current_user_getter() when forwarding db=."""
        db1, db2 = db_pair
        await setup(AAuditItem, db1, db2)

        AsyncAuditMixin.set_current_user_getter(lambda: "getter_user")
        try:
            item = AAuditItem(name="getter_test")
            await item.save(db=db2)

            assert item.created_by == "getter_user"
            assert item.updated_by == "getter_user"

            assert (await db1.find_document("aaudit_items", item._id)) is None
        finally:
            AsyncAuditMixin.set_current_user_getter(None)

    @pytest.mark.asyncio
    async def test_soft_delete_with_db(self, db_pair):
        """AsyncSoftDeleteMixin.soft_delete(db=db2) sets deleted_at in db2."""
        db1, db2 = db_pair
        await setup(ASoftItem, db1, db2)

        item = ASoftItem(name="soft_test")
        await item.save(db=db2)

        await item.soft_delete(db=db2)

        assert item.is_deleted is True
        assert_recent_utc(item.deleted_at, "deleted_at")

        doc = await db2.find_document("asoft_items", item._id)
        assert doc is not None
        assert doc["deleted_at"] == item.deleted_at.isoformat()

        # db1 isolation
        assert (await db1.find_document("asoft_items", item._id)) is None

    @pytest.mark.asyncio
    async def test_soft_delete_restore_with_db(self, db_pair):
        """AsyncSoftDeleteMixin.restore(db=db2) clears deleted_at in db2."""
        db1, db2 = db_pair
        await setup(ASoftItem, db1, db2)

        item = ASoftItem(name="restore_test")
        await item.save(db=db2)
        await item.soft_delete(db=db2)

        assert item.is_deleted is True

        await item.restore(db=db2)

        assert item.is_deleted is False
        assert item.deleted_at is None

        doc = await db2.find_document("asoft_items", item._id)
        assert doc is not None
        assert doc["deleted_at"] is None

        # db1 isolation
        assert (await db1.find_document("asoft_items", item._id)) is None

    @pytest.mark.asyncio
    async def test_soft_delete_hard_delete_with_db(self, db_pair):
        """AsyncSoftDeleteMixin.hard_delete(db=db2) permanently removes from db2."""
        db1, db2 = db_pair
        await setup(ASoftItem, db1, db2)

        item = ASoftItem(name="hard_test")
        await item.save(db=db2)
        saved_id = item._id

        await item.hard_delete(db=db2)

        assert item._id is None
        assert (await db2.find_document("asoft_items", saved_id)) is None

    @pytest.mark.asyncio
    async def test_audit_log_mixin_save_with_db(self, db_pair):
        """AsyncAuditLogMixin.save(db=db2) writes audit entries to db2."""
        db1, db2 = db_pair
        await setup(AAuditLogItem, db1, db2)

        item = AAuditLogItem(name="log_test")
        await item.save(db=db2)

        assert item._id > 0

        # Verify "create" audit entry
        logs = await item.get_audit_log(db=db2)
        assert len(logs) == 1
        assert logs[0]["action"] == "create"

        # Update and check "update" audit entry
        item.name = "log_test_updated"
        await item.save(db=db2)

        logs = await item.get_audit_log(db=db2)
        assert len(logs) == 2
        assert logs[0]["action"] == "update"
        assert logs[0]["changes"]["name"]["old"] == "log_test"
        assert logs[0]["changes"]["name"]["new"] == "log_test_updated"
        assert logs[1]["action"] == "create"

        # Verify no data in db1
        assert (await db1.find_document("aaudit_log_items", item._id)) is None

    @pytest.mark.asyncio
    async def test_audit_log_mixin_delete_with_db(self, db_pair):
        """AsyncAuditLogMixin.delete(db=db2) writes 'delete' audit to db2."""
        db1, db2 = db_pair
        await setup(AAuditLogItem, db1, db2)

        item = AAuditLogItem(name="del_log_test")
        await item.save(db=db2)
        saved_id = item._id

        await item.delete(db=db2)

        # Read audit log directly since item._id is now None
        cursor = await db2.adapter.execute(
            "SELECT action FROM aaudit_log_items_audit WHERE record_id = ? ORDER BY _id DESC;",
            [saved_id],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        actions = [r[0] for r in rows]
        assert len(actions) == 2
        assert actions[0] == "delete"
        assert actions[1] == "create"

    @pytest.mark.asyncio
    async def test_full_mixin_save_with_db(self, db_pair):
        """AsyncFullMixin save(db=db2) works end-to-end."""
        db1, db2 = db_pair
        await setup(AFullItem, db1, db2)

        item = AFullItem(name="full_test")
        await item.save(db=db2)

        assert item._id > 0
        # TimestampMixin contract
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.created_at == item.updated_at
        # SoftDeleteMixin contract: initially not deleted
        assert item.deleted_at is None
        assert item.is_deleted is False

        doc = await db2.find_document("afull_items", item._id)
        assert doc is not None
        assert doc["name"] == "full_test"

        assert (await db1.find_document("afull_items", item._id)) is None

    @pytest.mark.asyncio
    async def test_mixin_save_default_unchanged(self, db_pair):
        """save() without db= still uses class-level binding (backward compat)."""
        db1, db2 = db_pair
        await setup(ATSItem, db1, db2)

        item = ATSItem(name="default_test")
        await item.save()

        assert item._id > 0

        doc = await db1.find_document("ats_items", item._id)
        assert doc is not None
        assert doc["name"] == "default_test"

        assert (await db2.find_document("ats_items", item._id)) is None

    # ── Gap coverage tests ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_audit_fields_persist_to_db(self, db_pair):
        """AsyncAuditMixin fields survive DB round-trip (not just in-memory)."""
        db1, db2 = db_pair
        await setup(AAuditItem, db1, db2)

        AsyncAuditMixin.set_current_user("persist_user")
        try:
            item = AAuditItem(name="persist_test")
            await item.save(db=db2)

            doc = await db2.find_document("aaudit_items", item._id)
            assert doc["created_by"] == "persist_user"
            assert doc["updated_by"] == "persist_user"
            assert doc["created_at"] == item.created_at.isoformat()
            assert doc["updated_at"] == item.updated_at.isoformat()
        finally:
            AsyncAuditMixin.set_current_user(None)

    @pytest.mark.asyncio
    async def test_audit_log_entry_fields(self, db_pair):
        """AsyncAuditLogMixin log entries contain user, timestamp, and snapshot."""
        db1, db2 = db_pair
        await setup(AAuditLogItem, db1, db2)

        AsyncAuditMixin.set_current_user("log_user")
        try:
            item = AAuditLogItem(name="log_fields_test")
            await item.save(db=db2)

            logs = await item.get_audit_log(db=db2)
            assert len(logs) == 1
            assert logs[0]["user"] == "log_user"
            assert isinstance(logs[0]["timestamp"], str)
            # Verify ISO format by parsing
            datetime.fromisoformat(logs[0]["timestamp"])
            assert logs[0]["snapshot"]["name"] == "log_fields_test"
        finally:
            AsyncAuditMixin.set_current_user(None)

    @pytest.mark.asyncio
    async def test_soft_delete_class_methods_filter_correctly(self, db_pair):
        """active(), with_deleted(), only_deleted() return correct filtered sets."""
        db1, db2 = db_pair
        await setup(ASoftItem, db1, db2)

        # Save to class-bound db (db1) for class method queries
        await ASoftItem(name="alpha").save()
        await ASoftItem(name="beta").save()
        await ASoftItem(name="gamma").save()

        # Soft-delete beta via queryset lookup
        beta = await ASoftItem.query().filter(F("name") == "beta").first()
        await beta.soft_delete()

        active = await ASoftItem.active().all()
        assert len(active) == 2
        active_names = sorted(i.name for i in active)
        assert active_names == ["alpha", "gamma"]

        with_del = await ASoftItem.with_deleted().all()
        assert len(with_del) == 3
        all_names = sorted(i.name for i in with_del)
        assert all_names == ["alpha", "beta", "gamma"]

        only_del = await ASoftItem.only_deleted().all()
        assert len(only_del) == 1
        assert only_del[0].name == "beta"

    @pytest.mark.asyncio
    async def test_audit_log_no_entry_on_unchanged_save(self, db_pair):
        """Re-saving without changes produces no new audit entry."""
        db1, db2 = db_pair
        await setup(AAuditLogItem, db1, db2)

        item = AAuditLogItem(name="unchanged_test")
        await item.save(db=db2)

        logs = await item.get_audit_log(db=db2)
        assert len(logs) == 1
        assert logs[0]["action"] == "create"

        # Re-save without changes
        await item.save(db=db2)

        logs = await item.get_audit_log(db=db2)
        assert len(logs) == 1  # Still only the "create" entry
        assert logs[0]["action"] == "create"

    @pytest.mark.asyncio
    async def test_hooks_disabled_bypasses_veto(self, db_pair):
        """_hooks_enabled = False skips veto hook entirely."""
        db1, db2 = db_pair
        await setup(ADisabledHooksVetoItem, db1, db2)

        item = ADisabledHooksVetoItem(name="no_veto")
        await item.save(db=db2)  # Should succeed despite before_save returning False

        assert item._id > 0
        doc = await db2.find_document("ahooked_items", item._id)
        assert doc is not None
        assert doc["name"] == "no_veto"

    @pytest.mark.asyncio
    async def test_audit_mixin_raising_getter_produces_none(self, db_pair):
        """Getter that raises exception results in created_by/updated_by = None."""
        db1, db2 = db_pair
        await setup(AAuditItem, db1, db2)

        def raising_getter():
            raise ValueError("simulated failure")

        AsyncAuditMixin.set_current_user_getter(raising_getter)
        try:
            item = AAuditItem(name="raise_test")
            await item.save(db=db2)

            assert item.created_by is None
            assert item.updated_by is None

            doc = await db2.find_document("aaudit_items", item._id)
            assert doc["created_by"] is None
            assert doc["updated_by"] is None
        finally:
            AsyncAuditMixin.set_current_user_getter(None)

    @pytest.mark.asyncio
    async def test_full_mixin_hard_delete_with_db(self, db_pair):
        """AsyncFullMixin hard_delete(db=db2) fires hooks and removes from db2."""
        db1, db2 = db_pair
        await setup(ATrackedFullItem, db1, db2)

        item = ATrackedFullItem(name="full_del")
        await item.save(db=db2)
        saved_id = item._id
        assert saved_id > 0

        await item.hard_delete(db=db2)

        assert item._id is None
        assert item.hook_log == ["before_save", "after_save", "before_delete", "after_delete"]
        assert (await db2.find_document("atracked_full_items", saved_id)) is None
