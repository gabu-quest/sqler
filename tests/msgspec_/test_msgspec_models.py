"""Tests for SQLer msgspec models (Struct-based, C-level speed).

These tests verify that msgspec models provide API compatibility with
the dataclass-based lite models while using msgspec Structs.
"""

import warnings
from datetime import date, datetime
from typing import Optional

import pytest
from sqler import (
    MSGSPEC_AVAILABLE,
    SQLerDB,
    SQLerMsgspecModel,
    SQLerMsgspecModelBase,
    is_msgspec_model,
)
from sqler.query import F

pytestmark = pytest.mark.skipif(not MSGSPEC_AVAILABLE, reason="msgspec not installed")


class TestCompatibilityHelpers:
    """Test msgspec compatibility detection helpers."""

    def test_msgspec_available(self):
        """msgspec should be available in test environment."""
        assert MSGSPEC_AVAILABLE is True

    def test_is_msgspec_model(self):
        """is_msgspec_model should correctly identify msgspec models."""

        class MsgUser(SQLerMsgspecModel):
            name: str

        assert is_msgspec_model(MsgUser) is True
        assert is_msgspec_model(SQLerMsgspecModel) is True
        assert is_msgspec_model(str) is False

    def test_is_msgspec_model_not_lite(self):
        """is_msgspec_model should not match lite models."""
        from sqler import SQLerLiteModel, is_lite_model

        assert is_lite_model(SQLerMsgspecModel) is False
        assert is_msgspec_model(SQLerLiteModel) is False


