from __future__ import annotations

import asyncio
import random
import sqlite3
from typing import ClassVar, Optional, Type, TypeVar

from pydantic import PrivateAttr

from sqler.exceptions import StaleVersionError
from sqler.query.async_query import AsyncSQLerQuery

from .async_model import AsyncSQLerModel
from .async_queryset import AsyncSQLerQuerySet
from .utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    apply_numeric_scalar_deltas,
    can_rebase_deltas,
    compute_numeric_scalar_deltas,
)

TASafe = TypeVar("TASafe", bound="AsyncSQLerSafeModel")


class AsyncSQLerSafeModel(AsyncSQLerModel):
    """Async model with optimistic locking via ``_version`` column.

    New rows start at version 0. Updates require the current ``_version`` and
    increment it atomically. Conflicts raise :class:`StaleVersionError`.

    Intent Rebasing:
        For simple counter operations (e.g., incrementing a count field),
        the model supports automatic conflict resolution called "intent rebasing".
        When a save fails due to a stale version, instead of raising an error,
        the library can rebase your intent onto the latest version.

        Configure rebasing via the ``_rebase_config`` class variable::

            class Counter(AsyncSQLerSafeModel):
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
    _snapshot: Optional[dict] = PrivateAttr(default=None)

    # Configurable intent rebasing (can be overridden in subclasses)
    _rebase_config: ClassVar[RebaseConfig] = DEFAULT_REBASE_CONFIG

    @classmethod
    def query(cls: Type[TASafe]) -> AsyncSQLerQuerySet[TASafe]:  # type: ignore[override]
        db, table = cls._require_binding()
        q = AsyncSQLerQuery(table=table, adapter=db.adapter).with_version()
        return AsyncSQLerQuerySet[TASafe](cls, q)

    @classmethod
    def set_db(cls, db, table: Optional[str] = None) -> None:  # type: ignore[override]
        super().set_db(db, table)
        # ensure versioned schema
        # db is AsyncSQLerDB
        # we cannot await here; users should ensure schema via an async helper, or we
        # leave it to first save/refresh paths which call versioned helpers
        # For explicitness, no-op here; version checks happen on use.

    @classmethod
    async def from_id(cls: Type[TASafe], id_: int) -> Optional[TASafe]:  # type: ignore[override]
        db, table = cls._require_binding()
        doc = await db.find_document_with_version(table, id_)
        if doc is None:
            return None
        inst = cls.model_validate(doc)  # type: ignore[call-arg]
        inst._id = doc.get("_id")
        inst._version = doc.get("_version", 0)
        try:
            inst._snapshot = {k: v for k, v in doc.items() if k not in {"_id", "_version"}}  # type: ignore[attr-defined]
        except Exception:
            pass
        return inst  # type: ignore[return-value]

    async def save(self: TASafe) -> TASafe:  # type: ignore[override]
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
        target_payload = await self._adump_with_relations()
        delta = compute_numeric_scalar_deltas(orig or {}, target_payload) if has_snapshot else None

        # Use the configurable rebase checker
        can_rebase = has_snapshot and can_rebase_deltas(delta, rebase_config) if delta else False

        max_retries = rebase_config.max_retries if rebase_config else 128
        base = 0.002  # seconds

        for attempt in range(max_retries):
            try:
                attempt_payload = dict(target_payload)
                attempt_payload["_version"] = 0 if self._id is None else self._version + 1
                new_id, new_version = await db.upsert_with_version(
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
            except RuntimeError as e:
                if not can_rebase:
                    raise StaleVersionError(str(e)) from e
                if self._id is None:
                    raise StaleVersionError(str(e)) from e
                latest = await cls.from_id(self._id)
                if latest is None:
                    raise StaleVersionError(str(e)) from e
                latest_payload = latest.model_dump(exclude={"_id"})
                rebased = {**latest_payload}
                for k, v in target_payload.items():
                    if delta is None or k not in delta:
                        rebased[k] = v
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
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
            # backoff
            await asyncio.sleep((base * (2 ** min(attempt, 10))) + (random.random() * 0.0005))

        raise StaleVersionError("save retries exhausted")

    async def refresh(self: TASafe) -> TASafe:  # type: ignore[override]
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = await db.find_document_with_version(table, self._id)
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
