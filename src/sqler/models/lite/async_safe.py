"""AsyncSQLerLiteSafeModel - Async dataclass model with optimistic locking.

This module provides AsyncSQLerLiteSafeModel, a version-controlled async model
that uses optimistic locking via a ``_version`` column. It maintains API
compatibility with AsyncSQLerSafeModel while being Pydantic-free.
"""

from __future__ import annotations

from dataclasses import fields
from typing import (
    TYPE_CHECKING,
    Awaitable,
    ClassVar,
    TypeVar,
)

from sqler.models import _dirty, _write_rules
from sqler.models import save_retry as _retry
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
)

if TYPE_CHECKING:
    pass

TASafe = TypeVar("TASafe", bound="AsyncSQLerLiteSafeModel")


def _resolve(payload: dict, db, cls) -> Awaitable[dict]:
    """``payload`` with the model of each typed reference in place, read from ``db``."""
    return cls._aresolve_relations(payload, db=db)


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
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and ``db=`` for
        every other call (``await .save(db=db)``, ``Model.from_id(id, db=db)``).

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
            object.__setattr__(inst, "_id", id_)
            object.__setattr__(inst, "_version", version)
            object.__setattr__(inst, "_snapshot", _dirty.snapshot(payload))

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
        return await _retry.arun(self, db, table, _partial, held, _resolve, object.__setattr__)

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
