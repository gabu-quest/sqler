"""Async per-call DB binding tests for save(db=...) and delete(db=...).

Covers all 4 async model variants:
AsyncSQLerModel, AsyncSQLerSafeModel, AsyncSQLerLiteModel, AsyncSQLerLiteSafeModel.
"""

from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import NotBoundError, StaleVersionError
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.utils import RebaseConfig

# ── Pydantic model fixtures ──────────────────────────────────────────────────


class AItem(AsyncSQLerModel):
    __tablename__ = "aitems"
    name: str
    value: int = 0


class ASafeCounter(AsyncSQLerSafeModel):
    __tablename__ = "asafe_counters"
    name: str
    count: int = 0


class ASafeRebaseCounter(AsyncSQLerSafeModel):
    __tablename__ = "asafe_rebase_counters"
    name: str
    count: int = 0
    _rebase_config = RebaseConfig(
        enabled=True,
        allowed_fields={"count"},
        max_delta=100,
    )


# ── Lite model fixtures ──────────────────────────────────────────────────────


@dataclass
class ALiteItem(AsyncSQLerLiteModel):
    __tablename__ = "alite_items"
    name: str
    value: int = 0


@dataclass
class ALiteSafeCounter(AsyncSQLerLiteSafeModel):
    __tablename__ = "alite_safe_counters"
    name: str
    count: int = 0


