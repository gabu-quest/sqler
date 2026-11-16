"""SoftDeleteMixin: soft_delete, restore, active, with_deleted and only_deleted."""

import pytest
from sqler import SoftDeleteMixin, SQLerDB, SQLerModel


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
