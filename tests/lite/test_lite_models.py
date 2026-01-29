"""Tests for SQLer Lite models (Pydantic-free, dataclass-based).

These tests verify that lite models provide API compatibility with
Pydantic-based models while using standard library dataclasses.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pytest
from sqler import (
    PYDANTIC_AVAILABLE,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteModelBase,
    SQLerLiteSafeModel,
    get_model_backend,
    is_lite_model,
    is_pydantic_model,
)
from sqler.query import F


class TestCompatibilityHelpers:
    """Test compatibility detection helpers."""

    def test_pydantic_available(self):
        """Pydantic should be available in test environment."""
        assert PYDANTIC_AVAILABLE is True

    def test_get_model_backend(self):
        """Backend should be 'pydantic' when available."""
        assert get_model_backend() == "pydantic"

    def test_is_lite_model(self):
        """is_lite_model should correctly identify lite models."""

        @dataclass
        class LiteUser(SQLerLiteModel):
            name: str

        assert is_lite_model(LiteUser) is True
        assert is_lite_model(SQLerLiteModel) is True
        assert is_lite_model(str) is False

    def test_is_pydantic_model(self):
        """is_pydantic_model should correctly identify Pydantic models."""
        from sqler import SQLerModel

        class PydanticUser(SQLerModel):
            name: str

        assert is_pydantic_model(PydanticUser) is True
        assert is_pydantic_model(SQLerLiteModel) is False


class TestSQLerLiteModelBase:
    """Test the base class functionality."""

    def test_model_validate(self):
        """model_validate should create instance from dict."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str
            age: int

        user = User.model_validate({"name": "Alice", "age": 30})
        assert user.name == "Alice"
        assert user.age == 30

    def test_model_validate_with_id(self):
        """model_validate should handle _id field."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str

        user = User.model_validate({"name": "Bob", "_id": 42})
        assert user.name == "Bob"
        assert user._id == 42

    def test_model_validate_ignores_extra(self):
        """model_validate should ignore unknown fields (like Pydantic extra='ignore')."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str

        user = User.model_validate({"name": "Carol", "unknown_field": "value"})
        assert user.name == "Carol"
        assert not hasattr(user, "unknown_field")

    def test_model_dump(self):
        """model_dump should serialize to dict."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str
            age: int

        user = User(name="Dave", age=25)
        data = user.model_dump()
        assert data == {"name": "Dave", "age": 25}

    def test_model_dump_excludes_private(self):
        """model_dump should exclude fields starting with underscore."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str

        user = User(name="Eve")
        object.__setattr__(user, "_id", 100)
        data = user.model_dump()
        assert "_id" not in data
        assert data == {"name": "Eve"}

    def test_model_dump_exclude_none(self):
        """model_dump with exclude_none should skip None values."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str
            email: Optional[str] = None

        user = User(name="Frank", email=None)
        data = user.model_dump(exclude_none=True)
        assert "email" not in data
        assert data == {"name": "Frank"}

    def test_model_dump_exclude_set(self):
        """model_dump with exclude should skip specified fields."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str
            secret: str

        user = User(name="Grace", secret="hidden")
        data = user.model_dump(exclude={"secret"})
        assert "secret" not in data
        assert data == {"name": "Grace"}

    def test_model_fields(self):
        """model_fields should return field metadata."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str
            age: int

        fields = User.model_fields
        assert "name" in fields
        assert "age" in fields

    def test_get_field_names(self):
        """get_field_names should return public field names."""

        @dataclass
        class User(SQLerLiteModelBase):
            name: str
            age: int

        names = User.get_field_names()
        assert names == {"name", "age"}

    def test_datetime_serialization(self):
        """model_dump should serialize datetime to ISO format."""

        @dataclass
        class Event(SQLerLiteModelBase):
            name: str
            when: datetime

        now = datetime(2024, 1, 15, 10, 30, 0)
        event = Event(name="Meeting", when=now)
        data = event.model_dump()
        assert data["when"] == "2024-01-15T10:30:00"


class TestSQLerLiteModel:
    """Test SQLerLiteModel persistence."""

    @pytest.fixture
    def db(self):
        """Create in-memory database."""
        return SQLerDB.in_memory()

    def test_basic_crud(self, db):
        """Test create, read, update, delete operations."""

        @dataclass
        class User(SQLerLiteModel):
            __tablename__ = "users"
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

        @dataclass
        class Item(SQLerLiteModel):
            __tablename__ = "items"
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

        @dataclass
        class Item(SQLerLiteModel):
            __tablename__ = "items2"
            name: str

        Item.set_db(db)

        assert Item.count() == 0
        Item(name="A").save()
        Item(name="B").save()
        assert Item.count() == 2

    def test_query_filter(self, db):
        """Test filtering with expressions."""

        @dataclass
        class Product(SQLerLiteModel):
            __tablename__ = "products"
            name: str
            price: float

        Product.set_db(db)

        Product(name="Apple", price=1.50).save()
        Product(name="Banana", price=0.75).save()
        Product(name="Cherry", price=3.00).save()

        # Filter by price
        expensive = Product.filter(F("price") > 1.00).all()
        assert len(expensive) == 2
        names = {p.name for p in expensive}
        assert names == {"Apple", "Cherry"}

    def test_from_ids_batch(self, db):
        """Test batch loading by IDs."""

        @dataclass
        class Item(SQLerLiteModel):
            __tablename__ = "batch_items"
            name: str

        Item.set_db(db)

        ids = []
        for name in ["A", "B", "C", "D"]:
            item = Item(name=name)
            item.save()
            ids.append(item._id)

        # Load subset
        items = Item.from_ids([ids[0], ids[2]])
        assert len(items) == 2
        names = {i.name for i in items}
        assert names == {"A", "C"}

    def test_refresh(self, db):
        """Test refreshing instance from database."""

        @dataclass
        class Item(SQLerLiteModel):
            __tablename__ = "refresh_items"
            value: int

        Item.set_db(db)

        item = Item(value=100)
        item.save()

        # Modify directly in DB
        db.adapter.execute(
            "UPDATE refresh_items SET data = ? WHERE _id = ?",
            ['{"value": 999}', item._id],
        )
        db.adapter.auto_commit()

        # Refresh
        item.refresh()
        assert item.value == 999

    def test_dirty_tracking(self, db):
        """Test dirty field tracking."""

        @dataclass
        class Item(SQLerLiteModel):
            __tablename__ = "dirty_items"
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

        @dataclass
        class Category(SQLerLiteModel):
            name: str

        Category.set_db(db)
        assert Category._table == "categories"

        @dataclass
        class Box(SQLerLiteModel):
            name: str

        Box.set_db(db)
        assert Box._table == "boxes"


class TestSQLerLiteSafeModel:
    """Test SQLerLiteSafeModel with optimistic locking."""

    @pytest.fixture
    def db(self):
        """Create in-memory database."""
        return SQLerDB.in_memory()

    def test_version_increment(self, db):
        """Test that _version increments on save."""

        @dataclass
        class Counter(SQLerLiteSafeModel):
            __tablename__ = "counters"
            name: str
            value: int = 0

        Counter.set_db(db)

        c = Counter(name="test", value=0)
        c.save()
        assert c._version == 0

        c.value = 1
        c.save()
        assert c._version == 1

        c.value = 2
        c.save()
        assert c._version == 2

    def test_stale_version_error(self, db):
        """Test that stale version raises error."""
        from sqler import StaleVersionError

        @dataclass
        class Counter(SQLerLiteSafeModel):
            __tablename__ = "stale_counters"
            value: int = 0

        Counter.set_db(db)

        c1 = Counter(value=0)
        c1.save()

        # Load same record in another instance
        c2 = Counter.from_id(c1._id)

        # Update via c2
        c2.value = 100
        c2.save()

        # c1 has stale version, should fail
        c1.value = 200
        with pytest.raises(StaleVersionError):
            c1.save()

    def test_from_id_includes_version(self, db):
        """Test that from_id loads version."""

        @dataclass
        class Item(SQLerLiteSafeModel):
            __tablename__ = "version_items"
            name: str

        Item.set_db(db)

        item = Item(name="test")
        item.save()
        item.name = "updated"
        item.save()

        loaded = Item.from_id(item._id)
        assert loaded._version == 1

    def test_refresh_updates_version(self, db):
        """Test that refresh updates version."""

        @dataclass
        class Item(SQLerLiteSafeModel):
            __tablename__ = "refresh_version"
            value: int

        Item.set_db(db)

        item = Item(value=1)
        item.save()

        # Simulate external update
        item2 = Item.from_id(item._id)
        item2.value = 999
        item2.save()

        # Refresh original
        item.refresh()
        assert item.value == 999
        assert item._version == 1


class TestLiteModelRelations:
    """Test relationship handling in lite models."""

    @pytest.fixture
    def db(self):
        """Create in-memory database."""
        return SQLerDB.in_memory()

    def test_ref_encoding(self, db):
        """Test that related models are encoded as refs."""

        @dataclass
        class Author(SQLerLiteModel):
            __tablename__ = "authors"
            name: str

        @dataclass
        class Book(SQLerLiteModel):
            __tablename__ = "books"
            title: str
            author: Optional[Author] = None

        Author.set_db(db)
        Book.set_db(db)

        author = Author(name="Hemingway")
        author.save()

        book = Book(title="The Old Man and the Sea", author=author)
        book.save()

        # Verify ref encoding in database
        import json

        cursor = db.adapter.execute("SELECT data FROM books WHERE _id = ?", [book._id])
        row = cursor.fetchone()
        data = json.loads(row[0])
        assert data["author"] == {"_table": "authors", "_id": author._id}

    def test_ref_resolution(self, db):
        """Test that refs are resolved on load."""

        @dataclass
        class Author(SQLerLiteModel):
            __tablename__ = "authors2"
            name: str

        @dataclass
        class Book(SQLerLiteModel):
            __tablename__ = "books2"
            title: str
            author: Optional[Author] = None

        Author.set_db(db)
        Book.set_db(db)

        author = Author(name="Twain")
        author.save()

        book = Book(title="Tom Sawyer", author=author)
        book.save()

        # Load and verify resolution
        loaded = Book.from_id(book._id)
        assert isinstance(loaded.author, Author)
        assert loaded.author.name == "Twain"


class TestAsyncLiteModels:
    """Test async lite models."""

    @pytest.mark.asyncio
    async def test_async_crud(self):
        """Test async CRUD operations."""
        from sqler import AsyncSQLerDB, AsyncSQLerLiteModel

        db = AsyncSQLerDB.in_memory()
        await db.connect()

        @dataclass
        class User(AsyncSQLerLiteModel):
            __tablename__ = "async_users"
            name: str

        User.set_db(db)

        # Create
        user = User(name="Async Alice")
        await user.save()
        assert user._id is not None

        # Read
        loaded = await User.from_id(user._id)
        assert loaded.name == "Async Alice"

        # Update
        user.name = "Async Bob"
        await user.save()
        reloaded = await User.from_id(user._id)
        assert reloaded.name == "Async Bob"

        # Delete
        await user.delete()
        assert await User.from_id(user._id) is None

        await db.close()

    @pytest.mark.asyncio
    async def test_async_safe_model(self):
        """Test async safe model with versioning."""
        from sqler import AsyncSQLerDB, AsyncSQLerLiteSafeModel

        db = AsyncSQLerDB.in_memory()
        await db.connect()

        @dataclass
        class Counter(AsyncSQLerLiteSafeModel):
            __tablename__ = "async_counters"
            value: int = 0

        Counter.set_db(db)

        c = Counter(value=0)
        await c.save()
        assert c._version == 0

        c.value = 10
        await c.save()
        assert c._version == 1

        await db.close()