@dataclass
class ALiteSafeRebaseCounter(AsyncSQLerLiteSafeModel):
    __tablename__ = "alite_safe_rebase_counters"
    name: str
    count: int = 0
    _rebase_config = RebaseConfig(
        enabled=True,
        allowed_fields={"count"},
        max_delta=100,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_pair():
    """Create two independent async in-memory databases with cleanup."""
    db1 = AsyncSQLerDB.in_memory(shared=True, name="pcdb1")
    db2 = AsyncSQLerDB.in_memory(shared=True, name="pcdb2")
    await db1.connect()
    await db2.connect()
    try:
        yield db1, db2
    finally:
        await db1.close()
        await db2.close()


async def setup_async_model(model_cls, db1, db2):
    """Bind model to db1 and ensure table in db2."""
    model_cls.set_db(db1)
    if hasattr(model_cls, "_ensure_schema"):
        await model_cls._ensure_schema()
    else:
        await db1._ensure_table(model_cls._table)
    await db2._ensure_table(model_cls._table)


async def setup_async_safe_model(model_cls, db1, db2):
    """Bind safe model to db1 and ensure versioned table in db2."""
    model_cls.set_db(db1)
    await db1._ensure_table(model_cls._table)
    await db1._ensure_versioned_table(model_cls._table)
    await db2._ensure_table(model_cls._table)
    await db2._ensure_versioned_table(model_cls._table)


# ══════════════════════════════════════════════════════════════════════════════
# AsyncSQLerModel (Pydantic)
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncPydanticSaveDb:
    """save(db=...) on AsyncSQLerModel."""

    @pytest.mark.asyncio
    async def test_save_insert_to_alternate_db(self, db_pair):
        """Async save(db=db2) inserts into db2, not db1."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="async_alpha", value=10)
        await item.save(db=db2)

        assert item._id is not None

        doc = await db2.find_document("aitems", item._id)
        assert doc is not None
        assert doc["name"] == "async_alpha"
        assert doc["value"] == 10

        doc1 = await db1.find_document("aitems", item._id)
        assert doc1 is None

    @pytest.mark.asyncio
    async def test_save_update_in_alternate_db(self, db_pair):
        """Async save(db=db2) updates existing row in db2."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="async_beta", value=1)
        await item.save(db=db2)
        original_id = item._id

        item.value = 77
        await item.save(db=db2)

        assert item._id == original_id
        doc = await db2.find_document("aitems", item._id)
        assert doc["value"] == 77

    @pytest.mark.asyncio
    async def test_save_default_behavior_unchanged(self, db_pair):
        """Async save() without db uses class binding."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="async_default", value=5)
        await item.save()

        doc1 = await db1.find_document("aitems", item._id)
        assert doc1 is not None
        assert doc1["name"] == "async_default"

        doc2 = await db2.find_document("aitems", item._id)
        assert doc2 is None

    @pytest.mark.asyncio
    async def test_save_does_not_mutate_class_binding(self, db_pair):
        """Async save(db=db2) must not change class._db."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        assert AItem._db is db1

        item = AItem(name="stealth", value=0)
        await item.save(db=db2)

        assert AItem._db is db1

    @pytest.mark.asyncio
    async def test_save_alternating_databases(self, db_pair):
        """Async alternating saves target correct databases."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        for i in range(6):
            item = AItem(name=f"async_item_{i}", value=i)
            if i % 2 == 0:
                await item.save(db=db1)
            else:
                await item.save(db=db2)

        # Verify counts: 3 even items in db1, 3 odd items in db2
        all_db1 = await AItem.using(db1).all()
        all_db2 = await AItem.using(db2).all()
        assert len(all_db1) == 3
        assert len(all_db2) == 3

        names_db1 = sorted(i.name for i in all_db1)
        names_db2 = sorted(i.name for i in all_db2)
        assert names_db1 == ["async_item_0", "async_item_2", "async_item_4"]
        assert names_db2 == ["async_item_1", "async_item_3", "async_item_5"]


class TestAsyncPydanticDeleteDb:
    """delete(db=...) on AsyncSQLerModel."""

    @pytest.mark.asyncio
    async def test_delete_from_alternate_db(self, db_pair):
        """Async delete(db=db2) removes from db2."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="async_doomed", value=666)
        await item.save(db=db2)
        saved_id = item._id

        await item.delete(db=db2)

        assert item._id is None
        assert (await db2.find_document("aitems", saved_id)) is None

    @pytest.mark.asyncio
    async def test_delete_default_uses_class_binding(self, db_pair):
        """Async delete() without db uses class binding."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="async_del_default", value=1)
        await item.save()
        saved_id = item._id

        await item.delete()

        assert item._id is None
        assert (await db1.find_document("aitems", saved_id)) is None

    @pytest.mark.asyncio
    async def test_delete_unsaved_raises(self, db_pair):
        """Async delete(db=...) on unsaved raises ValueError."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="unsaved", value=0)
        with pytest.raises(ValueError, match="Cannot delete unsaved"):
            await item.delete(db=db2)

    @pytest.mark.asyncio
    async def test_delete_with_policy_db_override(self, db_pair):
        """Async delete_with_policy(db=db2) works."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="policy", value=1)
        await item.save(db=db2)
        saved_id = item._id

        await item.delete_with_policy(db=db2, on_delete="restrict")

        assert item._id is None
        assert (await db2.find_document("aitems", saved_id)) is None


# ══════════════════════════════════════════════════════════════════════════════
# AsyncSQLerSafeModel (Pydantic, versioned)
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncSafeSaveDb:
    """save(db=...) on AsyncSQLerSafeModel."""

    @pytest.mark.asyncio
    async def test_safe_save_insert_to_alternate_db(self, db_pair):
        """Async safe save(db=db2) creates versioned row in db2."""
        db1, db2 = db_pair
        await setup_async_safe_model(ASafeCounter, db1, db2)

        counter = ASafeCounter(name="ahits", count=0)
        await counter.save(db=db2)

        assert counter._id is not None
        assert counter._version == 0

        doc = await db2.find_document_with_version("asafe_counters", counter._id)
        assert doc is not None
        assert doc["name"] == "ahits"
        assert doc["_version"] == 0

        assert (await db1.find_document_with_version("asafe_counters", counter._id)) is None

    @pytest.mark.asyncio
    async def test_safe_save_increments_version(self, db_pair):
        """Async safe sequential saves to db2 increment version."""
        db1, db2 = db_pair
        await setup_async_safe_model(ASafeCounter, db1, db2)

        counter = ASafeCounter(name="aviews", count=0)
        await counter.save(db=db2)
        assert counter._version == 0

        counter.count = 10
        await counter.save(db=db2)
        assert counter._version == 1

        counter.count = 20
        await counter.save(db=db2)
        assert counter._version == 2

        doc = await db2.find_document_with_version("asafe_counters", counter._id)
        assert doc["count"] == 20
        assert doc["_version"] == 2

    @pytest.mark.asyncio
    async def test_safe_stale_raises_in_alternate_db(self, db_pair):
        """Async safe stale version in db2 raises StaleVersionError."""
        db1, db2 = db_pair
        await setup_async_safe_model(ASafeCounter, db1, db2)

        c = ASafeCounter(name="arace", count=0)
        await c.save(db=db2)

        # Bump version behind the scenes in db2
        c2_doc = await db2.find_document_with_version("asafe_counters", c._id)
        c2_doc["count"] = 50
        c2_doc["_version"] = 1
        await db2.upsert_with_version("asafe_counters", c._id, c2_doc, 0)

        c.count = 99
        with pytest.raises(StaleVersionError):
            await c.save(db=db2)

    @pytest.mark.asyncio
    async def test_safe_rebase_in_alternate_db(self, db_pair):
        """Async safe intent rebasing works via db2."""
        db1, db2 = db_pair
        await setup_async_safe_model(ASafeRebaseCounter, db1, db2)

        c = ASafeRebaseCounter(name="arebase", count=10)
        await c.save(db=db2)
        assert c._version == 0

        # Simulate concurrent update in db2
        c2_doc = await db2.find_document_with_version("asafe_rebase_counters", c._id)
        c2_doc["count"] = 15
        c2_doc["_version"] = 1
        await db2.upsert_with_version("asafe_rebase_counters", c._id, c2_doc, 0)

        # c is stale (v0), intent: count 10 -> 13, delta = +3
        c.count = 13
        await c.save(db=db2)  # rebase: 15 + 3 = 18

        doc = await db2.find_document_with_version("asafe_rebase_counters", c._id)
        assert doc["count"] == 18
        assert doc["_version"] == 2

    @pytest.mark.asyncio
    async def test_safe_version_isolation(self, db_pair):
        """Async safe versions in db1 and db2 are independent."""
        db1, db2 = db_pair
        await setup_async_safe_model(ASafeCounter, db1, db2)

        c1 = ASafeCounter(name="aiso", count=0)
        await c1.save(db=db1)
        for _ in range(5):
            c1.count += 1
            await c1.save(db=db1)

        c2 = ASafeCounter(name="aiso", count=0)
        await c2.save(db=db2)

        doc1 = await db1.find_document_with_version("asafe_counters", c1._id)
        doc2 = await db2.find_document_with_version("asafe_counters", c2._id)
        assert doc1["_version"] == 5
        assert doc1["count"] == 5
        assert doc2["_version"] == 0
        assert doc2["count"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# AsyncSQLerLiteModel (Dataclass)
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncLiteSaveDb:
    """save(db=...) on AsyncSQLerLiteModel."""

    @pytest.mark.asyncio
    async def test_lite_save_insert_to_alternate_db(self, db_pair):
        """Async lite save(db=db2) inserts into db2."""
        db1, db2 = db_pair
        await setup_async_model(ALiteItem, db1, db2)

        item = ALiteItem(name="alite_alpha", value=10)
        await item.save(db=db2)

        assert item._id is not None
        doc = await db2.find_document("alite_items", item._id)
        assert doc is not None
        assert doc["name"] == "alite_alpha"
        assert doc["value"] == 10

        assert (await db1.find_document("alite_items", item._id)) is None

    @pytest.mark.asyncio
    async def test_lite_save_update_in_alternate_db(self, db_pair):
        """Async lite save(db=db2) updates in db2."""
        db1, db2 = db_pair
        await setup_async_model(ALiteItem, db1, db2)

        item = ALiteItem(name="alite_update", value=1)
        await item.save(db=db2)
        original_id = item._id

        item.value = 42
        await item.save(db=db2)

        assert item._id == original_id
        doc = await db2.find_document("alite_items", item._id)
        assert doc["value"] == 42

    @pytest.mark.asyncio
    async def test_lite_save_default_unchanged(self, db_pair):
        """Async lite save() without db uses class binding."""
        db1, db2 = db_pair
        await setup_async_model(ALiteItem, db1, db2)

        item = ALiteItem(name="alite_default", value=7)
        await item.save()

        assert (await db1.find_document("alite_items", item._id)) is not None
        assert (await db2.find_document("alite_items", item._id)) is None


class TestAsyncLiteDeleteDb:
    """delete(db=...) on AsyncSQLerLiteModel."""

    @pytest.mark.asyncio
    async def test_lite_delete_from_alternate_db(self, db_pair):
        """Async lite delete(db=db2) removes from db2."""
        db1, db2 = db_pair
        await setup_async_model(ALiteItem, db1, db2)

        item = ALiteItem(name="alite_doomed", value=0)
        await item.save(db=db2)
        saved_id = item._id

        await item.delete(db=db2)

        assert item._id is None
        assert (await db2.find_document("alite_items", saved_id)) is None

    @pytest.mark.asyncio
    async def test_lite_delete_unsaved_raises(self, db_pair):
        """Async lite delete(db=...) on unsaved raises ValueError."""
        db1, db2 = db_pair
        await setup_async_model(ALiteItem, db1, db2)

        item = ALiteItem(name="unsaved", value=0)
        with pytest.raises(ValueError, match="Cannot delete unsaved"):
            await item.delete(db=db2)

    @pytest.mark.asyncio
    async def test_lite_delete_with_policy_db_override(self, db_pair):
        """Async lite delete_with_policy(db=db2) uses the specified db."""
        db1, db2 = db_pair
        await setup_async_model(ALiteItem, db1, db2)

        item = ALiteItem(name="lite_policy", value=1)
        await item.save(db=db2)
        saved_id = item._id

        await item.delete_with_policy(db=db2, on_delete="restrict")

        assert item._id is None
        assert (await db2.find_document("alite_items", saved_id)) is None

    @pytest.mark.asyncio
    async def test_lite_delete_wrong_db_is_noop(self, db_pair):
        """Async lite deleting from wrong db doesn't affect original."""
        db1, db2 = db_pair
        await setup_async_model(ALiteItem, db1, db2)

        item = ALiteItem(name="lite_mismatch", value=1)
        await item.save(db=db1)
        saved_id = item._id

        await item.delete(db=db2)

        assert (await db1.find_document("alite_items", saved_id)) is not None


# ══════════════════════════════════════════════════════════════════════════════
# AsyncSQLerLiteSafeModel (Dataclass, versioned)
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncLiteSafeSaveDb:
    """save(db=...) on AsyncSQLerLiteSafeModel."""

    @pytest.mark.asyncio
    async def test_lite_safe_save_to_alternate_db(self, db_pair):
        """Async lite safe save(db=db2) creates versioned row in db2."""
        db1, db2 = db_pair
        await setup_async_safe_model(ALiteSafeCounter, db1, db2)

        counter = ALiteSafeCounter(name="alite_hits", count=0)
        await counter.save(db=db2)

        assert counter._id is not None
        assert counter._version == 0

        doc = await db2.find_document_with_version("alite_safe_counters", counter._id)
        assert doc is not None
        assert doc["name"] == "alite_hits"
        assert doc["_version"] == 0

        assert (await db1.find_document_with_version("alite_safe_counters", counter._id)) is None

    @pytest.mark.asyncio
    async def test_lite_safe_increments_version(self, db_pair):
        """Async lite safe sequential saves to db2 increment version."""
        db1, db2 = db_pair
        await setup_async_safe_model(ALiteSafeCounter, db1, db2)

        counter = ALiteSafeCounter(name="aviews", count=0)
        await counter.save(db=db2)
        assert counter._version == 0

        counter.count = 10
        await counter.save(db=db2)
        assert counter._version == 1

        doc = await db2.find_document_with_version("alite_safe_counters", counter._id)
        assert doc["count"] == 10
        assert doc["_version"] == 1

    @pytest.mark.asyncio
    async def test_lite_safe_stale_raises(self, db_pair):
        """Async lite safe stale version raises StaleVersionError."""
        db1, db2 = db_pair
        await setup_async_safe_model(ALiteSafeCounter, db1, db2)

        c = ALiteSafeCounter(name="arace", count=0)
        await c.save(db=db2)

        c2_doc = await db2.find_document_with_version("alite_safe_counters", c._id)
        c2_doc["count"] = 50
        c2_doc["_version"] = 1
        await db2.upsert_with_version("alite_safe_counters", c._id, c2_doc, 0)

        c.count = 99
        with pytest.raises(StaleVersionError):
            await c.save(db=db2)

    @pytest.mark.asyncio
    async def test_lite_safe_rebase_in_alternate_db(self, db_pair):
        """Async lite safe intent rebasing works via db2."""
        db1, db2 = db_pair
        await setup_async_safe_model(ALiteSafeRebaseCounter, db1, db2)

        c = ALiteSafeRebaseCounter(name="arebase", count=10)
        await c.save(db=db2)

        c2_doc = await db2.find_document_with_version("alite_safe_rebase_counters", c._id)
        c2_doc["count"] = 20
        c2_doc["_version"] = 1
        await db2.upsert_with_version("alite_safe_rebase_counters", c._id, c2_doc, 0)

        # delta = +5 (from 10 to 15)
        c.count = 15
        await c.save(db=db2)  # rebase: 20 + 5 = 25

        doc = await db2.find_document_with_version("alite_safe_rebase_counters", c._id)
        assert doc["count"] == 25
        assert doc["_version"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# Cross-cutting async edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestAsyncCrossCuttingEdgeCases:
    """Async edge cases that stress per-call db binding."""

    @pytest.mark.asyncio
    async def test_bulk_save_to_alternate_db(self, db_pair):
        """Batch of 100 async saves to db2 all land correctly."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        ids = []
        for i in range(100):
            item = AItem(name=f"async_bulk_{i}", value=i * 10)
            await item.save(db=db2)
            ids.append(item._id)

        assert len(set(ids)) == 100

        for i, _id in enumerate(ids):
            doc = await db2.find_document("aitems", _id)
            assert doc is not None
            assert doc["name"] == f"async_bulk_{i}"
            assert doc["value"] == i * 10
            assert (await db1.find_document("aitems", _id)) is None

    @pytest.mark.asyncio
    async def test_save_delete_cycle_across_dbs(self, db_pair):
        """Async save to db2, delete from db2, save again to db2."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="aphoenix", value=1)
        await item.save(db=db2)
        first_id = item._id

        await item.delete(db=db2)
        assert item._id is None

        item.value = 2
        await item.save(db=db2)
        assert item._id is not None
        assert item._id != first_id

        doc = await db2.find_document("aitems", item._id)
        assert doc["name"] == "aphoenix"
        assert doc["value"] == 2

    @pytest.mark.asyncio
    async def test_class_binding_not_required_for_db_param(self):
        """Async save(db=...) works on unbound class with __tablename__."""
        db = AsyncSQLerDB.in_memory(shared=True, name="pcdb_unbound")
        await db.connect()
        try:
            await db._ensure_table("async_ephemerals")

            class AsyncEphemeral(AsyncSQLerModel):
                __tablename__ = "async_ephemerals"
                tag: str

            obj = AsyncEphemeral(tag="async_ghost")
            await obj.save(db=db)

            assert obj._id is not None
            doc = await db.find_document("async_ephemerals", obj._id)
            assert doc["tag"] == "async_ghost"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_save_without_db_on_unbound_raises(self):
        """Async save() without db on unbound raises NotBoundError."""

        class AUnbound(AsyncSQLerModel):
            __tablename__ = "aunbounds"
            x: int = 0

        obj = AUnbound(x=1)
        with pytest.raises(NotBoundError):
            await obj.save()

    @pytest.mark.asyncio
    async def test_delete_without_db_on_unbound_raises(self):
        """Async delete() without db on unbound raises NotBoundError."""

        class AUnbound2(AsyncSQLerModel):
            __tablename__ = "aunbounds2"
            x: int = 0

        obj = AUnbound2(x=1)
        obj._id = 1
        with pytest.raises(NotBoundError):
            await obj.delete()

    @pytest.mark.asyncio
    async def test_safe_interleaved_db_saves(self, db_pair):
        """Async safe model saves alternating dbs have independent versions."""
        db1, db2 = db_pair
        await setup_async_safe_model(ASafeCounter, db1, db2)

        c1 = ASafeCounter(name="adb1_counter", count=0)
        await c1.save(db=db1)

        c2 = ASafeCounter(name="adb2_counter", count=0)
        await c2.save(db=db2)

        for _ in range(3):
            c1.count += 1
            await c1.save(db=db1)

        c2.count += 1
        await c2.save(db=db2)

        doc1 = await db1.find_document_with_version("asafe_counters", c1._id)
        doc2 = await db2.find_document_with_version("asafe_counters", c2._id)

        assert doc1["_version"] == 3
        assert doc1["count"] == 3
        assert doc2["_version"] == 1
        assert doc2["count"] == 1

    @pytest.mark.asyncio
    async def test_lite_class_binding_not_required(self):
        """Async lite save(db=...) works without set_db when __tablename__ is set."""
        db = AsyncSQLerDB.in_memory(shared=True, name="pcdb_lite_unbound")
        await db.connect()
        try:
            await db._ensure_table("alite_ephemerals")

            @dataclass
            class ALiteEphemeral(AsyncSQLerLiteModel):
                __tablename__ = "alite_ephemerals"
                tag: str

            obj = ALiteEphemeral(tag="alite_ghost")
            await obj.save(db=db)

            assert obj._id is not None
            doc = await db.find_document("alite_ephemerals", obj._id)
            assert doc["tag"] == "alite_ghost"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_save_to_third_db(self, db_pair):
        """Async save(db=db3) works when class is bound to db1."""
        db1, db2 = db_pair
        db3 = AsyncSQLerDB.in_memory(shared=True, name="pcdb3")
        await db3.connect()
        try:
            await setup_async_model(AItem, db1, db2)
            await db3._ensure_table("aitems")

            item = AItem(name="third_party", value=42)
            await item.save(db=db3)

            doc = await db3.find_document("aitems", item._id)
            assert doc is not None
            assert doc["name"] == "third_party"

            # Verify db1 has no items (don't check by _id — IDs can overlap)
            all_db1 = await AItem.using(db1).all()
            assert len(all_db1) == 0
        finally:
            await db3.close()

    @pytest.mark.asyncio
    async def test_delete_wrong_db_is_noop(self, db_pair):
        """Async deleting from wrong db doesn't affect original."""
        db1, db2 = db_pair
        await setup_async_model(AItem, db1, db2)

        item = AItem(name="mismatch", value=1)
        await item.save(db=db1)
        saved_id = item._id

        await item.delete(db=db2)

        # Row should still exist in db1
        assert (await db1.find_document("aitems", saved_id)) is not None

    @pytest.mark.asyncio
    async def test_promoted_columns_save_to_alternate_db(self, db_pair):
        """Async models with __promoted__ can save(db=db2)."""
        db1, db2 = db_pair

        class APromoted(AsyncSQLerModel):
            __tablename__ = "apromoted_items"
            __promoted__ = {"status": "TEXT"}
            name: str
            status: str = "active"

        APromoted.set_db(db1)
        await APromoted._ensure_schema()
        await db2._ensure_table_with_promoted("apromoted_items", {"status": "TEXT"})

        p = APromoted(name="apromo", status="draft")
        await p.save(db=db2)

        doc = await db2.find_document("apromoted_items", p._id)
        assert doc is not None
        assert doc["name"] == "apromo"
        assert doc["status"] == "draft"

        assert (await db1.find_document("apromoted_items", p._id)) is None

    @pytest.mark.asyncio
    async def test_resolve_binding_table_fallback_to_class_name(self, db_pair):
        """Async _resolve_binding with db falls back to class name when no __tablename__."""
        db1, _ = db_pair

        class AWidget(AsyncSQLerModel):
            color: str = "red"

        await db1._ensure_table("awidgets")

        w = AWidget(color="blue")
        await w.save(db=db1)

        doc = await db1.find_document("awidgets", w._id)
        assert doc is not None
        assert doc["color"] == "blue"
