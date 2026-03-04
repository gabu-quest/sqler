"""AsyncSQLerLiteSafeModel - Async dataclass model with optimistic locking.

This module provides AsyncSQLerLiteSafeModel, a version-controlled async model
that uses optimistic locking via a ``_version`` column. It maintains API
compatibility with AsyncSQLerSafeModel while being Pydantic-free.
"""

from __future__ import annotations

import asyncio
import random
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
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    apply_numeric_scalar_deltas,
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

    Usage:
        from dataclasses import dataclass
        from sqler import AsyncSQLerLiteSafeModel, AsyncSQLerDB

        @dataclass
        class Counter(AsyncSQLerLiteSafeModel):
            __tablename__ = "counters"
            name: str
            count: int = 0

        db = AsyncSQLerDB(":memory:")
        await db.connect()
        Counter.set_db(db)

        c = Counter(name="views", count=0)
        await c.save()  # _version = 0

    Attributes:
        _version: The current version number for optimistic locking.
        _rebase_config: Class-level configuration for intent rebasing.
    """

    # Private version attribute (not a dataclass field)
    _version: int = 0

    # Configurable intent rebasing
    _rebase_config: ClassVar[RebaseConfig] = DEFAULT_REBASE_CONFIG

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
        return AsyncSQLerQuerySet[TASafe](cls, q)

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
        doc_resolved = await cls._aresolve_relations(doc)
        inst = cls.model_validate(doc_resolved)
        object.__setattr__(inst, "_id", doc.get("_id"))
        object.__setattr__(inst, "_version", doc.get("_version", 0))
        object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
        return inst

    @classmethod
    async def from_ids(cls: Type[TASafe], ids: list[int]) -> list[TASafe]:
        """Hydrate multiple instances by id list with versions.

        Args:
            ids: List of row ids to load.

        Returns:
            List of model instances. Missing ids are omitted.
        """
        if not ids:
            return []
        db, table = cls._require_binding()
        # Use versioned query for each
        docs = []
        for id_ in ids:
            doc = await db.find_document_with_version(table, id_)
            if doc is not None:
                docs.append(doc)
        if not docs:
            return []
        # Batch resolve
        qs = cls.query()
        docs = await qs._abatch_resolve(docs)
        instances = []
        for doc in docs:
            inst = cls.model_validate(doc)
            object.__setattr__(inst, "_id", doc.get("_id"))
            object.__setattr__(inst, "_version", doc.get("_version", 0))
            object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
            instances.append(inst)
        return instances

    @classmethod
    async def asave_many(cls: Type[TASafe], instances: list[TASafe], *, db=None) -> list[TASafe]:  # type: ignore[override]
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
                raise ValueError("asave_many() only accepts new instances (_id must be None)")

        db, table = cls._resolve_binding(db)
        promoted = getattr(cls, "__promoted__", None)

        if promoted and table not in db._promoted_columns:
            checks = getattr(cls, "__checks__", None)
            await db._ensure_table_with_promoted(table, promoted, checks)
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
            object.__setattr__(inst, "_snapshot", {k: v for k, v in payload.items() if k not in {"_id", "_version"}})
        return instances

    async def save(self: TASafe, *, db=None) -> TASafe:
        """Insert or update with optimistic locking and intent rebasing.

        If the save fails due to a stale version (another process updated
        the row), and the changes qualify for rebasing (based on ``_rebase_config``),
        the library will automatically fetch the latest version and reapply
        your changes on top of it.

        Args:
            db: Optional database to write to (overrides class-level binding).

        Returns:
            Self: The saved instance with updated ``_id`` and ``_version``.

        Raises:
            StaleVersionError: If the version is stale and cannot be rebased.
        """
        cls = self.__class__
        db, table = cls._resolve_binding(db)

        # Get the rebase configuration for this model class
        rebase_config = getattr(cls, "_rebase_config", DEFAULT_REBASE_CONFIG)

        # Capture initial intent as numeric scalar deltas from snapshot → target
        snap = self._snapshot
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
                promoted = getattr(cls, "__promoted__", None)
                if promoted:
                    if table not in db._promoted_columns:
                        checks = getattr(cls, "__checks__", None)
                        await db._ensure_table_with_promoted(table, promoted, checks)
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
                target_payload = attempt_payload
                snap_payload = {
                    k: v for k, v in target_payload.items() if k not in {"_id", "_version"}
                }
                object.__setattr__(self, "_snapshot", snap_payload)
                return self
            except StaleVersionError:
                if not can_rebase:
                    raise
                if self._id is None:
                    raise
                latest_doc = await db.find_document_with_version(table, self._id)
                if latest_doc is None:
                    raise
                latest_payload = {k: v for k, v in latest_doc.items() if k not in ("_id", "_version")}
                rebased = {**latest_payload}
                for k, v in target_payload.items():
                    if delta is None or k not in delta:
                        rebased[k] = v
                if delta is not None:
                    rebased = apply_numeric_scalar_deltas(rebased, delta)
                target_payload = rebased
                object.__setattr__(self, "_version", latest_doc.get("_version", 0))
                object.__setattr__(self, "_snapshot", latest_payload.copy())
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
            # Backoff
            await asyncio.sleep((base * (2 ** min(attempt, 10))) + (random.random() * 0.0005))

        raise StaleVersionError("save retries exhausted")

    async def refresh(self: TASafe) -> TASafe:
        """Reload this instance's fields and version from the database.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = await db.find_document_with_version(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        doc_resolved = await cls._aresolve_relations(doc)
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
        if self._id is not None:
            field_strs.append(f"_id={self._id}")
        field_strs.append(f"_version={self._version}")
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            field_strs.append(f"{f.name}={value!r}")
        return f"{cls_name}({', '.join(field_strs)})"
