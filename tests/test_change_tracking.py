"""Change tracking: ChangeTracker, TrackedModel and DiffMixin."""

import pytest
from sqler import (
    ChangeTracker,
    DiffMixin,
    QueryCache,
    SQLerDB,
    SQLerModel,
    TrackedModel,
)


class TestChangeTracker:
    """Tests for ChangeTracker class."""

    def test_tracker_snapshot(self):
        """Snapshot captures initial state."""
        tracker = ChangeTracker()
        tracker.snapshot({"name": "Alice", "age": 30})

        assert tracker._original == {"name": "Alice", "age": 30}

    def test_tracker_detect_changes(self):
        """Detects changed fields."""
        tracker = ChangeTracker()
        tracker.snapshot({"name": "Alice", "age": 30})

        changed = tracker.get_changed_fields({"name": "Bob", "age": 30})

        assert changed == {"name"}

    def test_tracker_get_changes(self):
        """Gets old and new values for changes."""
        tracker = ChangeTracker()
        tracker.snapshot({"name": "Alice", "age": 30})

        changes = tracker.get_changes({"name": "Bob", "age": 31})

        assert changes == {
            "name": ("Alice", "Bob"),
            "age": (30, 31),
        }


class TestTrackedModel:
    """Tests for TrackedModel mixin."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class User(TrackedModel, SQLerModel):
            name: str
            email: str
            age: int

        User.set_db(self.db)
        self.User = User

    def test_model_is_dirty_after_change(self):
        """Model is dirty after field change."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()
        user.mark_clean()

        user.name = "Bob"

        assert user.is_dirty
        assert "name" in user.changed_fields

    def test_model_not_dirty_after_save(self):
        """Model is clean after save."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()

        assert not user.is_dirty

    def test_get_changes(self):
        """get_changes returns old and new values."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()
        user.mark_clean()

        user.name = "Bob"
        user.age = 31

        changes = user.get_changes()

        assert changes["name"] == ("Alice", "Bob")
        assert changes["age"] == (30, 31)

    def test_revert_changes(self):
        """revert_changes restores original values."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()
        user.mark_clean()

        user.name = "Bob"
        user.revert_changes()

        assert user.name == "Alice"
        assert not user.is_dirty


class TestDiffMixin:
    """Tests for DiffMixin."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Item(DiffMixin, SQLerModel):
            name: str
            quantity: int

        Item.set_db(self.db)
        self.Item = Item

    def test_diff_detects_differences(self):
        """diff() detects field differences."""
        item1 = self.Item(name="Apple", quantity=10)
        item2 = self.Item(name="Apple", quantity=15)

        diff = item1.diff(item2)

        assert diff == {"quantity": (10, 15)}

    def test_is_equal(self):
        """is_equal() compares field values."""
        item1 = self.Item(name="Apple", quantity=10)
        item2 = self.Item(name="Apple", quantity=10)
        item3 = self.Item(name="Orange", quantity=10)

        assert item1.is_equal(item2)
        assert not item1.is_equal(item3)

    def test_clone(self):
        """clone() creates a copy with overrides."""
        item = self.Item(name="Apple", quantity=10)
        item.save()

        cloned = item.clone(quantity=20)

        assert cloned.name == "Apple"
        assert cloned.quantity == 20
        assert cloned._id is None  # Clone has no _id


class TestTrackedModelWithCache:
    """A tracked model held in the query cache."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

    def test_tracked_model_with_cache(self):
        """TrackedModel works with caching."""

        class Item(TrackedModel, SQLerModel):
            name: str
            count: int

        Item.set_db(self.db)

        cache = QueryCache(max_size=100)

        # Create and cache
        item = Item(name="Test", count=10)
        item.save()
        cache.set("item:1", item)

        # Modify
        item.name = "Modified"
        assert item.is_dirty

        # Cache still has old version (by design)
        cached = cache.get("item:1")
        assert cached.name == "Modified"  # Same object reference
