"""Model mixins for common functionality.

This module provides reusable mixins for timestamps, soft delete,
and lifecycle hooks.
"""

import json
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Optional, Self

from sqler import _stored
from sqler.models import _dirty
from sqler.models._table_names import _instance_binding
from sqler.models._write_rules import WriteRules, deletable, new_only
from sqler.utils import validate_table_name

# Context variables for thread/async-safe user tracking
_current_user_var: ContextVar[Optional[str]] = ContextVar("audit_current_user", default=None)
_async_current_user_var: ContextVar[Optional[str]] = ContextVar(
    "async_audit_current_user", default=None
)


_ONE_MICROSECOND = timedelta(microseconds=1)


def _stamp_times(created_at: Optional[datetime], updated_at: Optional[datetime]) -> tuple[datetime, datetime]:
    """The one timestamp rule: return the ``(created_at, updated_at)`` to store on save.

    * First save of a new object (no ``created_at``): both are the same ``now``.
    * A caller-set ``created_at`` is kept; ``updated_at`` is ``now``, or one microsecond
      after ``created_at`` when ``now`` is not later.
    * Any save of an object that already has an ``updated_at`` gives a value strictly
      later than it, even when the clock has not advanced or has gone backwards.

    A naive datetime already on the object raises ``TypeError`` (it cannot be compared
    with the aware ``now``); that is unchanged.
    """
    now = datetime.now(timezone.utc)
    if created_at is None:
        created_at = now
        if updated_at is None:
            return created_at, now
    floor = created_at if updated_at is None else max(created_at, updated_at)
    return created_at, max(now, floor + _ONE_MICROSECOND)


