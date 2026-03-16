"""Async tests for mixins with AsyncSQLerLiteModel (dataclass backend).

Verifies that async mixin variants work correctly when combined with
the Pydantic-free async dataclass backend.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqler import AsyncSQLerLiteModel
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.mixins import (
    AsyncAuditLogMixin,
    AsyncAuditMixin,
    AsyncFullMixin,
    AsyncHooksMixin,
    AsyncSoftDeleteMixin,
    TimestampMixin,
)


# ── Model fixtures ──────────────────────────────────────────────────────────


@dataclass
class ALiteTSItem(TimestampMixin, AsyncSQLerLiteModel):
    __tablename__ = "alite_ts_items"
    name: str = ""


@dataclass
class ALiteHookedItem(AsyncHooksMixin, AsyncSQLerLiteModel):
    __tablename__ = "alite_hooked_items"
    name: str = ""
    hook_log: list = field(default_factory=list)

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


@dataclass
class ALiteVetoSaveItem(AsyncHooksMixin, AsyncSQLerLiteModel):
    __tablename__ = "alite_hooked_items"
    name: str = ""

    async def before_save(self) -> bool:
        return False


@dataclass
class ALiteSoftItem(AsyncSoftDeleteMixin, AsyncSQLerLiteModel):
    __tablename__ = "alite_soft_items"
    name: str = ""


@dataclass
class ALiteAuditItem(AsyncAuditMixin, AsyncSQLerLiteModel):
    __tablename__ = "alite_audit_items"
    name: str = ""


@dataclass
class ALiteAuditLogItem(AsyncAuditLogMixin, AsyncSQLerLiteModel):
    __tablename__ = "alite_audit_log_items"
    name: str = ""


@dataclass
class ALiteFullItem(AsyncFullMixin, AsyncSQLerLiteModel):
    __tablename__ = "alite_full_items"
    name: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

MAX_DELTA = timedelta(seconds=5)


async def make_async_db_pair():
    db1 = AsyncSQLerDB.in_memory(shared=False)
    db2 = AsyncSQLerDB.in_memory(shared=False)
    await db1.connect()
    await db2.connect()
    return db1, db2


async def async_setup(model_cls, db1, db2):
    with pytest.warns(DeprecationWarning):
        model_cls.set_db(db1)
    await db1._ensure_table(model_cls._table)
    await db2._ensure_table(model_cls._table)


def assert_recent_utc(dt, label="timestamp"):
    assert dt is not None, f"{label} should not be None"
    assert dt.tzinfo is not None, f"{label} should be timezone-aware"
    assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < MAX_DELTA.total_seconds(), (
        f"{label} should be within {MAX_DELTA} of now, got {dt}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncLiteTimestampMixin:
    @pytest.mark.asyncio
    async def test_save_sets_timestamps(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteTSItem, db1, db2)

        item = ALiteTSItem(name="ts_test")
        await item.save(db=db2)

        assert item._id > 0
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.created_at == item.updated_at

        doc = await db2.find_document("alite_ts_items", item._id)
        assert doc is not None
        assert doc["name"] == "ts_test"

        await db1.close()
        await db2.close()


class TestAsyncLiteHooksMixin:
    @pytest.mark.asyncio
    async def test_save_fires_hooks(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteHookedItem, db1, db2)

        item = ALiteHookedItem(name="hooked")
        await item.save(db=db2)

        assert item._id > 0
        assert item.hook_log == ["before_save", "after_save"]

        await db1.close()
        await db2.close()

    @pytest.mark.asyncio
    async def test_delete_fires_hooks(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteHookedItem, db1, db2)

        item = ALiteHookedItem(name="del_test")
        await item.save(db=db2)
        saved_id = item._id

        await item.delete(db=db2)

        assert item.hook_log == ["before_save", "after_save", "before_delete", "after_delete"]
        assert item._id is None
        assert await db2.find_document("alite_hooked_items", saved_id) is None

        await db1.close()
        await db2.close()

    @pytest.mark.asyncio
    async def test_before_save_veto(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteVetoSaveItem, db1, db2)

        item = ALiteVetoSaveItem(name="veto")
        with pytest.raises(RuntimeError, match="before_save\\(\\) returned False"):
            await item.save(db=db2)

        await db1.close()
        await db2.close()


class TestAsyncLiteSoftDeleteMixin:
    @pytest.mark.asyncio
    async def test_soft_delete_and_restore(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteSoftItem, db1, db2)

        item = ALiteSoftItem(name="soft")
        await item.save(db=db2)

        await item.soft_delete(db=db2)
        assert item.is_deleted is True
        assert_recent_utc(item.deleted_at, "deleted_at")

        await item.restore(db=db2)
        assert item.is_deleted is False
        assert item.deleted_at is None

        await db1.close()
        await db2.close()

    @pytest.mark.asyncio
    async def test_hard_delete(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteSoftItem, db1, db2)

        item = ALiteSoftItem(name="hard")
        await item.save(db=db2)
        saved_id = item._id

        await item.hard_delete(db=db2)
        assert item._id is None
        assert await db2.find_document("alite_soft_items", saved_id) is None

        await db1.close()
        await db2.close()


class TestAsyncLiteAuditMixin:
    @pytest.mark.asyncio
    async def test_created_by_updated_by(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteAuditItem, db1, db2)

        AsyncAuditMixin.set_current_user("test_user")
        try:
            item = ALiteAuditItem(name="audit")
            await item.save(db=db2)

            assert item.created_by == "test_user"
            assert item.updated_by == "test_user"
            assert_recent_utc(item.created_at, "created_at")
            assert_recent_utc(item.updated_at, "updated_at")
        finally:
            AsyncAuditMixin.set_current_user(None)
            await db1.close()
            await db2.close()

    @pytest.mark.asyncio
    async def test_created_by_immutable_on_update(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteAuditItem, db1, db2)

        AsyncAuditMixin.set_current_user("creator")
        try:
            item = ALiteAuditItem(name="original")
            await item.save(db=db2)

            AsyncAuditMixin.set_current_user("updater")
            item.name = "updated"
            await item.save(db=db2)

            assert item.created_by == "creator"
            assert item.updated_by == "updater"
        finally:
            AsyncAuditMixin.set_current_user(None)
            await db1.close()
            await db2.close()


class TestAsyncLiteAuditLogMixin:
    @pytest.mark.asyncio
    async def test_create_logged(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteAuditLogItem, db1, db2)

        item = ALiteAuditLogItem(name="log_test")
        await item.save(db=db2)

        logs = await item.get_audit_log(db=db2)
        assert len(logs) == 1
        assert logs[0]["action"] == "create"
        assert logs[0]["snapshot"]["name"] == "log_test"

        await db1.close()
        await db2.close()

    @pytest.mark.asyncio
    async def test_update_logged_with_changes(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteAuditLogItem, db1, db2)

        item = ALiteAuditLogItem(name="original")
        await item.save(db=db2)

        item.name = "updated"
        await item.save(db=db2)

        logs = await item.get_audit_log(db=db2)
        assert len(logs) == 2
        assert logs[0]["action"] == "update"
        assert logs[0]["changes"]["name"]["old"] == "original"
        assert logs[0]["changes"]["name"]["new"] == "updated"
        assert logs[1]["action"] == "create"

        await db1.close()
        await db2.close()


class TestAsyncLiteFullMixin:
    @pytest.mark.asyncio
    async def test_full_mixin_save(self):
        db1, db2 = await make_async_db_pair()
        await async_setup(ALiteFullItem, db1, db2)

        item = ALiteFullItem(name="full")
        await item.save(db=db2)

        assert item._id > 0
        assert_recent_utc(item.created_at, "created_at")
        assert_recent_utc(item.updated_at, "updated_at")
        assert item.deleted_at is None
        assert item.is_deleted is False

        await db1.close()
        await db2.close()
