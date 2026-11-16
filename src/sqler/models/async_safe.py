from __future__ import annotations

import sqlite3
from typing import ClassVar, Optional, TypeVar

from pydantic import PrivateAttr

from sqler.exceptions import StaleVersionError
from sqler.models import _dirty, _refs

from . import save_retry as _retry
from .async_model import AsyncSQLerModel
from .utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
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
    # from_ids reads each row with its version, as from_id does
    _versioned_rows: ClassVar[bool] = True

    @classmethod
    def set_db(cls, db, table: Optional[str] = None) -> None:  # type: ignore[override]
        super().set_db(db, table)
        # ensure versioned schema
        # db is AsyncSQLerDB
        # we cannot await here; users should ensure schema via an async helper, or we
        # leave it to first save/refresh paths which call versioned helpers
        # For explicitness, no-op here; version checks happen on use.

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
            inst._id = id_
            inst._version = version
            inst._snapshot = _dirty.snapshot(payload)

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
        snap = getattr(self, "_snapshot", None)
        held = await self._adump_with_relations()
        delta = compute_numeric_scalar_deltas(snap, held) if snap else None
        can_rebase = bool(delta) and can_rebase_deltas(delta, rebase_config)

        retry = _retry.SaveRetry(rebase_config)

        payload, written = held, None
        version, base = self._version, snap  # the model keeps its own until a write succeeds
        while True:
            retry.begin_try()
            try:
                promoted = getattr(cls, "__promoted__", None)
                if promoted:
                    await db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
                if _partial:
                    changed = _dirty.changed_fields(base or {}, payload)
                    new_id, new_version = self._id, await db._update_fields(
                        table, self._id, payload, changed, promoted or (), version
                    )
                elif promoted:
                    new_id, new_version = await db.upsert_with_version_promoted(
                        table, self._id, payload, version,
                        list(promoted.keys()),
                    )
                else:
                    new_id, new_version = await db.upsert_with_version(
                        table, self._id, payload, version
                    )
                self._id = new_id
                self._version = new_version
                self._snapshot = _dirty.snapshot(payload)
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
                payload = _retry.rebase(latest_doc, held, snap, delta, stale)
                async def build():
                    return cls.model_validate(await _refs.aresolve(payload, db, frozenset(), cls))

                written = await retry.abuild(stale, build)
                version, base = latest_doc.get("_version", 0), _dirty.snapshot(latest_doc)
            except sqlite3.OperationalError as e:
                if not _retry.is_lock_error(e):
                    raise
                delay = retry.after_lock(e)
            await _retry.asleep(delay)

    def _refreshed(self: TASafe, doc: dict, row: dict) -> TASafe:
        super()._refreshed(doc, row)
        self._version = doc.get("_version", 0)
        return self