class TimestampMixin(WriteRules):
    """Mixin that automatically manages created_at and updated_at fields.

    Usage::

        class User(TimestampMixin, SQLerModel):
            name: str

        user = User(name="Alice").save()
        print(user.created_at)  # datetime when created
        print(user.updated_at)  # datetime when last saved
    """

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def _set_timestamps(self) -> None:
        """Set timestamp fields before save."""
        self.created_at, self.updated_at = _stamp_times(self.created_at, self.updated_at)  # type: ignore[attr-defined]

    def _stamp(self) -> None:
        self._set_timestamps()
        super()._stamp()


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

    deleted_at: Optional[datetime] = None

    # Class-level configuration for default query behavior
    _soft_delete_default_exclude: ClassVar[bool] = True

    @property
    def is_deleted(self) -> bool:
        """Return True if this record has been soft-deleted."""
        return self.deleted_at is not None  # type: ignore[attr-defined]

    def soft_delete(self, *, db=None) -> Self:
        """Mark this record as deleted without removing from database.

        Returns:
            Self: The soft-deleted instance.
        """
        self.deleted_at = datetime.now(timezone.utc)  # type: ignore[attr-defined]
        return self.save(db=db)  # type: ignore[attr-defined]

    def restore(self, *, db=None) -> Self:
        """Restore a soft-deleted record.

        Returns:
            Self: The restored instance.
        """
        self.deleted_at = None  # type: ignore[attr-defined]
        return self.save(db=db)  # type: ignore[attr-defined]

    def hard_delete(self, *, db=None) -> None:
        """Permanently delete this record from the database."""
        self.delete(db=db)  # type: ignore[attr-defined]

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

    deleted_at: Optional[datetime] = None

    _soft_delete_default_exclude: ClassVar[bool] = True

    @property
    def is_deleted(self) -> bool:
        """Return True if this record has been soft-deleted."""
        return self.deleted_at is not None  # type: ignore[attr-defined]

    async def soft_delete(self, *, db=None) -> Self:
        """Mark this record as deleted without removing from database (async).

        Returns:
            Self: The soft-deleted instance.
        """
        self.deleted_at = datetime.now(timezone.utc)  # type: ignore[attr-defined]
        return await self.save(db=db)  # type: ignore[attr-defined]

    async def restore(self, *, db=None) -> Self:
        """Restore a soft-deleted record (async).

        Returns:
            Self: The restored instance.
        """
        self.deleted_at = None  # type: ignore[attr-defined]
        return await self.save(db=db)  # type: ignore[attr-defined]

    async def hard_delete(self, *, db=None) -> None:
        """Permanently delete this record from the database (async)."""
        await self.delete(db=db)  # type: ignore[attr-defined]

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

    def save(self, *, db=None, _partial: bool = False) -> Self:
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
        result = super().save(db=db, _partial=_partial)  # type: ignore[misc]
        if self._hooks_enabled:
            self.after_save()
        return result  # type: ignore[return-value]

    @classmethod
    def save_many(cls, instances: list, *, db=None) -> list:
        """Insert new instances in one batch, with each one's hooks.

        ``before_save()`` runs on each instance, in order, before anything is stamped or
        written; one that returns False raises ``RuntimeError`` naming its index, and
        nothing is written. ``after_save()`` runs on each, in order, once the batch is
        written.
        """
        if not cls._hooks_enabled:
            return super().save_many(instances, db=db)  # type: ignore[misc]
        new_only(instances)
        for index, inst in enumerate(instances):
            if not inst.before_save():
                raise RuntimeError(
                    f"before_save() returned False for instance {index}, "
                    f"save_many aborted for {cls.__name__}"
                )
        result = super().save_many(instances, db=db)  # type: ignore[misc]
        for inst in instances:
            inst.after_save()
        return result

    def delete_with_policy(self, *, db=None, on_delete: str = "restrict") -> None:
        """Delete with before/after hooks (``delete()`` comes through here).

        Calls before_delete() first. If it returns False, the delete is aborted.
        After a successful delete, after_delete() is called.

        Raises:
            RuntimeError: If before_delete() returns False.
        """
        if self._hooks_enabled:
            deletable(self, on_delete)
            if not self.before_delete():
                raise RuntimeError(
                    f"before_delete() returned False, delete aborted for {self.__class__.__name__}"
                )
        super().delete_with_policy(db=db, on_delete=on_delete)  # type: ignore[misc]
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

    async def save(self, *, db=None, _partial: bool = False) -> Self:
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
        result = await super().save(db=db, _partial=_partial)  # type: ignore[misc]
        if self._hooks_enabled:
            await self.after_save()
        return result  # type: ignore[return-value]

    @classmethod
    async def save_many(cls, instances: list, *, db=None) -> list:
        """Insert new instances in one batch, with each one's hooks (async); see
        :meth:`HooksMixin.save_many`."""
        if not cls._hooks_enabled:
            return await super().save_many(instances, db=db)  # type: ignore[misc]
        new_only(instances)
        for index, inst in enumerate(instances):
            if not await inst.before_save():
                raise RuntimeError(
                    f"before_save() returned False for instance {index}, "
                    f"save_many aborted for {cls.__name__}"
                )
        result = await super().save_many(instances, db=db)  # type: ignore[misc]
        for inst in instances:
            await inst.after_save()
        return result

    async def delete_with_policy(self, *, db=None, on_delete: str = "restrict") -> None:
        """Delete with before/after hooks (async; ``delete()`` comes through here).

        Calls before_delete() first. If it returns False, the delete is aborted.
        After a successful delete, after_delete() is called.

        Raises:
            RuntimeError: If before_delete() returns False.
        """
        if self._hooks_enabled:
            deletable(self, on_delete)
            if not await self.before_delete():
                raise RuntimeError(
                    f"before_delete() returned False, delete aborted for {self.__class__.__name__}"
                )
        await super().delete_with_policy(db=db, on_delete=on_delete)  # type: ignore[misc]
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


class AuditMixin(WriteRules):
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

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None

    # Optional custom getter (takes precedence over context var)
    _current_user_getter: ClassVar[Optional[Any]] = None

    @classmethod
    def set_current_user(cls, user: Optional[str]) -> None:
        """Set the current user for audit tracking (thread/async-safe).

        Args:
            user: User identifier (email, username, ID, etc.)
        """
        _current_user_var.set(user)

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
        return _current_user_var.get()

    def _set_audit_fields(self) -> None:
        """Set audit fields before save."""
        user = self.get_current_user()

        if self.created_at is None:  # type: ignore[attr-defined]
            self.created_by = user  # type: ignore[attr-defined]
        self.created_at, self.updated_at = _stamp_times(self.created_at, self.updated_at)  # type: ignore[attr-defined]
        self.updated_by = user  # type: ignore[attr-defined]

    def _stamp(self) -> None:
        self._set_audit_fields()
        super()._stamp()


class AsyncAuditMixin(WriteRules):
    """Async version of AuditMixin for async models.

    Usage::

        class User(AsyncAuditMixin, AsyncSQLerModel):
            name: str

        AsyncAuditMixin.set_current_user("admin@example.com")
        user = await User(name="Alice").save()
    """

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None

    # Optional custom getter (takes precedence over context var)
    _current_user_getter: ClassVar[Optional[Any]] = None

    @classmethod
    def set_current_user(cls, user: Optional[str]) -> None:
        """Set the current user for audit tracking (async-safe)."""
        _async_current_user_var.set(user)

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
        return _async_current_user_var.get()

    def _set_audit_fields(self) -> None:
        """Set audit fields before save."""
        user = self.get_current_user()

        if self.created_at is None:  # type: ignore[attr-defined]
            self.created_by = user  # type: ignore[attr-defined]
        self.created_at, self.updated_at = _stamp_times(self.created_at, self.updated_at)  # type: ignore[attr-defined]
        self.updated_by = user  # type: ignore[attr-defined]

    def _stamp(self) -> None:
        self._set_audit_fields()
        super()._stamp()