class TestSQLerMsgspecModelBase:
    """Test the base class functionality."""

    def test_model_validate(self):
        """model_validate should create instance from dict."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        user = User.model_validate({"name": "Alice", "age": 30})
        assert user.name == "Alice"
        assert user.age == 30

    def test_model_validate_with_id(self):
        """model_validate should handle _id field."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User.model_validate({"name": "Bob", "_id": 42})
        assert user.name == "Bob"
        assert user._id == 42

    def test_model_validate_ignores_extra(self):
        """model_validate should ignore unknown fields."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User.model_validate({"name": "Carol", "unknown_field": "value"})
        assert user.name == "Carol"
        assert not hasattr(user, "unknown_field")

    def test_model_dump(self):
        """model_dump should serialize to dict."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        user = User(name="Dave", age=25)
        data = user.model_dump()
        assert data == {"name": "Dave", "age": 25}

    def test_model_dump_excludes_private(self):
        """model_dump should exclude fields starting with underscore."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User(name="Eve")
        user._id = 100
        data = user.model_dump()
        assert "_id" not in data
        assert "_snapshot" not in data
        assert data == {"name": "Eve"}

    def test_model_dump_exclude_none(self):
        """model_dump with exclude_none should skip None values."""

        class User(SQLerMsgspecModelBase):
            name: str
            email: Optional[str] = None

        user = User(name="Frank", email=None)
        data = user.model_dump(exclude_none=True)
        assert "email" not in data
        assert data == {"name": "Frank"}

    def test_model_dump_exclude_set(self):
        """model_dump with exclude should skip specified fields."""

        class User(SQLerMsgspecModelBase):
            name: str
            secret: str

        user = User(name="Grace", secret="hidden")
        data = user.model_dump(exclude={"secret"})
        assert "secret" not in data
        assert data == {"name": "Grace"}

    def test_model_fields(self):
        """model_fields should return field metadata."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        mf = User.model_fields
        assert "name" in mf
        assert "age" in mf
        assert "_id" not in mf
        assert "_snapshot" not in mf

    def test_get_field_names(self):
        """get_field_names should return public field names."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        names = User.get_field_names()
        assert names == {"name", "age"}

    def test_datetime_serialization(self):
        """model_dump should serialize datetime to ISO format."""

        class Event(SQLerMsgspecModelBase):
            name: str
            when: datetime

        now = datetime(2024, 1, 15, 10, 30, 0)
        event = Event(name="Meeting", when=now)
        data = event.model_dump()
        assert data["when"] == "2024-01-15T10:30:00"

    def test_repr(self):
        """__repr__ should show class name and public fields."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        user = User(name="Alice", age=30)
        assert repr(user) == "User(name='Alice', age=30)"

    def test_repr_with_id(self):
        """__repr__ should show _id when set."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User(name="Alice")
        user._id = 5
        assert repr(user) == "User(_id=5, name='Alice')"

    def test_default_values(self):
        """Struct fields with defaults should work."""

        class Item(SQLerMsgspecModelBase):
            name: str
            count: int = 0
            tags: list[str] = []

        item = Item(name="test")
        assert item.name == "test"
        assert item.count == 0
        assert item.tags == []

    def test_list_field_roundtrip(self):
        """Lists should serialize and deserialize correctly."""

        class Item(SQLerMsgspecModelBase):
            tags: list[str] = []

        item = Item.model_validate({"tags": ["a", "b", "c"]})
        assert item.tags == ["a", "b", "c"]
        data = item.model_dump()
        assert data["tags"] == ["a", "b", "c"]


class TestSQLerMsgspecModel:
    """Test SQLerMsgspecModel persistence."""

    @pytest.fixture
    def db(self):
        """Create in-memory database."""
        return SQLerDB.in_memory()

    def test_basic_crud(self, db):
        """Test create, read, update, delete operations."""

        class User(SQLerMsgspecModel):
            __tablename__ = "msg_users"
            name: str
            email: str

        User.set_db(db)

        # Create
        user = User(name="Alice", email="alice@example.com")
        assert user._id is None
        user.save()
        assert user._id is not None

        # Read
        loaded = User.from_id(user._id)
        assert loaded is not None
        assert loaded.name == "Alice"
        assert loaded.email == "alice@example.com"

        # Update
        user.name = "Alice Updated"
        user.save()
        reloaded = User.from_id(user._id)
        assert reloaded.name == "Alice Updated"

        # Delete
        user.delete()
        assert user._id is None
        assert User.from_id(1) is None

    def test_query_all(self, db):
        """Test querying all records."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_items"
            name: str

        Item.set_db(db)

        Item(name="One").save()
        Item(name="Two").save()
        Item(name="Three").save()

        items = Item.all()
        assert len(items) == 3
        names = {i.name for i in items}
        assert names == {"One", "Two", "Three"}

    def test_query_count(self, db):
        """Test counting records."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_items2"
            name: str

        Item.set_db(db)

        assert Item.count() == 0
        Item(name="A").save()
        Item(name="B").save()
        assert Item.count() == 2

    def test_query_filter(self, db):
        """Test filtering with expressions."""

        class Product(SQLerMsgspecModel):
            __tablename__ = "msg_products"
            name: str
            price: float

        Product.set_db(db)

        Product(name="Apple", price=1.50).save()
        Product(name="Banana", price=0.75).save()
        Product(name="Cherry", price=3.00).save()

        expensive = Product.filter(F("price") > 1.00).all()
        assert len(expensive) == 2
        names = {p.name for p in expensive}
        assert names == {"Apple", "Cherry"}

    def test_from_ids_batch(self, db):
        """Test batch loading by IDs."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_batch_items"
            name: str

        Item.set_db(db)

        ids = []
        for name in ["A", "B", "C", "D"]:
            item = Item(name=name)
            item.save()
            ids.append(item._id)

        items = Item.from_ids([ids[0], ids[2]])
        assert len(items) == 2
        names = {i.name for i in items}
        assert names == {"A", "C"}

    def test_refresh(self, db):
        """Test refreshing instance from database."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_refresh_items"
            value: int

        Item.set_db(db)

        item = Item(value=100)
        item.save()

        # Modify directly in DB
        db.adapter.execute(
            "UPDATE msg_refresh_items SET data = ? WHERE _id = ?",
            ['{"value": 999}', item._id],
        )
        db.adapter.auto_commit()

        item.refresh()
        assert item.value == 999

    def test_dirty_tracking(self, db):
        """Test dirty field tracking."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_dirty_items"
            name: str
            value: int

        Item.set_db(db)

        item = Item(name="Test", value=1)
        assert item.is_dirty()  # New instance is dirty

        item.save()
        assert not item.is_dirty()  # After save, not dirty

        item.value = 2
        assert item.is_dirty()
        assert item.get_dirty_fields() == {"value"}

    def test_default_table_name(self, db):
        """Test automatic table name generation."""

        class MsgCategory(SQLerMsgspecModel):
            name: str

        MsgCategory.set_db(db)
        assert MsgCategory._table == "msgcategories"

    def test_using_api(self, db):
        """Test the using() API for ad-hoc database binding."""

        class Task(SQLerMsgspecModel):
            __tablename__ = "msg_tasks"
            title: str

        db._ensure_table("msg_tasks")
        Task(title="First").save(db=db)
        Task(title="Second").save(db=db)

        results = Task.using(db).all()
        assert len(results) == 2
        titles = {t.title for t in results}
        assert titles == {"First", "Second"}


