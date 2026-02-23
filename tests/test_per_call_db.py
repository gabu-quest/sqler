"""Per-call DB binding tests for save(db=...) and delete(db=...).

These tests verify that instance methods can target a specific database
without mutating class-level state. Covers all 4 sync model variants:
SQLerModel, SQLerSafeModel, SQLerLiteModel, SQLerLiteSafeModel.
"""

from dataclasses import dataclass
from typing import Optional

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel
from sqler.exceptions import InvalidIdentifierError, InvalidTableNameError, NotBoundError, StaleVersionError
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.utils import RebaseConfig
from sqler.query import F


# ── Pydantic model fixtures ──────────────────────────────────────────────────


class Item(SQLerModel):
    __tablename__ = "items"
    name: str
    value: int = 0


class SafeCounter(SQLerSafeModel):
    __tablename__ = "safe_counters"
    name: str
    count: int = 0


class SafeRebaseCounter(SQLerSafeModel):
    __tablename__ = "safe_rebase_counters"
    name: str
    count: int = 0
    _rebase_config = RebaseConfig(
        enabled=True,
        allowed_fields={"count"},
        max_delta=100,
    )


# ── Lite model fixtures ──────────────────────────────────────────────────────


@dataclass
class LiteItem(SQLerLiteModel):
    __tablename__ = "lite_items"
    name: str
    value: int = 0


@dataclass
class LiteSafeCounter(SQLerLiteSafeModel):
    __tablename__ = "lite_safe_counters"
    name: str
    count: int = 0