def _audit_table_ddl(audit_table: str) -> str:
    return f"""
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


def _audit_insert(
    audit_table: str, record_id, action: str, user, changes: Optional[dict], snapshot: dict
) -> tuple[str, list]:
    """The statement and parameters of one audit row; both columns are documents in stored form."""
    return (
        f"INSERT INTO {audit_table} (record_id, action, user, timestamp, changes, snapshot) "
        f"VALUES (?, ?, ?, ?, ?, ?);",
        [
            record_id,
            action,
            user,
            datetime.now(timezone.utc).isoformat(),
            _stored.dumps(changes) if changes else None,
            _stored.dumps(snapshot),
        ],
    )


def _audit_changes(old_row: Optional[dict], written: dict) -> dict[str, dict[str, Any]]:
    """What an update changed: for each key of the two stored documents that differs, its value
    in the row read before the save and in the document the save wrote (``None`` where a side
    has no such key). No row before the save, no changes."""
    if old_row is None:
        return {}
    old = _dirty.snapshot(old_row)
    return {
        key: {"old": old.get(key), "new": written.get(key)}
        for key in sorted(_dirty.changed_fields(old, written))
    }


def _audit_statements(audit_table: str, user, rows) -> list[tuple[str, list]]:
    """The audit rows for rows a write changed (see ``_write_rules``): a create and a
    delete each one, an update one where its stored document changed."""
    statements = []
    for action, record_id, before, after in rows:
        if action == "create":
            statements.append(_audit_insert(audit_table, record_id, action, user, None, after))
        elif action == "delete":
            statements.append(_audit_insert(audit_table, record_id, action, user, None, before))
        elif changes := _audit_changes(before, after):
            statements.append(_audit_insert(audit_table, record_id, action, user, changes, after))
    return statements


def _audit_entries(rows) -> list[dict[str, Any]]:
    return [
        {
            "action": row[0],
            "user": row[1],
            "timestamp": row[2],
            "changes": json.loads(row[3]) if row[3] else None,
            "snapshot": json.loads(row[4]) if row[4] else None,
        }
        for row in rows
    ]


class AuditLogMixin(WriteRules):
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

    _row_rules: ClassVar[bool] = True

    @classmethod
    def _rows_written(cls, db, table: str, rows) -> None:
        """Write the audit rows of ``rows`` (inside the write's transaction)."""
        audit_table = validate_table_name(f"{table}_audit")
        statements = _audit_statements(audit_table, AuditMixin.get_current_user(), rows)
        if statements:
            db.adapter.execute(_audit_table_ddl(audit_table))
            for sql, params in statements:
                db.adapter.execute(sql, params)
        super()._rows_written(db, table, rows)

    def get_audit_log(self, *, db=None) -> list[dict[str, Any]]:
        """Get the audit log for this record.

        Returns:
            List of audit log entries, newest first.
        """
        db, table = _instance_binding(self, db)
        audit_table = validate_table_name(f"{table}_audit")
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

        return _audit_entries(rows)


class AsyncAuditLogMixin(WriteRules):
    """Async version of AuditLogMixin.

    Usage::

        class User(AsyncAuditLogMixin, AsyncSQLerModel):
            name: str

        user = await User(name="Alice").save()
        logs = await user.get_audit_log()
    """

    _row_rules: ClassVar[bool] = True

    @classmethod
    async def _arows_written(cls, db, table: str, rows) -> None:
        """Write the audit rows of ``rows`` (inside the write's transaction)."""
        audit_table = validate_table_name(f"{table}_audit")
        statements = _audit_statements(audit_table, AsyncAuditMixin.get_current_user(), rows)
        if statements:
            cursor = await db.adapter.execute(_audit_table_ddl(audit_table))
            await cursor.close()
            for sql, params in statements:
                cursor = await db.adapter.execute(sql, params)
                await cursor.close()
        await super()._arows_written(db, table, rows)

    async def get_audit_log(self, *, db=None) -> list[dict[str, Any]]:
        """Get the audit log for this record (async)."""
        db, table = _instance_binding(self, db)
        audit_table = validate_table_name(f"{table}_audit")
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

        return _audit_entries(rows)
