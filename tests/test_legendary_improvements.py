"""Tests for the legendary improvements to SQLer.

These tests cover the major enhancements added to make the library legendary:
- Soft delete auto-filtering
- Configurable intent rebasing
- Improved exception hierarchy
- NULL comparison handling
- Shared utility functions
"""

import sys
sys.path.insert(0, 'src')

import pytest
from sqler import (
    SQLerDB,
    SQLerModel,
    SQLerSafeModel,
    SoftDeleteMixin,
    TimestampMixin,
    F,
    RebaseConfig,
    DEFAULT_REBASE_CONFIG,
    PERMISSIVE_REBASE_CONFIG,
    NO_REBASE_CONFIG,
    CircularReferenceError,
    SQLerError,
)
from sqler.utils import (
    validate_table_name,
    is_ref_dict,
    collect_references,
    ReferenceTracker,
)
from sqler.models.utils import (
    compute_numeric_scalar_deltas,
    apply_numeric_scalar_deltas,
    can_rebase_deltas,
)
from sqler.exceptions import InvalidTableNameError


@pytest.fixture
def db():
    """Create an in-memory database for testing."""
    return SQLerDB.in_memory()


class TestSoftDeleteMixin:
    """Tests for the enhanced SoftDeleteMixin with auto-filtering methods."""

    def test_soft_delete_basic(self, db):
        """Test basic soft delete functionality."""
        class User(SoftDeleteMixin, SQLerModel):
            name: str

        User.set_db(db)
        user = User(name="Alice").save()

        assert user.deleted_at is None
        assert not user.is_deleted

        user.soft_delete()

        assert user.deleted_at is not None
        assert user.is_deleted

    def test_active_query_filters_deleted(self, db):
        """Test that .active() excludes soft-deleted records."""
        class User(SoftDeleteMixin, SQLerModel):
            name: str

        User.set_db(db)

        # Create some users
        u1 = User(name="Alice").save()
        u2 = User(name="Bob").save()
        u3 = User(name="Charlie").save()

        # Soft delete one
        u2.soft_delete()

        # active() should exclude deleted
        active = User.active().all()
        assert len(active) == 2
        assert all(not u.is_deleted for u in active)
        assert set(u.name for u in active) == {"Alice", "Charlie"}

    def test_with_deleted_returns_all(self, db):
        """Test that .with_deleted() returns all records."""
        class User(SoftDeleteMixin, SQLerModel):
            name: str

        User.set_db(db)

        u1 = User(name="Alice").save()
        u2 = User(name="Bob").save()
        u2.soft_delete()

        all_users = User.with_deleted().all()
        assert len(all_users) == 2

    def test_only_deleted_returns_deleted_only(self, db):
        """Test that .only_deleted() returns only deleted records."""
        class User(SoftDeleteMixin, SQLerModel):
            name: str

        User.set_db(db)

        u1 = User(name="Alice").save()
        u2 = User(name="Bob").save()
        u2.soft_delete()

        deleted = User.only_deleted().all()
        assert len(deleted) == 1
        assert deleted[0].name == "Bob"
        assert deleted[0].is_deleted

    def test_restore_soft_deleted(self, db):
        """Test restoring a soft-deleted record."""
        class User(SoftDeleteMixin, SQLerModel):
            name: str

        User.set_db(db)

        user = User(name="Alice").save()
        user.soft_delete()
        assert user.is_deleted

        user.restore()
        assert not user.is_deleted
        assert user.deleted_at is None

        # Should appear in active query
        active = User.active().all()
        assert len(active) == 1


