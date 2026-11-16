"""Model change tracking for SQLer.

Provides dirty checking, change history, and partial updates
to optimize writes and enable audit trails.

Usage::

    from sqler.tracking import TrackedModel

    class User(TrackedModel):
        name: str
        email: str
        age: int

    user = User.from_id(1)
    user.name = "New Name"

    # Check what changed
    print(user.is_dirty)  # True
    print(user.changed_fields)  # {'name'}
    print(user.get_changes())  # {'name': ('Old Name', 'New Name')}

    # Save only changed fields (with PartialUpdateMixin)
    user.save_partial()  # writes only 'name'
"""

import inspect
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Self, Set, Tuple, TypeVar

T = TypeVar("T", bound="TrackedModel")

if TYPE_CHECKING:
    from sqler.models._host import ModelHost as _ModelHost
else:
    _ModelHost = object


@dataclass
class FieldChange:
    """Represents a change to a field."""

    field: str
    old_value: Any
    new_value: Any
    changed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "changed_at": self.changed_at.isoformat(),
        }


class ChangeTracker:
    """Tracks changes to model fields."""

    __slots__ = ("_original", "_changes", "_tracking_enabled")

    def __init__(self):
        self._original: dict[str, Any] = {}
        self._changes: list[FieldChange] = []
        self._tracking_enabled: bool = True

    def snapshot(self, data: dict[str, Any]) -> None:
        """Take a snapshot of current state."""
        self._original = deepcopy(data)
        self._changes = []

    def record_change(self, field: str, old_value: Any, new_value: Any) -> None:
        """Record a field change."""
        if not self._tracking_enabled:
            return
        self._changes.append(
            FieldChange(
                field=field,
                old_value=deepcopy(old_value),
                new_value=deepcopy(new_value),
                changed_at=datetime.now(),
            )
        )

    def get_changed_fields(self, current: dict[str, Any]) -> Set[str]:
        """Get set of fields that have changed from original."""
        changed = set()
        for field, original_value in self._original.items():
            if field in current and current[field] != original_value:
                changed.add(field)
        return changed

    def get_changes(self, current: dict[str, Any]) -> dict[str, Tuple[Any, Any]]:
        """Get dict of field -> (old_value, new_value) for changed fields."""
        changes = {}
        for field in self.get_changed_fields(current):
            changes[field] = (self._original.get(field), current.get(field))
        return changes

    def get_change_history(self) -> list[FieldChange]:
        """Get full change history."""
        return list(self._changes)

    def reset(self, data: dict[str, Any]) -> None:
        """Reset tracking with new snapshot."""
        self.snapshot(data)

    @property
    def has_changes(self) -> bool:
        """Check if there are recorded changes."""
        return len(self._changes) > 0


