from __future__ import annotations

import sqlite3
from typing import ClassVar, Optional, Type, TypeVar

from pydantic import PrivateAttr

from sqler.exceptions import StaleVersionError
from sqler.models import _dirty, _refs
from sqler.models._table_names import _instance_binding

from . import save_retry as _retry
from .model import SQLerModel
from .queryset import SQLerQuerySet
from .utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    can_rebase_deltas,
    compute_numeric_scalar_deltas,
)

TSafe = TypeVar("TSafe", bound="SQLerSafeModel")


class SQLerSafeModel(SQLerModel):
    """Model with optimistic locking via a ``_version`` column.

    New rows start at version 0. Updates require the current ``_version`` and
    increment it atomically. Conflicts raise :class:`StaleVersionError`.

    Intent Rebasing:
        For simple counter operations (e.g., incrementing a count field),
        the model supports automatic conflict resolution called "intent rebasing".
        When a save fails due to a stale version, instead of raising an error,
        the library can rebase your intent onto the latest version.

        Configure rebasing via the ``_rebase_config`` class variable::

            class Counter(SQLerSafeModel):
                count: int = 0

                # Allow rebasing for 'count' and 'views' fields
                _rebase_config = RebaseConfig(
                    enabled=True,
                    allowed_fields={"count", "views"},
                    max_delta=5,  # Allow ±5 increments
                )

    Attributes:
        _version: The current version number for optimistic locking.
        _rebase_config: Class-level configuration for intent rebasing.
    """

    _version: int = PrivateAttr(default=0)

    # Configurable intent rebasing (can be overridden in subclasses)
    _rebase_config: ClassVar[RebaseConfig] = DEFAULT_REBASE_CONFIG
    # from_ids reads each row with its version, as from_id does
    _versioned_rows: ClassVar[bool] = True

    @classmethod
    def set_db(cls: Type[TSafe], db, table: Optional[str] = None) -> None:  # type: ignore[override]
        """Bind the model to a DB and ensure the versioned schema.

        Adds a ``_version`` column to the table if missing.
        """
        super().set_db(db, table)
        # upgrade table to versioned
        db._ensure_versioned_table(cls._table)  # type: ignore[arg-type]

    @classmethod
    def _load_refs(cls: Type[TSafe], ids: list, seen: frozenset) -> dict:  # type: ignore[override]
        """Load the rows ``ids`` with their versions and snapshots, one statement."""
        db, table = cls._require_binding()
        out: dict = {}
        for doc in _refs.read_versioned(db, table, ids):
            rid = doc["_id"]
            try:
                typed = _refs.resolve(doc, db, seen | {(table, rid)}, cls)
                inst = _refs.from_row(cls, typed, doc)
            except _refs.UNLOADABLE:
                out[rid] = _refs.KEEP
                continue
            out[rid] = inst
        return out

    @classmethod
    def from_id(cls: Type[TSafe], id_: int) -> Optional[TSafe]:  # type: ignore[override]
        db, table = cls._require_binding()
        doc = db.find_document_with_version(table, id_)
        if doc is None:
            return None
        typed = _refs.resolve(doc, db, frozenset(), cls)
        return _refs.from_row(cls, typed, doc)  # type: ignore[return-value]

    @classmethod
    def query(cls: Type[TSafe]) -> SQLerQuerySet[TSafe]:  # type: ignore[override]
        db, table = cls._require_binding()
        q = db.query(table).with_version()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
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
            db._ensure_promoted(table, promoted, checks)
        db._ensure_versioned_table(table)

        payloads = [inst._dump_with_relations() for inst in instances]

        if promoted:
            promoted_fields = list(promoted.keys())
            id_version_pairs = db.insert_many_with_version_promoted(table, payloads, promoted_fields)
        else:
            id_version_pairs = db.insert_many_with_version(table, payloads)

        for inst, (id_, version), payload in zip(instances, id_version_pairs, payloads):
            inst._id = id_
            inst._version = version
            inst._snapshot = _dirty.snapshot(payload)
        return instances

    def save(self: TSafe, *, db=None, _partial: bool = False) -> TSafe:  # type: ignore[override]
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
        snap = getattr(self, "_snapshot", None)
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
                    db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None))
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
                self._id = new_id
                self._version = new_version
                self._snapshot = _dirty.snapshot(attempt_payload)
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
                written = retry.build(
                    stale, lambda: cls.model_validate(_refs.resolve(payload, db, frozenset(), cls))
                )
                self._version = latest_doc.get("_version", 0)
                self._snapshot = _dirty.snapshot(latest_doc)

            _retry.sleep(delay)

    def refresh(self: TSafe) -> TSafe:  # type: ignore[override]
        cls = self.__class__
        db, table = _instance_binding(self)
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = db.find_document_with_version(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        row, doc = doc, _refs.resolve(doc, db, frozenset(), cls)
        fresh = cls.model_validate(doc)  # type: ignore[call-arg]
        for fname in self.__class__.model_fields:
            if fname == "_id":
                continue
            setattr(self, fname, getattr(fresh, fname))
        self._id = doc.get("_id")
        self._version = doc.get("_version", 0)
        self._snapshot = _dirty.snapshot(row)
        return self
