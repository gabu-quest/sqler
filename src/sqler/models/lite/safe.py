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
from sqler.models import _dirty
from sqler.models import save_retry as _retry
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    can_rebase_deltas,
    compute_numeric_scalar_deltas,
)

if TYPE_CHECKING:
    from sqler.db.sqler_db import SQLerDB

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
    def _insert_rows(cls, db, table: str, instances: list) -> None:
        """Write new instances in one batch at ``_version=0``; each takes its ``_id``,
        ``_version`` and snapshot."""
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

    def _write(self, db, table: str, _partial: bool):
        """Write this instance's row with optimistic locking and intent rebasing (``save()``).

        If the save fails due to a stale version (another process updated
        the row), and the changes qualify for rebasing (based on ``_rebase_config``),
        the library will automatically fetch the latest version and reapply
        your changes on top of it.

        Raises:
            StaleVersionError: If the version is stale and cannot be rebased.
        """
        cls = self.__class__

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
                promoted = getattr(cls, "__promoted__", None)
                if promoted:
                    db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
                if _partial:
                    changed = _dirty.changed_fields(self._snapshot or {}, payload)
                    new_id, new_version = self._id, db._update_fields(
                        table, self._id, payload, changed, promoted or (), self._version
                    )
                elif promoted:
                    new_id, new_version = db.upsert_with_version_promoted(
                        table, self._id, payload, self._version,
                        list(promoted.keys()),
                    )
                else:
                    new_id, new_version = db.upsert_with_version(
                        table, self._id, payload, self._version
                    )
                object.__setattr__(self, "_id", new_id)
                object.__setattr__(self, "_version", new_version)
                object.__setattr__(self, "_snapshot", _dirty.snapshot(payload))
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

    def _refreshed(self: TSafe, doc: dict, row: dict) -> TSafe:
        super()._refreshed(doc, row)
        object.__setattr__(self, "_id", doc.get("_id"))
        object.__setattr__(self, "_version", doc.get("_version", 0))
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
