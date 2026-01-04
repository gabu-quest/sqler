"""Model mixins for common functionality.

This module provides reusable mixins for timestamps, soft delete,
and lifecycle hooks.
"""

from datetime import datetime, timezone
from typing import Any, ClassVar, Optional, Self

from pydantic import Field, PrivateAttr


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
    deleted_at timestamp. The mixin provides convenient class methods
    for querying active (non-deleted) and all records.

    Usage::

        class User(SoftDeleteMixin, SQLerModel):
            name: str

        user = User(name="Alice").save()
        user.soft_delete()  # Sets deleted_at instead of deleting
        user.restore()      # Clears deleted_at
        user.is_deleted     # True if soft-deleted

        # Query methods
        User.active()       # Only non-deleted records
        User.with_deleted() # All records including deleted
        User.only_deleted() # Only deleted records
    """

    deleted_at: Optional[datetime] = Field(default=None)

    # Class-level configuration for default query behavior
    _soft_delete_default_exclude: ClassVar[bool] = True

    @property
    def is_deleted(self) -> bool:
        """Return True if this record has been soft-deleted."""
        return self.deleted_at is not None  # type: ignore[attr-defined]

    def soft_delete(self) -> Self:
        """Mark this record as deleted without removing from database.

        Returns:
            Self: The soft-deleted instance.
        """
        self.deleted_at = datetime.now(timezone.utc)  # type: ignore[attr-defined]
        return self.save()  # type: ignore[attr-defined]

    def restore(self) -> Self:
        """Restore a soft-deleted record.

        Returns:
            Self: The restored instance.
        """
        self.deleted_at = None  # type: ignore[attr-defined]
        return self.save()  # type: ignore[attr-defined]

    def hard_delete(self) -> None:
        """Permanently delete this record from the database."""
        self.delete()  # type: ignore[attr-defined]

    @classmethod
    def active(cls):
        """Return a queryset that excludes soft-deleted records.

        This is the recommended way to query for active (non-deleted) records.

        Usage::

            # Get all active users
            active_users = User.active().all()

            # Filter active users
            admins = User.active().filter(F("role") == "admin").all()

        Returns:
            SQLerQuerySet: Queryset filtered to non-deleted records.
        """
        from sqler.query import F

        return cls.query().filter(F("deleted_at") == None)  # noqa: E711

    @classmethod
    def with_deleted(cls):
        """Return a queryset that includes soft-deleted records.

        Use this when you need to access all records regardless of deletion status.

        Usage::

            # Get all users including deleted
            all_users = User.with_deleted().all()

        Returns:
            SQLerQuerySet: Queryset including all records.
        """
        return cls.query()  # type: ignore[attr-defined]

    @classmethod
    def only_deleted(cls):
        """Return a queryset containing only soft-deleted records.

        Use this to find and potentially restore deleted records.

        Usage::

            # Get all deleted users
            deleted_users = User.only_deleted().all()

            # Restore a specific deleted user
            user = User.only_deleted().filter(F("email") == "alice@example.com").first()
            if user:
                user.restore()

        Returns:
            SQLerQuerySet: Queryset filtered to only deleted records.
        """
        from sqler.query import F

        return cls.query().filter(F("deleted_at") != None)  # noqa: E711


class AsyncSoftDeleteMixin:
    """Async version of SoftDeleteMixin for async models.

    Provides soft delete functionality with async save/delete methods.

    Usage::

        class User(AsyncSoftDeleteMixin, AsyncSQLerModel):
            name: str

        user = await User(name="Alice").save()
        await user.soft_delete()  # Sets deleted_at instead of deleting
        await user.restore()      # Clears deleted_at
        user.is_deleted           # True if soft-deleted

        # Query methods (return async querysets)
        await User.active().all()       # Only non-deleted records
        await User.with_deleted().all() # All records including deleted
        await User.only_deleted().all() # Only deleted records
    """

    deleted_at: Optional[datetime] = Field(default=None)

    _soft_delete_default_exclude: ClassVar[bool] = True

    @property
    def is_deleted(self) -> bool:
        """Return True if this record has been soft-deleted."""
        return self.deleted_at is not None  # type: ignore[attr-defined]

    async def soft_delete(self) -> Self:
        """Mark this record as deleted without removing from database (async).

        Returns:
            Self: The soft-deleted instance.
        """
        self.deleted_at = datetime.now(timezone.utc)  # type: ignore[attr-defined]
        return await self.save()  # type: ignore[attr-defined]

    async def restore(self) -> Self:
        """Restore a soft-deleted record (async).

        Returns:
            Self: The restored instance.
        """
        self.deleted_at = None  # type: ignore[attr-defined]
        return await self.save()  # type: ignore[attr-defined]

    async def hard_delete(self) -> None:
        """Permanently delete this record from the database (async)."""
        await self.delete()  # type: ignore[attr-defined]

    @classmethod
    def active(cls):
        """Return an async queryset that excludes soft-deleted records.

        Usage::

            # Get all active users
            active_users = await User.active().all()

            # Filter active users
            admins = await User.active().filter(F("role") == "admin").all()

        Returns:
            AsyncSQLerQuerySet: Queryset filtered to non-deleted records.
        """
        from sqler.query import F

        return cls.query().filter(F("deleted_at") == None)  # noqa: E711

    @classmethod
    def with_deleted(cls):
        """Return an async queryset that includes soft-deleted records.

        Usage::

            # Get all users including deleted
            all_users = await User.with_deleted().all()

        Returns:
            AsyncSQLerQuerySet: Queryset including all records.
        """
        return cls.query()  # type: ignore[attr-defined]

    @classmethod
    def only_deleted(cls):
        """Return an async queryset containing only soft-deleted records.

        Usage::

            # Get all deleted users
            deleted_users = await User.only_deleted().all()

            # Restore a specific deleted user
            user = await User.only_deleted().filter(F("email") == "alice@example.com").first()
            if user:
                await user.restore()

        Returns:
            AsyncSQLerQuerySet: Queryset filtered to only deleted records.
        """
        from sqler.query import F

        return cls.query().filter(F("deleted_at") != None)  # noqa: E711


class HooksMixin:
    """Mixin that provides lifecycle hooks for models.

    Override the hook methods to add custom behavior before/after
    save and delete operations. The hooks are called automatically
    when save() or delete() is called.

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

    def save(self) -> Self:
        """Save with before/after hooks.

        Calls before_save() first. If it returns False, the save is aborted.
        After a successful save, after_save() is called.

        Returns:
            Self: The saved instance.

        Raises:
            RuntimeError: If before_save() returns False.
        """
        if self._hooks_enabled and not self.before_save():
            raise RuntimeError(
                f"before_save() returned False, save aborted for {self.__class__.__name__}"
            )
        result = super().save()  # type: ignore[misc]
        if self._hooks_enabled:
            self.after_save()
        return result  # type: ignore[return-value]

    def delete(self) -> None:
        """Delete with before/after hooks.

        Calls before_delete() first. If it returns False, the delete is aborted.
        After a successful delete, after_delete() is called.

        Raises:
            RuntimeError: If before_delete() returns False.
        """
        if self._hooks_enabled and not self.before_delete():
            raise RuntimeError(
                f"before_delete() returned False, delete aborted for {self.__class__.__name__}"
            )
        super().delete()  # type: ignore[misc]
        if self._hooks_enabled:
            self.after_delete()