class TestNullComparison:
    """Tests for proper NULL comparison handling in queries."""

    def test_eq_none_generates_is_null(self, db):
        """Test that == None generates IS NULL SQL."""
        from sqler import F

        expr = F("field") == None  # noqa: E711
        assert "IS NULL" in expr.sql
        assert expr.params == []

    def test_ne_none_generates_is_not_null(self, db):
        """Test that != None generates IS NOT NULL SQL."""
        from sqler import F

        expr = F("field") != None  # noqa: E711
        assert "IS NOT NULL" in expr.sql
        assert expr.params == []

    def test_null_comparison_works_in_queries(self, db):
        """Test NULL comparisons work correctly in actual queries."""
        from typing import Optional

        class Item(SQLerModel):
            name: str
            category: Optional[str] = None

        Item.set_db(db)

        # Create items with and without category
        Item(name="A", category="tools").save()
        Item(name="B", category=None).save()
        Item(name="C", category="food").save()

        # Find items without category
        no_category = Item.filter(F("category") == None).all()  # noqa: E711
        assert len(no_category) == 1
        assert no_category[0].name == "B"

        # Find items with category
        with_category = Item.filter(F("category") != None).all()  # noqa: E711
        assert len(with_category) == 2


class TestRebaseConfig:
    """Tests for configurable intent rebasing in SQLerSafeModel."""

    def test_default_rebase_config(self):
        """Test default rebase config allows count field with ±1."""
        config = DEFAULT_REBASE_CONFIG

        assert config.can_rebase("count", 1)
        assert config.can_rebase("count", -1)
        assert not config.can_rebase("count", 2)  # exceeds max_delta
        assert not config.can_rebase("views", 1)  # not in allowed_fields

    def test_permissive_rebase_config(self):
        """Test permissive config allows any integer field."""
        config = PERMISSIVE_REBASE_CONFIG

        assert config.can_rebase("count", 1)
        assert config.can_rebase("views", 1)
        assert config.can_rebase("likes", -1)
        assert not config.can_rebase("score", 5)  # exceeds max_delta

    def test_no_rebase_config(self):
        """Test disabled rebase config."""
        config = NO_REBASE_CONFIG

        assert not config.can_rebase("count", 1)
        assert not config.can_rebase("anything", 0)

    def test_custom_rebase_config(self):
        """Test custom rebase configuration."""
        config = RebaseConfig(
            enabled=True,
            allowed_fields={"likes", "views"},
            max_delta=10,
        )

        assert config.can_rebase("likes", 5)
        assert config.can_rebase("views", -10)
        assert not config.can_rebase("likes", 11)  # exceeds max_delta
        assert not config.can_rebase("count", 1)  # not in allowed_fields

    def test_can_rebase_deltas(self):
        """Test the can_rebase_deltas helper function."""
        # Single field delta within limits
        assert can_rebase_deltas({"count": 1})
        assert can_rebase_deltas({"count": -1})

        # Multiple fields should fail (safety check)
        assert not can_rebase_deltas({"count": 1, "views": 1})

        # Empty delta
        assert not can_rebase_deltas({})

        # Exceeds max_delta
        assert not can_rebase_deltas({"count": 5})

        # Field not in allowed_fields
        assert not can_rebase_deltas({"views": 1})


class TestNumericDeltaUtils:
    """Tests for numeric delta computation and application utilities."""

    def test_compute_deltas_basic(self):
        """Test basic delta computation."""
        orig = {"count": 5, "score": 10}
        target = {"count": 7, "score": 10}

        deltas = compute_numeric_scalar_deltas(orig, target)

        assert deltas == {"count": 2}  # score unchanged

    def test_compute_deltas_with_missing_field(self):
        """Test delta computation when original field is missing."""
        orig = {}
        target = {"count": 3}

        deltas = compute_numeric_scalar_deltas(orig, target)

        assert deltas == {"count": 3}

    def test_apply_deltas(self):
        """Test applying deltas to a document."""
        base = {"count": 10, "name": "test"}
        delta = {"count": 5}

        result = apply_numeric_scalar_deltas(base, delta)

        assert result["count"] == 15
        assert result["name"] == "test"