@dataclass
class LiteSafeRebaseCounter(SQLerLiteSafeModel):
    __tablename__ = "lite_safe_rebase_counters"
    name: str
    count: int = 0
    _rebase_config = RebaseConfig(
        enabled=True,
        allowed_fields={"count"},
        max_delta=100,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_db_pair():
    """Create two independent in-memory databases."""
    db1 = SQLerDB.in_memory(shared=False)
    db2 = SQLerDB.in_memory(shared=False)
    return db1, db2


def setup_model(model_cls, db1, db2):
    """Bind model to db1 and ensure table exists in db2."""
    model_cls.set_db(db1)
    db2._ensure_table(model_cls._table)


def setup_safe_model(model_cls, db1, db2):
    """Bind safe model to db1 and ensure versioned table in db2."""
    model_cls.set_db(db1)
    db2._ensure_table(model_cls._table)
    db2._ensure_versioned_table(model_cls._table)


# ══════════════════════════════════════════════════════════════════════════════
# SQLerModel (Pydantic)
# ══════════════════════════════════════════════════════════════════════════════


class TestPydanticSaveDb:
    """save(db=...) on SQLerModel."""

    def test_save_insert_to_alternate_db(self):
        """save(db=db2) inserts into db2, not db1."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="alpha", value=10)
        item.save(db=db2)

        assert item._id is not None

        # Must exist in db2
        doc = db2.find_document("items", item._id)
        assert doc is not None
        assert doc["name"] == "alpha"
        assert doc["value"] == 10

        # Must NOT exist in db1
        doc1 = db1.find_document("items", item._id)
        assert doc1 is None

    def test_save_update_in_alternate_db(self):
        """save(db=db2) updates existing row in db2."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="beta", value=1)
        item.save(db=db2)
        original_id = item._id

        item.value = 99
        item.save(db=db2)

        assert item._id == original_id
        doc = db2.find_document("items", item._id)
        assert doc["value"] == 99

    def test_save_default_behavior_unchanged(self):
        """save() without db uses class-level binding as before."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="gamma", value=5)
        item.save()

        doc1 = db1.find_document("items", item._id)
        assert doc1 is not None
        assert doc1["name"] == "gamma"

        doc2 = db2.find_document("items", item._id)
        assert doc2 is None

    def test_save_alternating_databases(self):
        """Alternating save(db=db1) and save(db=db2) writes to correct targets."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        for i in range(5):
            item = Item(name=f"item_{i}", value=i)
            if i % 2 == 0:
                item.save(db=db1)
            else:
                item.save(db=db2)

        # Verify counts: 3 even items in db1, 2 odd items in db2
        all_db1 = Item.using(db1).all()
        all_db2 = Item.using(db2).all()
        assert len(all_db1) == 3
        assert len(all_db2) == 2

        names_db1 = sorted(i.name for i in all_db1)
        names_db2 = sorted(i.name for i in all_db2)
        assert names_db1 == ["item_0", "item_2", "item_4"]
        assert names_db2 == ["item_1", "item_3"]

    def test_save_does_not_mutate_class_binding(self):
        """save(db=db2) must not change Item._db."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        assert Item._db is db1

        item = Item(name="stealth", value=0)
        item.save(db=db2)

        # Class binding unchanged
        assert Item._db is db1

    def test_save_to_third_db(self):
        """save(db=db3) works even when class is bound to db1."""
        db1, db2 = make_db_pair()
        db3 = SQLerDB.in_memory(shared=False)
        setup_model(Item, db1, db2)
        db3._ensure_table("items")

        item = Item(name="third_party", value=42)
        item.save(db=db3)

        doc = db3.find_document("items", item._id)
        assert doc is not None
        assert doc["name"] == "third_party"

        # Verify no items in db1 (don't check by _id — IDs overlap across DBs)
        all_db1 = Item.using(db1).all()
        assert len(all_db1) == 0


class TestPydanticDeleteDb:
    """delete(db=...) on SQLerModel."""

    def test_delete_from_alternate_db(self):
        """delete(db=db2) removes from db2, not db1."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="doomed", value=666)
        item.save(db=db2)
        saved_id = item._id
        assert saved_id is not None

        item.delete(db=db2)

        assert item._id is None
        assert db2.find_document("items", saved_id) is None

    def test_delete_default_behavior_unchanged(self):
        """delete() without db uses class-level binding."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="default_delete", value=1)
        item.save()
        saved_id = item._id

        item.delete()

        assert item._id is None
        assert db1.find_document("items", saved_id) is None

    def test_delete_unsaved_raises(self):
        """delete(db=...) on unsaved instance raises ValueError."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="unsaved", value=0)
        with pytest.raises(ValueError, match="Cannot delete unsaved"):
            item.delete(db=db2)

    def test_delete_wrong_db_is_noop(self):
        """Deleting from wrong db silently removes nothing (item saved in db1, deleted from db2)."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="mismatch", value=1)
        item.save(db=db1)
        saved_id = item._id

        # Attempting to delete from db2 where the row doesn't exist
        # This should call db2.delete_document which won't find the row
        item.delete(db=db2)

        # Row should still exist in db1
        assert db1.find_document("items", saved_id) is not None

    def test_delete_with_policy_db_override(self):
        """delete_with_policy(db=db2) uses the specified db."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="policy_test", value=1)
        item.save(db=db2)
        saved_id = item._id

        item.delete_with_policy(db=db2, on_delete="restrict")

        assert item._id is None
        assert db2.find_document("items", saved_id) is None


# ══════════════════════════════════════════════════════════════════════════════
# SQLerSafeModel (Pydantic, versioned)
# ══════════════════════════════════════════════════════════════════════════════


class TestPydanticSafeSaveDb:
    """save(db=...) on SQLerSafeModel."""

    def test_safe_save_insert_to_alternate_db(self):
        """Safe save(db=db2) creates versioned row in db2."""
        db1, db2 = make_db_pair()
        setup_safe_model(SafeCounter, db1, db2)

        counter = SafeCounter(name="hits", count=0)
        counter.save(db=db2)

        assert counter._id is not None
        assert counter._version == 0

        doc = db2.find_document_with_version("safe_counters", counter._id)
        assert doc is not None
        assert doc["name"] == "hits"
        assert doc["count"] == 0
        assert doc["_version"] == 0

        # Not in db1
        assert db1.find_document_with_version("safe_counters", counter._id) is None

    def test_safe_save_update_increments_version_in_alternate_db(self):
        """Sequential saves to db2 increment version correctly."""
        db1, db2 = make_db_pair()
        setup_safe_model(SafeCounter, db1, db2)

        counter = SafeCounter(name="views", count=0)
        counter.save(db=db2)
        assert counter._version == 0

        counter.count = 10
        counter.save(db=db2)
        assert counter._version == 1

        counter.count = 20
        counter.save(db=db2)
        assert counter._version == 2

        doc = db2.find_document_with_version("safe_counters", counter._id)
        assert doc["count"] == 20
        assert doc["_version"] == 2

    def test_safe_save_default_behavior_unchanged(self):
        """save() without db uses class binding."""
        db1, db2 = make_db_pair()
        setup_safe_model(SafeCounter, db1, db2)

        counter = SafeCounter(name="default", count=0)
        counter.save()

        doc = db1.find_document_with_version("safe_counters", counter._id)
        assert doc is not None
        assert doc["name"] == "default"
        assert doc["_version"] == 0
        assert db2.find_document_with_version("safe_counters", counter._id) is None

    def test_safe_save_stale_version_raises_in_alternate_db(self):
        """Concurrent stale write to db2 raises StaleVersionError."""
        db1, db2 = make_db_pair()
        setup_safe_model(SafeCounter, db1, db2)

        c1 = SafeCounter(name="race", count=0)
        c1.save(db=db2)

        # Simulate concurrent writer: bump version in db2
        c2_doc = db2.find_document_with_version("safe_counters", c1._id)
        c2_doc["count"] = 50
        c2_doc["_version"] = 1
        db2.upsert_with_version("safe_counters", c1._id, c2_doc, 0)

        # c1 still has version 0, should conflict
        c1.count = 99
        with pytest.raises(StaleVersionError):
            c1.save(db=db2)

    def test_safe_save_rebase_in_alternate_db(self):
        """Intent rebasing works when saving to an alternate db."""
        db1, db2 = make_db_pair()
        setup_safe_model(SafeRebaseCounter, db1, db2)

        # Create initial record in db2
        c1 = SafeRebaseCounter(name="rebase_test", count=10)
        c1.save(db=db2)
        assert c1._version == 0

        # Load a copy and modify it to create a stale scenario
        c2_doc = db2.find_document_with_version("safe_rebase_counters", c1._id)
        c2_doc["count"] = 15
        c2_doc["_version"] = 1
        db2.upsert_with_version("safe_rebase_counters", c1._id, c2_doc, 0)

        # c1 is now stale (version 0 vs db has 1), increment by 3
        c1.count = 13  # original was 10, so delta = +3
        c1.save(db=db2)  # should rebase: 15 + 3 = 18

        doc = db2.find_document_with_version("safe_rebase_counters", c1._id)
        assert doc["count"] == 18
        assert doc["_version"] == 2

    def test_safe_version_isolation_between_dbs(self):
        """Versions in db1 and db2 are completely independent."""
        db1, db2 = make_db_pair()
        setup_safe_model(SafeCounter, db1, db2)

        c1 = SafeCounter(name="iso", count=0)
        c1.save(db=db1)
        for _ in range(5):
            c1.count += 1
            c1.save(db=db1)

        c2 = SafeCounter(name="iso", count=0)
        c2.save(db=db2)

        # db1 should be at version 5, db2 at version 0
        doc1 = db1.find_document_with_version("safe_counters", c1._id)
        doc2 = db2.find_document_with_version("safe_counters", c2._id)
        assert doc1["_version"] == 5
        assert doc1["count"] == 5
        assert doc2["_version"] == 0
        assert doc2["count"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# SQLerLiteModel (Dataclass)
# ══════════════════════════════════════════════════════════════════════════════


class TestLiteSaveDb:
    """save(db=...) on SQLerLiteModel."""

    def test_lite_save_insert_to_alternate_db(self):
        """Lite save(db=db2) inserts into db2."""
        db1, db2 = make_db_pair()
        setup_model(LiteItem, db1, db2)

        item = LiteItem(name="lite_alpha", value=10)
        item.save(db=db2)

        assert item._id is not None
        doc = db2.find_document("lite_items", item._id)
        assert doc is not None
        assert doc["name"] == "lite_alpha"
        assert doc["value"] == 10

        assert db1.find_document("lite_items", item._id) is None

    def test_lite_save_update_in_alternate_db(self):
        """Lite save(db=db2) updates in db2."""
        db1, db2 = make_db_pair()
        setup_model(LiteItem, db1, db2)

        item = LiteItem(name="lite_update", value=1)
        item.save(db=db2)
        original_id = item._id

        item.value = 42
        item.save(db=db2)

        assert item._id == original_id
        doc = db2.find_document("lite_items", item._id)
        assert doc["value"] == 42

    def test_lite_save_default_unchanged(self):
        """Lite save() without db uses class binding."""
        db1, db2 = make_db_pair()
        setup_model(LiteItem, db1, db2)

        item = LiteItem(name="lite_default", value=7)
        item.save()

        assert db1.find_document("lite_items", item._id) is not None
        assert db2.find_document("lite_items", item._id) is None


class TestLiteDeleteDb:
    """delete(db=...) on SQLerLiteModel."""

    def test_lite_delete_from_alternate_db(self):
        """Lite delete(db=db2) removes from db2."""
        db1, db2 = make_db_pair()
        setup_model(LiteItem, db1, db2)

        item = LiteItem(name="lite_doomed", value=0)
        item.save(db=db2)
        saved_id = item._id

        item.delete(db=db2)

        assert item._id is None
        assert db2.find_document("lite_items", saved_id) is None

    def test_lite_delete_unsaved_raises(self):
        """Lite delete(db=...) on unsaved raises ValueError."""
        db1, db2 = make_db_pair()
        setup_model(LiteItem, db1, db2)

        item = LiteItem(name="unsaved", value=0)
        with pytest.raises(ValueError, match="Cannot delete unsaved"):
            item.delete(db=db2)

    def test_lite_delete_with_policy_db_override(self):
        """Lite delete_with_policy(db=db2) uses the specified db."""
        db1, db2 = make_db_pair()
        setup_model(LiteItem, db1, db2)

        item = LiteItem(name="lite_policy", value=1)
        item.save(db=db2)
        saved_id = item._id

        item.delete_with_policy(db=db2, on_delete="restrict")

        assert item._id is None
        assert db2.find_document("lite_items", saved_id) is None

    def test_lite_delete_wrong_db_is_noop(self):
        """Lite deleting from wrong db doesn't affect original."""
        db1, db2 = make_db_pair()
        setup_model(LiteItem, db1, db2)

        item = LiteItem(name="lite_mismatch", value=1)
        item.save(db=db1)
        saved_id = item._id

        item.delete(db=db2)

        assert db1.find_document("lite_items", saved_id) is not None


# ══════════════════════════════════════════════════════════════════════════════
# SQLerLiteSafeModel (Dataclass, versioned)
# ══════════════════════════════════════════════════════════════════════════════


class TestLiteSafeSaveDb:
    """save(db=...) on SQLerLiteSafeModel."""

    def test_lite_safe_save_to_alternate_db(self):
        """Lite safe save(db=db2) creates versioned row in db2."""
        db1, db2 = make_db_pair()
        setup_safe_model(LiteSafeCounter, db1, db2)

        counter = LiteSafeCounter(name="lite_hits", count=0)
        counter.save(db=db2)

        assert counter._id is not None
        assert counter._version == 0

        doc = db2.find_document_with_version("lite_safe_counters", counter._id)
        assert doc is not None
        assert doc["name"] == "lite_hits"
        assert doc["_version"] == 0

        assert db1.find_document_with_version("lite_safe_counters", counter._id) is None

    def test_lite_safe_save_increments_version(self):
        """Lite safe sequential saves to db2 increment version."""
        db1, db2 = make_db_pair()
        setup_safe_model(LiteSafeCounter, db1, db2)

        counter = LiteSafeCounter(name="views", count=0)
        counter.save(db=db2)
        assert counter._version == 0

        counter.count = 10
        counter.save(db=db2)
        assert counter._version == 1

        doc = db2.find_document_with_version("lite_safe_counters", counter._id)
        assert doc["count"] == 10
        assert doc["_version"] == 1

    def test_lite_safe_stale_raises(self):
        """Lite safe stale version to db2 raises StaleVersionError."""
        db1, db2 = make_db_pair()
        setup_safe_model(LiteSafeCounter, db1, db2)

        c = LiteSafeCounter(name="race", count=0)
        c.save(db=db2)

        # Bump version behind the scenes
        c2_doc = db2.find_document_with_version("lite_safe_counters", c._id)
        c2_doc["count"] = 50
        c2_doc["_version"] = 1
        db2.upsert_with_version("lite_safe_counters", c._id, c2_doc, 0)

        c.count = 99
        with pytest.raises(StaleVersionError):
            c.save(db=db2)

    def test_lite_safe_rebase_in_alternate_db(self):
        """Lite safe intent rebasing works via db2."""
        db1, db2 = make_db_pair()
        setup_safe_model(LiteSafeRebaseCounter, db1, db2)

        c = LiteSafeRebaseCounter(name="rebase", count=10)
        c.save(db=db2)

        # Simulate concurrent update
        c2_doc = db2.find_document_with_version("lite_safe_rebase_counters", c._id)
        c2_doc["count"] = 20
        c2_doc["_version"] = 1
        db2.upsert_with_version("lite_safe_rebase_counters", c._id, c2_doc, 0)

        # c is stale, count was 10, we set to 15 (delta = +5)
        c.count = 15
        c.save(db=db2)  # rebase: 20 + 5 = 25

        doc = db2.find_document_with_version("lite_safe_rebase_counters", c._id)
        assert doc["count"] == 25
        assert doc["_version"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# Cross-cutting edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestCrossCuttingEdgeCases:
    """Edge cases that stress the per-call db binding across model types."""

    def test_fresh_instance_to_different_db(self):
        """A fresh (unsaved) instance can target any db independently."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        # Save to db1
        item1 = Item(name="db1_item", value=1)
        item1.save(db=db1)

        # Fresh instance to db2
        item2 = Item(name="db2_item", value=2)
        item2.save(db=db2)

        all_db1 = Item.using(db1).all()
        all_db2 = Item.using(db2).all()
        assert len(all_db1) == 1
        assert all_db1[0].name == "db1_item"
        assert len(all_db2) == 1
        assert all_db2[0].name == "db2_item"

    def test_bulk_save_to_alternate_db(self):
        """Batch of 100 saves to db2 all land correctly."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        ids = []
        for i in range(100):
            item = Item(name=f"bulk_{i}", value=i * 10)
            item.save(db=db2)
            ids.append(item._id)

        assert len(set(ids)) == 100  # all unique IDs

        for i, _id in enumerate(ids):
            doc = db2.find_document("items", _id)
            assert doc is not None
            assert doc["name"] == f"bulk_{i}"
            assert doc["value"] == i * 10
            assert db1.find_document("items", _id) is None

    def test_save_delete_cycle_across_dbs(self):
        """Save to db2, delete from db2, save again to db2."""
        db1, db2 = make_db_pair()
        setup_model(Item, db1, db2)

        item = Item(name="phoenix", value=1)
        item.save(db=db2)
        first_id = item._id

        item.delete(db=db2)
        assert item._id is None

        item.value = 2
        item.save(db=db2)
        assert item._id is not None
        assert item._id != first_id  # new row

        doc = db2.find_document("items", item._id)
        assert doc["name"] == "phoenix"
        assert doc["value"] == 2

    def test_class_binding_not_required_for_db_param(self):
        """When db param is passed, class need not be fully bound (only table needed)."""
        db1 = SQLerDB.in_memory(shared=False)

        class Ephemeral(SQLerModel):
            __tablename__ = "ephemerals"
            tag: str

        # Don't call set_db — class is unbound
        db1._ensure_table("ephemerals")

        obj = Ephemeral(tag="ghost")
        # save() with explicit db should work via _resolve_binding
        obj.save(db=db1)

        assert obj._id is not None
        doc = db1.find_document("ephemerals", obj._id)
        assert doc["tag"] == "ghost"

    def test_save_without_db_on_unbound_raises(self):
        """save() without db on unbound model raises NotBoundError."""

        class Unbound(SQLerModel):
            __tablename__ = "unbounds"
            x: int = 0

        obj = Unbound(x=1)
        with pytest.raises(NotBoundError):
            obj.save()

    def test_delete_without_db_on_unbound_raises(self):
        """delete() without db on unbound model raises NotBoundError."""

        class Unbound2(SQLerModel):
            __tablename__ = "unbounds2"
            x: int = 0

        obj = Unbound2(x=1)
        obj._id = 1
        with pytest.raises(NotBoundError):
            obj.delete()

    def test_multiple_models_same_db_param(self):
        """Different model classes can save(db=db2) to the same database."""
        db1, db2 = make_db_pair()

        class Alpha(SQLerModel):
            __tablename__ = "alphas"
            a: str

        class Beta(SQLerModel):
            __tablename__ = "betas"
            b: int

        Alpha.set_db(db1)
        Beta.set_db(db1)
        db2._ensure_table("alphas")
        db2._ensure_table("betas")

        a = Alpha(a="hello")
        a.save(db=db2)
        b = Beta(b=42)
        b.save(db=db2)

        assert db2.find_document("alphas", a._id) is not None
        assert db2.find_document("betas", b._id) is not None
        assert db1.find_document("alphas", a._id) is None
        assert db1.find_document("betas", b._id) is None

    def test_safe_model_interleaved_db_saves(self):
        """Safe model saves alternating between dbs maintain independent versions."""
        db1, db2 = make_db_pair()
        setup_safe_model(SafeCounter, db1, db2)

        # Instance for db1
        c1 = SafeCounter(name="db1_counter", count=0)
        c1.save(db=db1)

        # Instance for db2
        c2 = SafeCounter(name="db2_counter", count=0)
        c2.save(db=db2)

        # Increment c1 three times in db1
        for _ in range(3):
            c1.count += 1
            c1.save(db=db1)

        # Increment c2 once in db2
        c2.count += 1
        c2.save(db=db2)

        doc1 = db1.find_document_with_version("safe_counters", c1._id)
        doc2 = db2.find_document_with_version("safe_counters", c2._id)

        assert doc1["_version"] == 3
        assert doc1["count"] == 3
        assert doc2["_version"] == 1
        assert doc2["count"] == 1

    def test_lite_model_class_binding_not_required(self):
        """Lite model save(db=...) works without set_db when __tablename__ is set."""
        db = SQLerDB.in_memory(shared=False)
        db._ensure_table("lite_ephemerals")

        @dataclass
        class LiteEphemeral(SQLerLiteModel):
            __tablename__ = "lite_ephemerals"
            tag: str

        obj = LiteEphemeral(tag="lite_ghost")
        obj.save(db=db)

        assert obj._id is not None
        doc = db.find_document("lite_ephemerals", obj._id)
        assert doc["tag"] == "lite_ghost"

    def test_resolve_binding_table_fallback_to_class_name(self):
        """_resolve_binding with db falls back to class name when no __tablename__."""
        db = SQLerDB.in_memory(shared=False)

        class Widget(SQLerModel):
            color: str = "red"

        # No set_db, no __tablename__ — should derive "widgets" from class name
        db._ensure_table("widgets")

        w = Widget(color="blue")
        w.save(db=db)

        doc = db.find_document("widgets", w._id)
        assert doc is not None
        assert doc["color"] == "blue"

    def test_promoted_columns_save_to_alternate_db(self):
        """Models with __promoted__ can save(db=db2)."""
        db1, db2 = make_db_pair()

        class Promoted(SQLerModel):
            __tablename__ = "promoted_items"
            __promoted__ = {"status": "TEXT"}
            name: str
            status: str = "active"

        Promoted.set_db(db1)
        db2._ensure_table_with_promoted("promoted_items", {"status": "TEXT"})

        p = Promoted(name="promo", status="draft")
        p.save(db=db2)

        doc = db2.find_document("promoted_items", p._id)
        assert doc is not None
        assert doc["name"] == "promo"
        assert doc["status"] == "draft"

        assert db1.find_document("promoted_items", p._id) is None


# ── Negative / Hardening Tests ──────────────────────────────────────────────


class TestNegativeInputs:
    """Tests for invalid inputs that must fail clearly, not silently."""

    def test_save_with_wrong_db_type_raises(self):
        """save(db=42) should fail with AttributeError, not silently corrupt."""
        db = SQLerDB.in_memory(shared=False)
        Item.set_db(db)
        item = Item(name="test", value=1)
        with pytest.raises(AttributeError, match="object has no attribute"):
            item.save(db=42)

    def test_delete_with_wrong_db_type_raises(self):
        """delete(db='wrong') should fail with AttributeError."""
        db = SQLerDB.in_memory(shared=False)
        Item.set_db(db)
        item = Item(name="test", value=1).save()
        with pytest.raises(AttributeError, match="object has no attribute"):
            item.delete(db="wrong")

    def test_using_none_raises(self):
        """using(None) should raise AttributeError (no .adapter on None)."""
        db = SQLerDB.in_memory(shared=False)
        Item.set_db(db)
        with pytest.raises(AttributeError, match="object has no attribute"):
            Item.using(None).all()

    def test_using_wrong_type_raises(self):
        """using(42) should raise AttributeError (no .adapter on int)."""
        db = SQLerDB.in_memory(shared=False)
        Item.set_db(db)
        with pytest.raises(AttributeError, match="object has no attribute"):
            Item.using(42).all()

    def test_save_unbound_model_no_db_raises(self):
        """save() on unbound model without db= raises NotBoundError."""

        class Unbound(SQLerModel):
            __tablename__ = "unbounds"
            name: str

        with pytest.raises(NotBoundError):
            Unbound(name="test").save()

    def test_save_unbound_model_with_db_succeeds(self):
        """save(db=db) on unbound model works — no set_db needed."""
        db = SQLerDB.in_memory(shared=False)

        class Unbound(SQLerModel):
            __tablename__ = "unbounds"
            name: str

        u = Unbound(name="works")
        u.save(db=db)
        assert u._id is not None
        doc = db.find_document("unbounds", u._id)
        assert doc is not None
        assert doc["name"] == "works"


class TestResolveBindingInjection:
    """Tests that _resolve_binding validates table names against injection."""

    def test_malicious_tablename_via_save_db_raises(self):
        """Model with SQL-injection __tablename__ is rejected by _resolve_binding."""

        class Evil(SQLerModel):
            __tablename__ = "users; DROP TABLE users--"
            name: str

        db = SQLerDB.in_memory(shared=False)
        with pytest.raises((InvalidIdentifierError, InvalidTableNameError)):
            Evil(name="hacker").save(db=db)

    def test_tablename_with_semicolon_rejected(self):
        """Semicolons in table names are rejected."""

        class Evil2(SQLerModel):
            __tablename__ = "foo;bar"
            name: str

        db = SQLerDB.in_memory(shared=False)
        with pytest.raises((InvalidIdentifierError, InvalidTableNameError)):
            Evil2(name="test").save(db=db)

    def test_tablename_with_quotes_rejected(self):
        """Quotes in table names are rejected."""

        class Evil3(SQLerModel):
            __tablename__ = "foo'bar"
            name: str

        db = SQLerDB.in_memory(shared=False)
        with pytest.raises((InvalidIdentifierError, InvalidTableNameError)):
            Evil3(name="test").save(db=db)

    def test_empty_tablename_falls_through_to_default(self):
        """Empty __tablename__ is falsy so _resolve_binding derives from class name."""
        db = SQLerDB.in_memory(shared=False)

        class EmptyName(SQLerModel):
            __tablename__ = ""
            name: str

        e = EmptyName(name="test")
        e.save(db=db)
        # Falls through to default: "emptynames"
        doc = db.find_document("emptynames", e._id)
        assert doc is not None
        assert doc["name"] == "test"

    def test_valid_tablename_with_underscores_works(self):
        """Valid table names with underscores are accepted."""

        class Normal(SQLerModel):
            __tablename__ = "my_valid_table"
            name: str

        db = SQLerDB.in_memory(shared=False)
        n = Normal(name="ok").save(db=db)
        doc = db.find_document("my_valid_table", n._id)
        assert doc is not None
        assert doc["name"] == "ok"
