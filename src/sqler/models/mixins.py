"""Model mixins for common functionality.

This module provides reusable mixins for timestamps, soft delete,
and lifecycle hooks.
"""

from datetime import datetime, timezone
from typing import Any, ClassVar, Optional, TypeVar

from pydantic import Field, PrivateAttr

T = TypeVar("T")


class TimestampMixin:
    """Mixin that automatically manages created_at and updated_at fields.

    Usage::

        class User(TimestampMixin, SQLerModel):
            name: str

        user = User(name="Alice").save()
        print(user.created_at)  # datetime when created
        print(user.updated_at)  # datetime when last saved
    """

    created_at: Optional[datetime] = Field(default=None)
    updated_at: Optional[datetime] = Field(default=None)

    def _set_timestamps(self) -> None:
        """Set timestamp fields before save."""
        now = datetime.now(timezone.utc)
        if self.created_at is None:  # type: ignore[attr-defined]
            self.created_at = now  # type: ignore[attr-defined]
        self.updated_at = now  # type: ignore[attr-defined]


class SoftDeleteMixin:
    """Mixin that provides soft delete functionality.

    Instead of actually deleting records, this marks them with a
    deleted_at timestamp. Use query().exclude_deleted() to filter
    out soft-deleted records.

    Usage::

        class User(SoftDeleteMixin, SQLerModel):
            name: str

        user = User(name="Alice").save()
        user.soft_delete()  # Sets deleted_at instead of deleting
        user.restore()      # Clears deleted_at
        user.is_deleted     # True if soft-deleted
    """

    deleted_at: Optional[datetime] = Field(default=None)

    @property
    def is_deleted(self) -> bool:
        """Return True if this record has been soft-deleted."""
        return self.deleted_at is not None  # type: ignore[attr-defined]

    def soft_delete(self: T) -> T:
        """Mark this record as deleted without removing from database.

        Returns:
            Self: The soft-deleted instance.
        """
        self.deleted_at = datetime.now(timezone.utc)  # type: ignore[attr-defined]
        return self.save()  # type: ignore[attr-defined]

    def restore(self: T) -> T:
        """Restore a soft-deleted record.

        Returns:
            Self: The restored instance.
        """
        self.deleted_at = None  # type: ignore[attr-defined]
        return self.save()  # type: ignore[attr-defined]

    def hard_delete(self) -> None:
        """Permanently delete this record from the database."""
        self.delete()  # type: ignore[attr-defined]


class HooksMixin:
    """Mixin that provides lifecycle hooks for models.

    Override the hook methods to add custom behavior before/after
    save and delete operations.

    Usage::

        class User(HooksMixin, SQLerModel):
            name: str
            email: str

            def before_save(self) -> bool:
                # Normalize email before saving
                self.email = self.email.lower()
                return True  # Return False to abort save

            def after_save(self) -> None:
                # Send notification after save
                print(f"Saved user {self.name}")

            def before_delete(self) -> bool:
                # Check if user can be deleted
                return not self.is_admin

            def after_delete(self) -> None:
                # Cleanup after delete
                print(f"Deleted user {self.name}")
    """

    # Class variable to track if hooks are enabled
    _hooks_enabled: ClassVar[bool] = True

    def before_save(self) -> bool:
        """Called before saving the model.

        Override to add custom validation or transformation logic.

        Returns:
            bool: True to proceed with save, False to abort.
        """
        return True

    def after_save(self) -> None:
        """Called after the model is saved.

        Override to add post-save logic like notifications or logging.
        """
        pass

    def before_delete(self) -> bool:
        """Called before deleting the model.

        Override to add deletion validation logic.

        Returns:
            bool: True to proceed with delete, False to abort.
        """
        return True

    def after_delete(self) -> None:
        """Called after the model is deleted.

        Override to add cleanup logic.
        """
        pass


class AsyncHooksMixin:
    """Async version of HooksMixin for async models.

    Usage::

        class User(AsyncHooksMixin, AsyncSQLerModel):
            name: str
            email: str

            async def before_save(self) -> bool:
                self.email = self.email.lower()
                return True

            async def after_save(self) -> None:
                await send_notification(self)
    """

    _hooks_enabled: ClassVar[bool] = True

    async def before_save(self) -> bool:
        """Called before saving the model (async).

        Returns:
            bool: True to proceed with save, False to abort.
        """
        return True

    async def after_save(self) -> None:
        """Called after the model is saved (async)."""
        pass

    async def before_delete(self) -> bool:
        """Called before deleting the model (async).

        Returns:
            bool: True to proceed with delete, False to abort.
        """
        return True

    async def after_delete(self) -> None:
        """Called after the model is deleted (async)."""
        pass


class FullMixin(TimestampMixin, SoftDeleteMixin, HooksMixin):
    """Convenience mixin combining timestamps, soft delete, and hooks.

    Usage::

        class User(FullMixin, SQLerModel):
            name: str
    """

    pass


class AsyncFullMixin(TimestampMixin, SoftDeleteMixin, AsyncHooksMixin):
    """Async version of FullMixin.

    Usage::

        class User(AsyncFullMixin, AsyncSQLerModel):
            name: str
    """

    pass