class _ChangeTracking(_ModelHost):
    """The tracker of a model and its answers, for :class:`TrackedModel` and
    :class:`AsyncTrackedModel`.

    ``is_dirty`` here is a property, where the model bases have an ``is_dirty()`` method:
    ``model.is_dirty`` is the tracker's answer, ``SQLerModel.is_dirty(model)`` the base's.
    """

    _tracker: Optional[ChangeTracker] = None

    def __init__(self, **data):
        super().__init__(**data)
        self._init_tracker()

    def __post_init__(self) -> None:
        # a lite model: its dataclass ``__init__`` replaces the one above
        super().__post_init__()  # type: ignore[misc]
        self._init_tracker()

    def _init_tracker(self) -> None:
        """Initialize the change tracker."""
        tracker = ChangeTracker()
        object.__setattr__(self, "_tracker", tracker)
        tracker.snapshot(self._get_trackable_data())

    def _get_trackable_data(self) -> dict[str, Any]:
        """Get current field values for tracking."""
        data = {}
        for field_name in type(self).model_fields:
            if not field_name.startswith("_"):
                data[field_name] = getattr(self, field_name, None)
        return data

    def __setattr__(self, name: str, value: Any) -> None:
        """Track field changes on assignment."""
        # Get old value before setting
        if (
            hasattr(self, "_tracker")
            and self._tracker is not None
            and not name.startswith("_")
            and name in type(self).model_fields
        ):
            old_value = getattr(self, name, None)
            super().__setattr__(name, value)
            if old_value != value:
                self._tracker.record_change(name, old_value, value)
        else:
            super().__setattr__(name, value)

    @property
    def is_dirty(self) -> bool:
        """Check if any tracked fields have changed."""
        if self._tracker is None:
            return False
        return len(self.changed_fields) > 0

    @property
    def changed_fields(self) -> Set[str]:
        """Get set of changed field names."""
        if self._tracker is None:
            return set()
        return self._tracker.get_changed_fields(self._get_trackable_data())

    def get_changes(self) -> dict[str, Tuple[Any, Any]]:
        """Get dict of field -> (old_value, new_value).

        Returns:
            Dictionary mapping field names to (old, new) value tuples.
        """
        if self._tracker is None:
            return {}
        return self._tracker.get_changes(self._get_trackable_data())

    def get_change_history(self) -> list[FieldChange]:
        """Get full change history with timestamps."""
        if self._tracker is None:
            return []
        return self._tracker.get_change_history()

    def mark_clean(self) -> None:
        """Mark the model as clean (no pending changes)."""
        if self._tracker is not None:
            self._tracker.snapshot(self._get_trackable_data())

    def revert_changes(self) -> None:
        """Revert all changes to original values."""
        if self._tracker is None:
            return
        for field, original_value in self._tracker._original.items():
            object.__setattr__(self, field, original_value)
        self._tracker._changes = []

    def revert_field(self, field: str) -> None:
        """Revert a specific field to its original value."""
        if self._tracker is None or field not in self._tracker._original:
            return
        object.__setattr__(self, field, self._tracker._original[field])


class TrackedModel(_ChangeTracking):
    """Mixin for a sync model (``SQLerModel``, ``SQLerSafeModel`` and their lite twins) to
    enable change tracking; an async model uses :class:`AsyncTrackedModel`.

    Usage::

        class User(TrackedModel, SQLerModel):
            name: str
            email: str

        user = User.from_id(1)
        user.name = "New Name"

        print(user.is_dirty)  # True
        print(user.changed_fields)  # {'name'}
        print(user.get_changes())  # {'name': ('Old Name', 'New Name')}

        user.save()  # the tracker is clean again
    """

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if any(inspect.iscoroutinefunction(vars(k).get("save")) for k in cls.__mro__):
            raise TypeError(
                f"{cls.__name__} is an async model: use AsyncTrackedModel "
                "(and AsyncPartialUpdateMixin) in place of TrackedModel (and PartialUpdateMixin)"
            )

    def save(self, *args, **kwargs):
        """Save and reset tracking."""
        result = super().save(*args, **kwargs)
        self.mark_clean()
        return result

    @classmethod
    def save_many(cls, instances, *args, **kwargs):
        """Save new instances in one batch and reset the tracking of each, as ``save()`` does."""
        result = super().save_many(instances, *args, **kwargs)
        for inst in instances:
            inst.mark_clean()
        return result


class AsyncTrackedModel(_ChangeTracking):
    """Async twin of :class:`TrackedModel`, for ``AsyncSQLerModel``, ``AsyncSQLerSafeModel``
    and their lite twins: ``await model.save()`` resets tracking once the save has run."""

    async def save(self, *args, **kwargs):
        """Save and reset tracking (async)."""
        result = await super().save(*args, **kwargs)
        self.mark_clean()
        return result

    @classmethod
    async def save_many(cls, instances, *args, **kwargs):
        """Save new instances in one batch and reset the tracking of each (async)."""
        result = await super().save_many(instances, *args, **kwargs)
        for inst in instances:
            inst.mark_clean()
        return result


