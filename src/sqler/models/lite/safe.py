"""SQLerLiteSafeModel - Dataclass model with optimistic locking.

This module provides SQLerLiteSafeModel, a version-controlled model that
uses optimistic locking via a ``_version`` column. It maintains API
compatibility with SQLerSafeModel while being Pydantic-free.
"""

from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import fields
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Optional,
    Type,
    TypeVar,
)

from sqler.exceptions import StaleVersionError
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    apply_numeric_scalar_deltas,
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

    Usage:
        from dataclasses import dataclass
        from sqler import SQLerLiteSafeModel, SQLerDB

        @dataclass
        class Counter(SQLerLiteSafeModel):
            __tablename__ = "counters"
            name: str
            count: int = 0

        db = SQLerDB(":memory:")
        Counter.set_db(db)

        c = Counter(name="views", count=0)
        c.save()  # _version = 0

        # Concurrent update simulation
        c2 = Counter.from_id(c._id)
        c2.count += 1
        c2.save()  # _version = 1

        # Original instance has stale version
        c.count += 1
        c.save()  # Raises StaleVersionError (or rebases if configured)

    Attributes:
        _version: The current version number for optimistic locking.
        _rebase_config: Class-level configuration for intent rebasing.
    """

    # Private version attribute (not a dataclass field)
    _version: int = 0

    # Configurable intent rebasing (can be overridden in subclasses)
    _rebase_config: ClassVar[RebaseConfig] = DEFAULT_REBASE_CONFIG

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
        doc_resolved = cls._resolve_relations(doc)
        inst = cls.model_validate(doc_resolved)
        object.__setattr__(inst, "_id", doc.get("_id"))
        object.__setattr__(inst, "_version", doc.get("_version", 0))
        object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
        return inst

    @classmethod
    def from_ids(cls: Type[TSafe], ids: list[int]) -> list[TSafe]:
        """Hydrate multiple instances by id list with versions.

        Args:
            ids: List of row ids to load.

        Returns:
            List of model instances. Missing ids are omitted.
        """
        if not ids:
            return []
        db, table = cls._require_binding()
        # Use versioned query
        docs = []
        for id_ in ids:
            doc = db.find_document_with_version(table, id_)
            if doc is not None:
                docs.append(doc)
        if not docs:
            return []
        # Batch resolve
        qs = cls.query()
        docs = qs._batch_resolve(docs)
        instances = []
        for doc in docs:
            inst = cls.model_validate(doc)
            object.__setattr__(inst, "_id", doc.get("_id"))
            object.__setattr__(inst, "_version", doc.get("_version", 0))
            object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
            instances.append(inst)
        return instances

    @classmethod
    def query(cls: Type[TSafe]) -> "SQLerQuerySet[TSafe]":
        """Return a queryset that includes version information."""
        db, table = cls._require_binding()
        q = db.query(table).with_version()
        from sqler.models.queryset import SQLerQuerySet

        return SQLerQuerySet[TSafe](cls, q)

    def save(self: TSafe) -> TSafe:
        """Insert or update with optimistic locking and intent rebasing.

        If the save fails due to a stale version (another process updated
        the row), and the changes qualify for rebasing (based on ``_rebase_config``),
        the library will automatically fetch the latest version and reapply
        your changes on top of it.

        Returns:
            Self: The saved instance with updated ``_id`` and ``_version``.

        Raises:
            StaleVersionError: If the version is stale and cannot be rebased.
        """
        cls = self.__class__
        db, table = cls._require_binding()

        # Get the rebase configuration for this model class
        rebase_config = getattr(cls, "_rebase_config", DEFAULT_REBASE_CONFIG)

        # Capture initial intent as numeric scalar deltas from snapshot → target
        snap = self._snapshot
        has_snapshot = isinstance(snap, dict) and len(snap) > 0
        orig = {k: v for k, v in snap.items() if k != "_version"} if has_snapshot else None
        target_payload = self._dump_with_relations()
        delta = compute_numeric_scalar_deltas(orig or {}, target_payload) if has_snapshot else None

        # Use the configurable rebase checker
        can_rebase = has_snapshot and can_rebase_deltas(delta, rebase_config) if delta else False

        max_retries = rebase_config.max_retries if rebase_config else 128
        base = 0.002  # seconds

        for attempt in range(max_retries):
            try:
                attempt_payload = dict(target_payload)
                attempt_payload["_version"] = 0 if self._id is None else self._version + 1
                new_id, new_version = db.upsert_with_version(
                    table, self._id, attempt_payload, self._version
                )
                object.__setattr__(self, "_id", new_id)
                object.__setattr__(self, "_version", new_version)
                target_payload = attempt_payload
                snap_payload = {
                    k: v for k, v in target_payload.items() if k not in {"_id", "_version"}
                }
                object.__setattr__(self, "_snapshot", snap_payload)
                return self
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                # Fall through to backoff
            except StaleVersionError:
                # Stale version: only rebase intent for simple counter deltas
                if not can_rebase:
                    raise
                if self._id is None:
                    raise
                latest = cls.from_id(self._id)
                if latest is None:
                    raise
                latest_payload = latest._dump_with_relations()
                rebased = {**latest_payload}
                # Apply our non-numeric desired fields from current target
                for k, v in target_payload.items():
                    if delta is None or k not in delta:
                        rebased[k] = v
                # Apply numeric deltas on top
                if delta is not None:
                    rebased = apply_numeric_scalar_deltas(rebased, delta)
                target_payload = rebased
                object.__setattr__(self, "_version", getattr(latest, "_version", 0))
                snap_payload = {
                    k: v for k, v in latest_payload.items() if k not in {"_id", "_version"}
                }
                object.__setattr__(self, "_snapshot", snap_payload)

            # Exponential backoff with jitter
            sleep = (base * (2 ** min(attempt, 10))) + (random.random() * 0.0005)
            time.sleep(sleep)

        raise StaleVersionError("save retries exhausted")

    def refresh(self: TSafe) -> TSafe:
        """Reload this instance's fields and version from the database.

        Raises:
            ValueError: If the instance has not been saved.
            LookupError: If the row no longer exists.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._require_binding()
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
        object.__setattr__(self, "_snapshot", cls._make_snapshot(doc))
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