class AsyncHooksMixin:
    """Async version of HooksMixin for async models.

    Override the async hook methods to add custom behavior before/after
    save and delete operations. The hooks are called automatically.

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

    async def save(self) -> Self:
        """Save with before/after hooks (async).

        Calls before_save() first. If it returns False, the save is aborted.
        After a successful save, after_save() is called.

        Returns:
            Self: The saved instance.

        Raises:
            RuntimeError: If before_save() returns False.
        """
        if self._hooks_enabled and not await self.before_save():
            raise RuntimeError(
                f"before_save() returned False, save aborted for {self.__class__.__name__}"
            )
        result = await super().save()  # type: ignore[misc]
        if self._hooks_enabled:
            await self.after_save()
        return result  # type: ignore[return-value]

    async def delete(self) -> None:
        """Delete with before/after hooks (async).

        Calls before_delete() first. If it returns False, the delete is aborted.
        After a successful delete, after_delete() is called.

        Raises:
            RuntimeError: If before_delete() returns False.
        """
        if self._hooks_enabled and not await self.before_delete():
            raise RuntimeError(
                f"before_delete() returned False, delete aborted for {self.__class__.__name__}"
            )
        await super().delete()  # type: ignore[misc]
        if self._hooks_enabled:
            await self.after_delete()


class FullMixin(TimestampMixin, SoftDeleteMixin, HooksMixin):
    """Convenience mixin combining timestamps, soft delete, and hooks.

    Usage::

        class User(FullMixin, SQLerModel):
            name: str
    """

    pass


class AsyncFullMixin(TimestampMixin, AsyncSoftDeleteMixin, AsyncHooksMixin):
    """Async version of FullMixin.

    Usage::

        class User(AsyncFullMixin, AsyncSQLerModel):
            name: str
    """

    pass


class AuditMixin:
    """Mixin that provides audit logging for model changes.

    Tracks who created/updated records and when. Requires a way to get
    the current user (via class variable or override).

    Usage::

        class User(AuditMixin, SQLerModel):
            name: str

        # Set the current user for audit tracking
        AuditMixin.set_current_user("admin@example.com")

        user = User(name="Alice").save()
        print(user.created_by)  # "admin@example.com"
        print(user.updated_by)  # "admin@example.com"

        user.name = "Alice Smith"
        user.save()
        print(user.updated_by)  # "admin@example.com"
        print(user.updated_at)  # datetime of last save

    For web applications, set current_user in middleware::

        @app.middleware("http")
        async def audit_middleware(request, call_next):
            user = get_user_from_request(request)
            AuditMixin.set_current_user(user.email if user else "anonymous")
            response = await call_next(request)
            return response
    """

    created_at: Optional[datetime] = Field(default=None)
    updated_at: Optional[datetime] = Field(default=None)
    created_by: Optional[str] = Field(default=None)
    updated_by: Optional[str] = Field(default=None)

    # Thread-local storage for current user
    _current_user: ClassVar[Optional[str]] = None
    _current_user_getter: ClassVar[Optional[Any]] = None

    @classmethod
    def set_current_user(cls, user: Optional[str]) -> None:
        """Set the current user for audit tracking.

        Args:
            user: User identifier (email, username, ID, etc.)
        """
        cls._current_user = user

    @classmethod
    def set_current_user_getter(cls, getter: Any) -> None:
        """Set a callable that returns the current user.

        Args:
            getter: Callable that returns user identifier string.

        Usage::

            def get_current_user():
                return g.user.email if hasattr(g, 'user') else None

            AuditMixin.set_current_user_getter(get_current_user)
        """
        cls._current_user_getter = getter

    @classmethod
    def get_current_user(cls) -> Optional[str]:
        """Get the current user for audit tracking.

        Returns:
            Current user identifier or None.
        """
        if cls._current_user_getter is not None:
            try:
                return cls._current_user_getter()
            except Exception:
                return None
        return cls._current_user

    def _set_audit_fields(self) -> None:
        """Set audit fields before save."""
        now = datetime.now(timezone.utc)
        user = self.get_current_user()

        if self.created_at is None:  # type: ignore[attr-defined]
            self.created_at = now  # type: ignore[attr-defined]
            self.created_by = user  # type: ignore[attr-defined]

        self.updated_at = now  # type: ignore[attr-defined]
        self.updated_by = user  # type: ignore[attr-defined]


class AsyncAuditMixin:
    """Async version of AuditMixin for async models.

    Usage::

        class User(AsyncAuditMixin, AsyncSQLerModel):
            name: str

        AsyncAuditMixin.set_current_user("admin@example.com")
        user = await User(name="Alice").save()
    """

    created_at: Optional[datetime] = Field(default=None)
    updated_at: Optional[datetime] = Field(default=None)
    created_by: Optional[str] = Field(default=None)
    updated_by: Optional[str] = Field(default=None)

    _current_user: ClassVar[Optional[str]] = None
    _current_user_getter: ClassVar[Optional[Any]] = None

    @classmethod
    def set_current_user(cls, user: Optional[str]) -> None:
        """Set the current user for audit tracking."""
        cls._current_user = user

    @classmethod
    def set_current_user_getter(cls, getter: Any) -> None:
        """Set a callable that returns the current user."""
        cls._current_user_getter = getter

    @classmethod
    def get_current_user(cls) -> Optional[str]:
        """Get the current user for audit tracking."""
        if cls._current_user_getter is not None:
            try:
                return cls._current_user_getter()
            except Exception:
                return None
        return cls._current_user

    def _set_audit_fields(self) -> None:
        """Set audit fields before save."""
        now = datetime.now(timezone.utc)
        user = self.get_current_user()

        if self.created_at is None:  # type: ignore[attr-defined]
            self.created_at = now  # type: ignore[attr-defined]
            self.created_by = user  # type: ignore[attr-defined]

        self.updated_at = now  # type: ignore[attr-defined]
        self.updated_by = user  # type: ignore[attr-defined]


class AuditLogMixin:
    """Mixin that logs all changes to a separate audit log table.

    This creates a full audit trail of all changes to a model, storing
    the old and new values for each field that changed.

    Usage::

        class User(AuditLogMixin, SQLerModel):
            name: str
            email: str

        user = User(name="Alice", email="alice@example.com").save()
        user.email = "alice@newdomain.com"
        user.save()

        # Get audit log
        logs = user.get_audit_log()
        for log in logs:
            print(f"{log['action']} by {log['user']} at {log['timestamp']}")
            print(f"  Changes: {log['changes']}")

    The audit log is stored in a table named `{model_table}_audit`.
    """

    # Private attribute to store the snapshot before save
    _pre_save_snapshot: Optional[dict[str, Any]] = PrivateAttr(default=None)

    def _capture_snapshot(self) -> None:
        """Capture current state before save for change detection."""
        # Get model data excluding private fields
        data = {}
        for field_name in self.model_fields:  # type: ignore[attr-defined]
            try:
                data[field_name] = getattr(self, field_name)
            except AttributeError:
                pass
        self._pre_save_snapshot = data

    def _get_changes(self) -> dict[str, dict[str, Any]]:
        """Get dict of changed fields with old/new values."""
        if self._pre_save_snapshot is None:
            return {}

        changes = {}
        for field_name in self.model_fields:  # type: ignore[attr-defined]
            try:
                old_value = self._pre_save_snapshot.get(field_name)
                new_value = getattr(self, field_name)
                if old_value != new_value:
                    changes[field_name] = {"old": old_value, "new": new_value}
            except AttributeError:
                pass

        return changes

    def _log_audit(self, action: str, changes: Optional[dict] = None) -> None:
        """Log an audit entry.

        Args:
            action: Type of action (create, update, delete).
            changes: Dict of field changes (for updates).
        """
        import json

        db, table = self._require_binding()  # type: ignore[attr-defined]
        audit_table = f"{table}_audit"

        # Ensure audit table exists
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {audit_table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            user TEXT,
            timestamp TEXT NOT NULL,
            changes TEXT,
            snapshot TEXT
        );
        """
        db.adapter.execute(ddl)
        db.adapter.auto_commit()

        # Create audit entry
        record_id = getattr(self, "_id", None)
        user = AuditMixin.get_current_user() if hasattr(AuditMixin, "get_current_user") else None

        # Get current snapshot
        snapshot = {}
        for field_name in self.model_fields:  # type: ignore[attr-defined]
            try:
                value = getattr(self, field_name)
                # Convert datetime to string for JSON
                if isinstance(value, datetime):
                    value = value.isoformat()
                snapshot[field_name] = value
            except AttributeError:
                pass

        db.adapter.execute(
            f"INSERT INTO {audit_table} (record_id, action, user, timestamp, changes, snapshot) "
            f"VALUES (?, ?, ?, ?, ?, ?);",
            [
                record_id,
                action,
                user,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(changes) if changes else None,
                json.dumps(snapshot),
            ],
        )
        db.adapter.auto_commit()

    def get_audit_log(self) -> list[dict[str, Any]]:
        """Get the audit log for this record.

        Returns:
            List of audit log entries, newest first.
        """
        import json

        db, table = self._require_binding()  # type: ignore[attr-defined]
        audit_table = f"{table}_audit"
        record_id = getattr(self, "_id", None)

        if record_id is None:
            return []

        try:
            cursor = db.adapter.execute(
                f"SELECT action, user, timestamp, changes, snapshot "
                f"FROM {audit_table} WHERE record_id = ? ORDER BY _id DESC;",
                [record_id],
            )
            rows = cursor.fetchall()
        except Exception:
            return []

        logs = []
        for row in rows:
            logs.append(
                {
                    "action": row[0],
                    "user": row[1],
                    "timestamp": row[2],
                    "changes": json.loads(row[3]) if row[3] else None,
                    "snapshot": json.loads(row[4]) if row[4] else None,
                }
            )

        return logs


