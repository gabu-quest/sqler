"""AsyncSQLerLiteSafeModel - Async dataclass model with optimistic locking.

This module provides AsyncSQLerLiteSafeModel, a version-controlled async model
that uses optimistic locking via a ``_version`` column. It maintains API
compatibility with AsyncSQLerSafeModel while being Pydantic-free.
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
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    can_rebase_deltas,
    compute_numeric_scalar_deltas,
)

if TYPE_CHECKING:
    from sqler.models.async_queryset import AsyncSQLerQuerySet

TASafe = TypeVar("TASafe", bound="AsyncSQLerLiteSafeModel")


class AsyncSQLerLiteSafeModel(AsyncSQLerLiteModel):
    """Async dataclass model with optimistic locking via ``_version`` column.

    New rows start at version 0. Updates require the current ``_version`` and
    increment it atomically. Conflicts raise :class:`StaleVersionError`.

    Intent Rebasing:
        For simple counter operations, the model supports automatic conflict
        resolution called "intent rebasing". Configure via ``_rebase_config``.

    Usage::

        from dataclasses import dataclass
        from sqler import AsyncSQLerLiteSafeModel, AsyncSQLerDB

        @dataclass
        class Counter(AsyncSQLerLiteSafeModel):
            __tablename__ = "counters"
            name: str
            count: int = 0

        db = AsyncSQLerDB(":memory:")
        await db.connect()
        c = Counter(name="views", count=0)
        await c.save(db=db)  # _version = 0

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and
        ``await .save(db=db)`` / ``await .delete(db=db)`` for writes.

    Attributes:
        _version: The current version number for optimistic locking.
        _rebase_config: Class-level configuration for intent rebasing.
    """

    # Private version attribute (not a dataclass field)
    _version: int = 0

    # Configurable intent rebasing
    _rebase_config: ClassVar[RebaseConfig] = DEFAULT_REBASE_CONFIG
    # from_ids reads each row with its version, as from_id does
    _versioned_rows: ClassVar[bool] = True

    def __post_init__(self) -> None:
        """Initialize private attributes after dataclass __init__."""
        super().__post_init__()
        object.__setattr__(self, "_version", 0)

    @classmethod
    def query(cls: Type[TASafe]) -> "AsyncSQLerQuerySet[TASafe]":
        """Return an async queryset that includes version information."""
        from sqler.models.async_queryset import AsyncSQLerQuerySet
        from sqler.query.async_query import AsyncSQLerQuery

        db, table = cls._require_binding()
        q = AsyncSQLerQuery(table=table, adapter=db.adapter).with_version()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        return AsyncSQLerQuerySet[TASafe](cls, q, db)

    @classmethod
    async def from_id(cls: Type[TASafe], id_: int) -> Optional[TASafe]:
        """Hydrate an instance by ``_id`` including version.

        Args:
            id_: Row id to load.

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._require_binding()
        doc = await db.find_document_with_version(table, id_)
        if doc is None:
            return None
        return (await _refs.arun(_refs.load(cls, [doc], db, table)))[0]

    @classmethod
    async def _insert_rows(cls, db, table: str, instances: list) -> None:
        """Write new instances in one batch at ``_version=0``; each takes its ``_id``,
        ``_version`` and snapshot."""
        promoted = getattr(cls, "__promoted__", None)

        if promoted:
            checks = getattr(cls, "__checks__", None)
            await db._ensure_promoted(table, promoted, checks, model=cls)
        await db._ensure_versioned_table(table)

        payloads = [await inst._adump_with_relations() for inst in instances]

        if promoted:
            promoted_fields = list(promoted.keys())
            id_version_pairs = await db.insert_many_with_version_promoted(table, payloads, promoted_fields)
        else:
            id_version_pairs = await db.insert_many_with_version(table, payloads)

        for inst, (id_, version), payload in zip(instances, id_version_pairs, payloads):
            object.__setattr__(inst, "_id", id_)
            object.__setattr__(inst, "_version", version)
            object.__setattr__(inst, "_snapshot", _dirty.snapshot(payload))

    async def _write(self, db, table: str, _partial: bool):
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
        held = await self._adump_with_relations()
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
                    await db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
                if _partial:
                    changed = _dirty.changed_fields(self._snapshot or {}, attempt_payload)
                    new_id, new_version = self._id, await db._update_fields(
                        table, self._id, attempt_payload, changed, promoted or (), self._version
                    )
                elif promoted:
                    new_id, new_version = await db.upsert_with_version_promoted(
                        table, self._id, attempt_payload, self._version,
                        list(promoted.keys()),
                    )
                else:
                    new_id, new_version = await db.upsert_with_version(
                        table, self._id, attempt_payload, self._version
                    )
                object.__setattr__(self, "_id", new_id)
                object.__setattr__(self, "_version", new_version)
                object.__setattr__(self, "_snapshot", _dirty.snapshot(attempt_payload))
                if written is not None:  # rebased: the row holds the other writer's work too
                    _retry.settle(self, held, written)
                return self
            except StaleVersionError as stale:
                if not can_rebase:
                    raise
                if self._id is None:
                    raise
                delay = retry.after_conflict()
                latest_doc = await retry.areread(lambda: db.find_document_with_version(table, self._id))
                if latest_doc is None:
                    raise
                payload = _retry.rebase(latest_doc, held, snap, delta)
                async def build():
                    return cls.model_validate(await cls._aresolve_relations(payload, db=db))

                written = await retry.abuild(stale, build)
                object.__setattr__(self, "_version", latest_doc.get("_version", 0))
                object.__setattr__(self, "_snapshot", _dirty.snapshot(latest_doc))
            except sqlite3.OperationalError as e:
                if not _retry.is_lock_error(e):
                    raise
                delay = retry.after_lock(e)
            await _retry.asleep(delay)

    async def refresh(self: TASafe) -> TASafe:
        """Reload this instance's fields and version from the database.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = _instance_binding(self)
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = await db.find_document_with_version(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        own = {(table, self._id): self}
        return (await _refs.arun(_refs.load(cls, [doc], db, table, self._refreshed, own)))[0]

    def _refreshed(self: TASafe, doc: dict, row: dict) -> TASafe:
        super()._refreshed(doc, row)
        object.__setattr__(self, "_id", doc.get("_id"))
        object.__setattr__(self, "_version", doc.get("_version", 0))
        return self

    def __repr__(self) -> str:
        """Include _version in repr."""
        cls_name = self.__class__.__name__
        field_strs = []
        if self._id is not None:
            field_strs.append(f"_id={self._id}")
        field_strs.append(f"_version={self._version}")
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            field_strs.append(f"{f.name}={value!r}")
        return f"{cls_name}({', '.join(field_strs)})"
