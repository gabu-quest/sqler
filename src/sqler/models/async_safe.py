from __future__ import annotations

from typing import Awaitable, ClassVar, Optional, TypeVar

from pydantic import PrivateAttr

from sqler.models import _dirty, _refs, _write_rules

from . import save_retry as _retry
from .async_model import AsyncSQLerModel
from .utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
)

TASafe = TypeVar("TASafe", bound="AsyncSQLerSafeModel")


def _resolve(payload: dict, db, cls) -> Awaitable[dict]:
    """``payload`` with the model of each typed reference in place, read from ``db``."""
    return _refs.aresolve(payload, db, frozenset(), cls)


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
            await _write_rules.ensure_columns(cls, db, table, promoted)
        await db._ensure_versioned_table(table)
        payloads = [await inst._adump_with_relations() for inst in instances]
        insert, args = _write_rules.rows_insert(db, table, payloads, promoted, versioned=True)
        id_version_pairs = await insert(*args)

        for inst, (id_, version), payload in zip(instances, id_version_pairs, payloads):
            inst._id = id_
            inst._version = version
            inst._snapshot = _dirty.snapshot(payload)

    async def _write(self, db, table: str, _partial: bool):
        """Write this instance's row with optimistic locking and intent rebasing (``save()``).

        If the save fails due to a stale version (another process updated
        the row), and the changes qualify for rebasing (based on ``_rebase_config``),
        the library will automatically fetch the latest version and reapply
        your changes on top of it. The steps and their order are
        :func:`sqler.models.save_retry.arun`.

        Raises:
            StaleVersionError: If the version is stale and cannot be rebased.
        """
        held = await self._adump_with_relations()
        return await _retry.arun(self, db, table, _partial, held, _resolve, setattr)

    def _refreshed(self: TASafe, doc: dict, row: dict) -> TASafe:
        super()._refreshed(doc, row)
        self._version = doc.get("_version", 0)
        return self