class TestMsgspecQuerysetCompat:
    """Verify that queryset's _attach_metadata works with msgspec Structs."""

    @pytest.fixture
    def db(self):
        return SQLerDB.in_memory()

    def test_attach_metadata_on_all(self, db):
        """_attach_metadata should set correct _id and _snapshot on Struct instances."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_meta_items"
            name: str
            value: int = 0

        Item.set_db(db)

        a = Item(name="A", value=10)
        a.save()
        b = Item(name="B", value=20)
        b.save()

        items = Item.all()
        assert len(items) == 2

        by_name = {i.name: i for i in items}
        assert by_name["A"]._id == a._id
        assert by_name["A"]._snapshot == {"name": "A", "value": 10}
        assert by_name["B"]._id == b._id
        assert by_name["B"]._snapshot == {"name": "B", "value": 20}

    def test_queryset_first(self, db):
        """first() should return a single instance with correct metadata."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_first_items"
            name: str

        Item.set_db(db)
        saved = Item(name="Only")
        saved.save()

        item = Item.first()
        assert item is not None
        assert item.name == "Only"
        assert item._id == saved._id
        assert item._snapshot == {"name": "Only"}

    def test_as_dicts_bypass(self, db):
        """as_dicts() should bypass hydration."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_dict_items"
            name: str
            value: int = 0

        Item.set_db(db)
        Item(name="X", value=42).save()

        dicts = Item.query().as_dicts()
        assert len(dicts) == 1
        assert dicts[0]["name"] == "X"
        assert dicts[0]["value"] == 42
        assert dicts[0]["_id"] == 1


class TestMsgspecErrorPaths:
    """Test error paths and edge cases."""

    @pytest.fixture
    def db(self):
        return SQLerDB.in_memory()

    def test_set_db_emits_deprecation_warning(self, db):
        """set_db() should emit DeprecationWarning."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_warn_items"
            name: str

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Item.set_db(db)

        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "set_db() is deprecated" in str(w[0].message)

    def test_unbound_model_raises_not_bound_error(self):
        """Calling all() on an unbound model should raise NotBoundError."""
        from sqler import NotBoundError

        class Ghost(SQLerMsgspecModel):
            __tablename__ = "msg_ghost"
            name: str

        with pytest.raises(NotBoundError, match="not bound"):
            Ghost.all()

    def test_delete_unsaved_raises_value_error(self, db):
        """delete() on unsaved instance should raise ValueError."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_del_unsaved"
            name: str

        Item.set_db(db)

        with pytest.raises(ValueError, match="Cannot delete unsaved model"):
            Item(name="unsaved").delete()

    def test_refresh_unsaved_raises_value_error(self, db):
        """refresh() on unsaved instance should raise ValueError."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_refresh_unsaved"
            name: str

        Item.set_db(db)

        with pytest.raises(ValueError, match="Cannot refresh unsaved"):
            Item(name="unsaved").refresh()

    def test_refresh_deleted_row_raises_lookup_error(self, db):
        """refresh() on a row deleted from DB should raise LookupError."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_refresh_deleted"
            name: str

        Item.set_db(db)

        item = Item(name="doomed")
        item.save()
        saved_id = item._id

        # Delete directly in DB
        db.delete_document("msg_refresh_deleted", saved_id)

        with pytest.raises(LookupError, match="not found for refresh"):
            item.refresh()

    def test_invalid_on_delete_raises_value_error(self, db):
        """delete_with_policy with invalid policy should raise ValueError."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_bad_policy"
            name: str

        Item.set_db(db)
        item = Item(name="test")
        item.save()

        with pytest.raises(ValueError, match="on_delete must be"):
            item.delete_with_policy(on_delete="nuke")

    def test_from_ids_empty_list(self, db):
        """from_ids with empty list should return empty list without DB call."""

        class Item(SQLerMsgspecModel):
            __tablename__ = "msg_empty_ids"
            name: str

        Item.set_db(db)
        assert Item.from_ids([]) == []

    def test_date_serialization(self):
        """model_dump should serialize date to ISO format."""

        class Event(SQLerMsgspecModelBase):
            name: str
            day: date

        event = Event(name="Holiday", day=date(2024, 3, 15))
        data = event.model_dump()
        assert data["day"] == "2024-03-15"
        assert data["name"] == "Holiday"

    def test_reserved_word_table_name(self, db):
        """Reserved SQL words should get _tbl suffix."""

        class Table(SQLerMsgspecModel):
            name: str

        Table.set_db(db)
        assert Table._table == "table_tbl"