class PartialUpdateMixin(_ModelHost):
    """Mixin that saves only the changed fields of a :class:`TrackedModel`.

    Usage::

        class User(PartialUpdateMixin, TrackedModel, SQLerModel):
            name: str
            email: str
            bio: str  # Large field

        user = User.from_id(1)
        user.name = "New Name"
        user.save_partial()  # Only updates 'name', not 'email' or 'bio'
    """

    def save_partial(self, *, db=None) -> "PartialUpdateMixin":
        """Save only the changed fields, through ``save()``.

        The fields that differ from the row as this model last loaded or saved it are
        written, each as ``save()`` stores it, into the row ``save()`` writes; the row's
        other fields are left as they are. Everything around ``save()`` runs as for
        ``save()`` (the mixins, the version check of a safe model). A model never saved is
        saved in full; with no changed field nothing is sent.

        Args:
            db: Optional database to write to (overrides class-level binding), as for ``save()``.

        Returns:
            Self after save

        Raises:
            NotFoundError: The row is gone: a write of some fields cannot recreate it.
            StaleVersionError: A safe model's row is at another version (as for ``save()``).
        """
        if getattr(self, "_id", None) is None:
            return self.save(db=db)
        if not _changed(self):
            return self
        return self.save(db=db, _partial=True)


class AsyncPartialUpdateMixin(_ModelHost):
    """Async twin of :class:`PartialUpdateMixin`, with :class:`AsyncTrackedModel`."""

    async def save_partial(self, *, db=None) -> "AsyncPartialUpdateMixin":
        """Save only the changed fields (async); see :meth:`PartialUpdateMixin.save_partial`."""
        if getattr(self, "_id", None) is None:
            return await self.save(db=db)
        if not _changed(self):
            return self
        return await self.save(db=db, _partial=True)


def _changed(model: Any) -> Set[str]:
    try:
        return model.changed_fields
    except AttributeError:
        raise TypeError("PartialUpdateMixin requires TrackedModel") from None


class DiffMixin(_ModelHost):
    """Mixin for comparing model instances.

    Usage::

        class User(DiffMixin, SQLerModel):
            name: str
            age: int

        user1 = User(name="Alice", age=30)
        user2 = User(name="Alice", age=31)

        diff = user1.diff(user2)
        # {'age': (30, 31)}
    """

    def diff(self, other: "DiffMixin") -> dict[str, Tuple[Any, Any]]:
        """Compare this instance with another.

        Args:
            other: Another instance to compare with

        Returns:
            Dict of field -> (self_value, other_value) for differences
        """
        if type(self) is not type(other):
            raise TypeError(f"Cannot compare {type(self)} with {type(other)}")

        differences = {}
        for field_name in type(self).model_fields:
            if field_name.startswith("_"):
                continue
            self_val = getattr(self, field_name, None)
            other_val = getattr(other, field_name, None)
            if self_val != other_val:
                differences[field_name] = (self_val, other_val)

        return differences

    def is_equal(self, other: "DiffMixin", *, ignore_id: bool = True) -> bool:
        """Check if two instances have equal field values.

        Args:
            other: Instance to compare
            ignore_id: Ignore _id field in comparison

        Returns:
            True if all compared fields are equal
        """
        if type(self) is not type(other):
            return False

        for field_name in type(self).model_fields:
            if ignore_id and field_name == "_id":
                continue
            if field_name.startswith("_"):
                continue
            if getattr(self, field_name, None) != getattr(other, field_name, None):
                return False

        return True

    def clone(self, **overrides) -> Self:
        """Create a copy of this instance with optional overrides.

        Args:
            **overrides: Field values to override in the clone

        Returns:
            New instance with same values (no _id)
        """
        data = {}
        for field_name in type(self).model_fields:
            if field_name == "_id":
                continue
            data[field_name] = getattr(self, field_name, None)

        data.update(overrides)
        return type(self)(**data)
