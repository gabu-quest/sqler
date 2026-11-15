from __future__ import annotations

import sqlite3
from typing import ClassVar, Optional, Type, TypeVar

from pydantic import PrivateAttr

from sqler.exceptions import StaleVersionError
from sqler.models import _dirty, _refs

from . import save_retry as _retry
from .model import SQLerModel
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
            inst._id = id_
            inst._version = version
            inst._snapshot = _dirty.snapshot(payload)

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

    def _refreshed(self: TSafe, doc: dict, row: dict) -> TSafe:
        super()._refreshed(doc, row)
        self._version = doc.get("_version", 0)
        return self
