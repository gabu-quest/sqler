from __future__ import annotations

import random
import sqlite3
import time
from typing import ClassVar, Optional, Type, TypeVar

from pydantic import PrivateAttr

from sqler.exceptions import StaleVersionError

from .model import SQLerModel
from .queryset import SQLerQuerySet
from .utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    apply_numeric_scalar_deltas,
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

    @classmethod
    def set_db(cls: Type[TSafe], db, table: Optional[str] = None) -> None:  # type: ignore[override]
        """Bind the model to a DB and ensure the versioned schema.

        Adds a ``_version`` column to the table if missing.
        """
        super().set_db(db, table)
        # upgrade table to versioned
        db._ensure_versioned_table(cls._table)  # type: ignore[arg-type]

    @classmethod
    def from_id(cls: Type[TSafe], id_: int) -> Optional[TSafe]:  # type: ignore[override]
        db, table = cls._require_binding()
        doc = db.find_document_with_version(table, id_)
        if doc is None:
            return None
        inst = cls.model_validate(doc)  # type: ignore[call-arg]
        inst._id = doc.get("_id")  # type: ignore[attr-defined]
        inst._version = doc.get("_version", 0)  # type: ignore[attr-defined]
        return inst  # type: ignore[return-value]

    @classmethod
    def query(cls: Type[TSafe]) -> SQLerQuerySet[TSafe]:  # type: ignore[override]
        db, table = cls._require_binding()
        q = db.query(table).with_version()
        return SQLerQuerySet[TSafe](cls, q)

    def save(self: TSafe) -> TSafe:  # type: ignore[override]
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
        snap = getattr(self, "_snapshot", None)
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
                self._id = new_id
                self._version = new_version
                target_payload = attempt_payload
                try:
                    snap_payload = {
                        k: v for k, v in target_payload.items() if k not in {"_id", "_version"}
                    }
                    self._snapshot = snap_payload  # type: ignore[attr-defined]
                except Exception:
                    pass
                return self
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                # fallthrough to backoff
            except RuntimeError as e:
                # Stale version: only rebase intent for simple counter deltas
                if not can_rebase:
                    raise StaleVersionError(str(e)) from e
                if self._id is None:
                    raise StaleVersionError(str(e)) from e
                latest = cls.from_id(self._id)
                if latest is None:
                    raise StaleVersionError(str(e)) from e
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
                self._version = getattr(latest, "_version", 0)
                try:
                    snap_payload = {
                        k: v for k, v in latest_payload.items() if k not in {"_id", "_version"}
                    }
                    self._snapshot = snap_payload  # type: ignore[attr-defined]
                except Exception:
                    pass

            # Exponential backoff with jitter
            sleep = (base * (2 ** min(attempt, 10))) + (random.random() * 0.0005)
            time.sleep(sleep)

        raise StaleVersionError("save retries exhausted")

    def refresh(self: TSafe) -> TSafe:  # type: ignore[override]
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = db.find_document_with_version(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        fresh = cls.model_validate(doc)  # type: ignore[call-arg]
        for fname in self.__class__.model_fields:
            if fname == "_id":
                continue
            setattr(self, fname, getattr(fresh, fname))
        self._id = doc.get("_id")
        self._version = doc.get("_version", 0)
        try:
            self._snapshot = {k: v for k, v in doc.items() if k not in {"_id", "_version"}}  # type: ignore[attr-defined]
        except Exception:
            pass
        return self