class AsyncAuditLogMixin:
    """Async version of AuditLogMixin.

    Usage::

        class User(AsyncAuditLogMixin, AsyncSQLerModel):
            name: str

        user = await User(name="Alice").save()
        logs = await user.get_audit_log()
    """

    _pre_save_snapshot: Optional[dict[str, Any]] = PrivateAttr(default=None)

    def _capture_snapshot(self) -> None:
        """Capture current state before save."""
        data = {}
        for field_name in self.model_fields:  # type: ignore[attr-defined]
            try:
                data[field_name] = getattr(self, field_name)
            except AttributeError:
                pass
        self._pre_save_snapshot = data

    def _get_changes(self) -> dict[str, dict[str, Any]]:
        """Get dict of changed fields with old/new values."""
        if self._pre_save_snapshot is None:
            return {}

        changes = {}
        for field_name in self.model_fields:  # type: ignore[attr-defined]
            try:
                old_value = self._pre_save_snapshot.get(field_name)
                new_value = getattr(self, field_name)
                if old_value != new_value:
                    changes[field_name] = {"old": old_value, "new": new_value}
            except AttributeError:
                pass

        return changes

    async def _log_audit(self, action: str, changes: Optional[dict] = None) -> None:
        """Log an audit entry (async)."""
        import json

        db, table = self._require_binding()  # type: ignore[attr-defined]
        audit_table = f"{table}_audit"

        ddl = f"""
        CREATE TABLE IF NOT EXISTS {audit_table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            user TEXT,
            timestamp TEXT NOT NULL,
            changes TEXT,
            snapshot TEXT
        );
        """
        cursor = await db.adapter.execute(ddl)
        await cursor.close()
        await db.adapter.auto_commit()

        record_id = getattr(self, "_id", None)
        user = (
            AsyncAuditMixin.get_current_user()
            if hasattr(AsyncAuditMixin, "get_current_user")
            else None
        )

        snapshot = {}
        for field_name in self.model_fields:  # type: ignore[attr-defined]
            try:
                value = getattr(self, field_name)
                if isinstance(value, datetime):
                    value = value.isoformat()
                snapshot[field_name] = value
            except AttributeError:
                pass

        cursor = await db.adapter.execute(
            f"INSERT INTO {audit_table} (record_id, action, user, timestamp, changes, snapshot) "
            f"VALUES (?, ?, ?, ?, ?, ?);",
            [
                record_id,
                action,
                user,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(changes) if changes else None,
                json.dumps(snapshot),
            ],
        )
        await cursor.close()
        await db.adapter.auto_commit()

    async def get_audit_log(self) -> list[dict[str, Any]]:
        """Get the audit log for this record (async)."""
        import json

        db, table = self._require_binding()  # type: ignore[attr-defined]
        audit_table = f"{table}_audit"
        record_id = getattr(self, "_id", None)

        if record_id is None:
            return []

        try:
            cursor = await db.adapter.execute(
                f"SELECT action, user, timestamp, changes, snapshot "
                f"FROM {audit_table} WHERE record_id = ? ORDER BY _id DESC;",
                [record_id],
            )
            rows = await cursor.fetchall()
            await cursor.close()
        except Exception:
            return []

        logs = []
        for row in rows:
            logs.append(
                {
                    "action": row[0],
                    "user": row[1],
                    "timestamp": row[2],
                    "changes": json.loads(row[3]) if row[3] else None,
                    "snapshot": json.loads(row[4]) if row[4] else None,
                }
            )

        return logs
