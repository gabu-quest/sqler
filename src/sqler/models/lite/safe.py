"""SQLerLiteSafeModel - Dataclass model with optimistic locking.

This module provides SQLerLiteSafeModel, a version-controlled model that
uses optimistic locking via a ``_version`` column. It maintains API
compatibility with SQLerSafeModel while being Pydantic-free.
"""

from __future__ import annotations

import sqlite3
from dataclasses import fields
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Optional,
    Type,
    TypeVar,
)

from sqler.exceptions import StaleVersionError
from sqler.models import _dirty, _refs
from sqler.models import save_retry as _retry
from sqler.models._table_names import _instance_binding
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    can_rebase_deltas,
    compute_numeric_scalar_deltas,
)

if TYPE_CHECKING:
    from sqler.db.sqler_db import SQLerDB
    from sqler.models.queryset import SQLerQuerySet

TSafe = TypeVar("TSafe", bound="SQLerLiteSafeModel")


class SQLerLiteSafeModel(SQLerLiteModel):
    """Dataclass model with optimistic locking via a ``_version`` column.

    New rows start at version 0. Updates require the current ``_version`` and
    increment it atomically. Conflicts raise :class:`StaleVersionError`.

    Intent Rebasing:
        For simple counter operations (e.g., incrementing a count field),
        the model supports automatic conflict resolution called "intent rebasing".
        Configure rebasing via the ``_rebase_config`` class variable.

    Usage::

        from dataclasses import dataclass
        from sqler import SQLerLiteSafeModel, SQLerDB

        @dataclass
        class Counter(SQLerLiteSafeModel):
            __tablename__ = "counters"
            name: str
            count: int = 0

        db = SQLerDB(":memory:")
        c = Counter(name="views", count=0)
        c.save(db=db)  # _version = 0

        # Concurrent update simulation
        c2 = Counter.using(db).filter(F("name") == "views").first()
        c2.count += 1
        c2.save(db=db)  # _version = 1

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and
        ``.save(db=db)`` / ``.delete(db=db)`` for writes.

    Attributes:
        _version: The current version number for optimistic locking.
        _rebase_config: Class-level configuration for intent rebasing.
    """

    # Private version attribute (not a dataclass field)
    _version: int = 0

    # Configurable intent rebasing (can be overridden in subclasses)
    _rebase_config: ClassVar[RebaseConfig] = DEFAULT_REBASE_CONFIG
    # from_ids reads each row with its version, as from_id does
    _versioned_rows: ClassVar[bool] = True

    def __post_init__(self) -> None:
        """Initialize private attributes after dataclass __init__."""
        super().__post_init__()
        object.__setattr__(self, "_version", 0)

    @classmethod
    def set_db(cls: Type[TSafe], db: "SQLerDB", table: Optional[str] = None) -> None:
        """Bind the model to a DB and ensure the versioned schema.

        Adds a ``_version`` column to the table if missing.
        """
        super().set_db(db, table)
        # Upgrade table to versioned
        db._ensure_versioned_table(cls._table)  # type: ignore[arg-type]

    @classmethod
    def from_id(cls: Type[TSafe], id_: int) -> Optional[TSafe]:
        """Hydrate an instance by ``_id`` including version.

        Args:
            id_: Row id to load.

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._require_binding()
        doc = db.find_document_with_version(table, id_)
        if doc is None:
            return None
        return _refs.from_row(cls, cls._resolve_relations(doc), doc)

    @classmethod
    def query(cls: Type[TSafe]) -> "SQLerQuerySet[TSafe]":
        """Return a queryset that includes version information."""
        db, table = cls._require_binding()
        q = db.query(table).with_version()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        from sqler.models.queryset import SQLerQuerySet

        return SQLerQuerySet[TSafe](cls, q, db)

    @classmethod
    def save_many(cls: Type[TSafe], instances: list[TSafe], *, db=None) -> list[TSafe]:  # type: ignore[override]
        """Insert multiple new instances with ``_version=0``.

        Uses multi-row INSERT for significantly better throughput. Only
        supports new (unsaved) instances. All instances get ``_version=0``.

        Args:
            instances: List of model instances with ``_id is None``.
            db: Optional database (overrides class-level binding).

        Returns:
            list: The same instances with ``_id`` and ``_version`` populated.

        Raises:
            ValueError: If any instance already has an ``_id``.
        """
        if not instances:
            return instances
        for inst in instances:
            if inst._id is not None:
                raise ValueError("save_many() only accepts new instances (_id must be None)")

        db, table = cls._resolve_binding(db)
        promoted = getattr(cls, "__promoted__", None)

        if promoted:
            checks = getattr(cls, "__checks__", None)
            db._ensure_promoted(table, promoted, checks, model=cls)
        db._ensure_versioned_table(table)

        payloads = [inst._dump_with_relations() for inst in instances]

        if promoted:
            promoted_fields = list(promoted.keys())
            id_version_pairs = db.insert_many_with_version_promoted(table, payloads, promoted_fields)
        else:
            id_version_pairs = db.insert_many_with_version(table, payloads)

        for inst, (id_, version), payload in zip(instances, id_version_pairs, payloads):
            object.__setattr__(inst, "_id", id_)
            object.__setattr__(inst, "_version", version)
            object.__setattr__(inst, "_snapshot", _dirty.snapshot(payload))
        return instances

    def save(self: TSafe, *, db=None, _partial: bool = False) -> TSafe:
        """Insert or update with optimistic locking and intent rebasing.

        If the save fails due to a stale version (another process updated
        the row), and the changes qualify for rebasing (based on ``_rebase_config``),
        the library will automatically fetch the latest version and reapply
        your changes on top of it.

        Args:
            db: Optional database to write to (overrides class-level binding).
            _partial: Write only the fields that differ from the row as this model last
                loaded or saved it, and leave the row's other fields as they are
                (``save_partial()``). The row must exist.

        Returns:
            Self: The saved instance with updated ``_id`` and ``_version``.

        Raises:
            StaleVersionError: If the version is stale and cannot be rebased.
        """
        cls = self.__class__
        db, table = _instance_binding(self, db)

        # Get the rebase configuration for this model class
        rebase_config = getattr(cls, "_rebase_config", DEFAULT_REBASE_CONFIG)

        # The caller's intent: the counter deltas from its snapshot to its payload
        snap = self._snapshot
        held = self._dump_with_relations()
        delta = compute_numeric_scalar_deltas(snap, held) if snap else None
        can_rebase = bool(delta) and can_rebase_deltas(delta, rebase_config)

        retry = _retry.SaveRetry(rebase_config)

        payload, written = held, None
        while True:
            retry.begin_try()
            try:
                attempt_payload = dict(payload)
                attempt_payload["_version"] = 0 if self._id is None else self._version + 1
                promoted = getattr(cls, "__promoted__", None)
                if promoted:
                    db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
                if _partial:
                    changed = _dirty.changed_fields(self._snapshot or {}, attempt_payload)
                    new_id, new_version = self._id, db._update_fields(
                        table, self._id, attempt_payload, changed, promoted or (), self._version
                    )
                elif promoted:
                    new_id, new_version = db.upsert_with_version_promoted(
                        table, self._id, attempt_payload, self._version,
                        list(promoted.keys()),
                    )
                else:
                    new_id, new_version = db.upsert_with_version(
                        table, self._id, attempt_payload, self._version
                    )
                object.__setattr__(self, "_id", new_id)
                object.__setattr__(self, "_version", new_version)
                object.__setattr__(self, "_snapshot", _dirty.snapshot(attempt_payload))
                if written is not None:  # rebased: the row holds the other writer's work too
                    _retry.settle(self, held, written)
                return self
            except sqlite3.OperationalError as e:
                if not _retry.is_lock_error(e):
                    raise
                delay = retry.after_lock(e)
            except StaleVersionError as stale:
                # Stale version: only rebase intent for simple counter deltas
                if not can_rebase:
                    raise
                if self._id is None:
                    raise
                delay = retry.after_conflict()
                latest_doc = retry.reread(lambda: db.find_document_with_version(table, self._id))
                if latest_doc is None:
                    raise
                payload = _retry.rebase(latest_doc, held, snap, delta)
                written = retry.build(stale, lambda: cls.model_validate(cls._resolve_relations(payload, db=db)))
                object.__setattr__(self, "_version", latest_doc.get("_version", 0))
                object.__setattr__(self, "_snapshot", _dirty.snapshot(latest_doc))

            _retry.sleep(delay)

    def refresh(self: TSafe) -> TSafe:
        """Reload this instance's fields and version from the database.

        Raises:
            ValueError: If the instance has not been saved.
            LookupError: If the row no longer exists.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = _instance_binding(self)
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = db.find_document_with_version(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        doc_resolved = cls._resolve_relations(doc)
        # Update fields in-place
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            if f.name in doc_resolved:
                object.__setattr__(self, f.name, doc_resolved[f.name])
        object.__setattr__(self, "_id", doc.get("_id"))
        object.__setattr__(self, "_version", doc.get("_version", 0))
        object.__setattr__(self, "_snapshot", _dirty.snapshot(doc))
        return self

    def __repr__(self) -> str:
        """Include _version in repr."""
        cls_name = self.__class__.__name__
        field_strs = []
        # Include _id and _version if set
        if self._id is not None:
            field_strs.append(f"_id={self._id}")
        field_strs.append(f"_version={self._version}")
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            field_strs.append(f"{f.name}={value!r}")
        return f"{cls_name}({', '.join(field_strs)})"