class TestTableNameValidation:
    """Tests for shared table name validation utility."""

    def test_valid_table_names(self):
        """Test that valid table names pass validation."""
        assert validate_table_name("users") == "users"
        assert validate_table_name("User_data") == "User_data"
        assert validate_table_name("_private") == "_private"
        assert validate_table_name("table123") == "table123"

    def test_invalid_table_names(self):
        """Test that invalid table names raise InvalidTableNameError."""
        with pytest.raises(InvalidTableNameError):
            validate_table_name("123starts_with_number")

        with pytest.raises(InvalidTableNameError):
            validate_table_name("has-dashes")

        with pytest.raises(InvalidTableNameError):
            validate_table_name("has spaces")

        with pytest.raises(InvalidTableNameError):
            validate_table_name("")


class TestReferenceUtilities:
    """Tests for reference-related utilities."""

    def test_is_ref_dict(self):
        """Test reference dictionary detection."""
        assert is_ref_dict({"_table": "users", "_id": 1})
        assert not is_ref_dict({"name": "Alice"})
        assert not is_ref_dict({"_table": "users"})  # missing _id
        assert not is_ref_dict([])
        assert not is_ref_dict("string")

    def test_collect_references(self):
        """Test reference collection from documents."""
        doc = {
            "name": "Order",
            "user": {"_table": "users", "_id": 1},
            "items": [
                {"_table": "products", "_id": 10},
                {"_table": "products", "_id": 11},
            ],
            "nested": {
                "supplier": {"_table": "suppliers", "_id": 5}
            }
        }

        refs = collect_references(doc)

        assert refs == {
            "users": {1},
            "products": {10, 11},
            "suppliers": {5},
        }

    def test_reference_tracker_basic(self):
        """Test basic reference tracking."""
        tracker = ReferenceTracker(max_depth=3)

        with tracker.visit("users", 1) as can_proceed:
            assert can_proceed

            with tracker.visit("posts", 10) as can_proceed2:
                assert can_proceed2

                # Circular reference detected
                with tracker.visit("users", 1) as can_proceed3:
                    assert not can_proceed3

    def test_reference_tracker_max_depth(self):
        """Test max depth enforcement."""
        tracker = ReferenceTracker(max_depth=2)

        with tracker.visit("a", 1):
            with tracker.visit("b", 2):
                # Should fail - max depth reached
                with tracker.visit("c", 3) as can_proceed:
                    assert not can_proceed


class TestExceptionHierarchy:
    """Tests for the exception hierarchy."""

    def test_circular_reference_error(self):
        """Test CircularReferenceError with path information."""
        path = [("users", 1), ("posts", 10), ("users", 1)]
        error = CircularReferenceError("Cycle detected", path=path)

        assert "Cycle detected" in str(error)
        assert "users:1" in str(error)
        assert error.path == path

    def test_sqler_error_base(self):
        """Test SQLerError base class."""
        error = SQLerError("Test error", details={"key": "value"})

        assert error.message == "Test error"
        assert error.details == {"key": "value"}


class TestTimestampMixin:
    """Tests for TimestampMixin functionality."""

    def test_timestamps_set_on_save(self, db):
        """Test that timestamps are set correctly."""
        class User(TimestampMixin, SQLerModel):
            name: str

        User.set_db(db)

        user = User(name="Alice")
        user._set_timestamps()
        user.save()

        assert user.created_at is not None
        assert user.updated_at is not None
        assert user.created_at == user.updated_at

    def test_updated_at_changes_on_update(self, db):
        """Test that updated_at changes on subsequent saves."""
        import time

        class User(TimestampMixin, SQLerModel):
            name: str

        User.set_db(db)

        user = User(name="Alice")
        user._set_timestamps()
        user.save()
        first_updated = user.updated_at

        time.sleep(0.01)  # Small delay

        user.name = "Alice Updated"
        user._set_timestamps()
        user.save()

        assert user.updated_at > first_updated
        # created_at should remain unchanged
        assert user.created_at == user.created_at


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
