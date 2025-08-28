from __future__ import annotations

import asyncio
import random
import sqlite3
from typing import Optional, Type, TypeVar

from pydantic import PrivateAttr

from sqler.query.async_query import AsyncSQLerQuery

from .async_model import AsyncSQLerModel
from .async_queryset import AsyncSQLerQuerySet

TASafe = TypeVar("TASafe", bound="AsyncSQLerSafeModel")


class AsyncSQLerSafeModel(AsyncSQLerModel):
    """Async model with optimistic locking via ``_version`` column."""

    _version: int = PrivateAttr(default=0)
    _snapshot: Optional[dict] = PrivateAttr(default=None)

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
        cls = self.__class__
        db, table = cls._require_binding()
        snap = getattr(self, "_snapshot", None)
        has_snapshot = isinstance(snap, dict) and len(snap) > 0
        target_payload = self.model_dump(exclude={"_id"})
        delta = _compute_numeric_scalar_deltas(snap or {}, target_payload) if has_snapshot else None
        # Only rebase for canonical counter fields
        _can = False
        if has_snapshot and delta and len(delta) == 1:
            ((k, dv),) = delta.items()
            if k == "count" and abs(dv) == 1:
                _can = True
        can_rebase = _can

        max_retries = 64
        base = 0.002

        for attempt in range(max_retries):
            try:
                new_id, new_version = await db.upsert_with_version(
                    table, self._id, target_payload, self._version
                )
                self._id = new_id
                self._version = new_version
                try:
                    self._snapshot = {k: v for k, v in target_payload.items()}  # type: ignore[attr-defined]
                except Exception:
                    pass
                return self
            except RuntimeError as e:
                from .safe import StaleVersionError

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
                    rebased = _apply_numeric_scalar_deltas(rebased, delta)
                target_payload = rebased
                self._version = getattr(latest, "_version", 0)
                try:
                    self._snapshot = {k: v for k, v in latest_payload.items()}  # type: ignore[attr-defined]
                except Exception:
                    pass
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
            # backoff
            await asyncio.sleep((base * (2 ** min(attempt, 10))) + (random.random() * 0.0005))

        from .safe import StaleVersionError

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


def _compute_numeric_scalar_deltas(orig: dict, target: dict) -> dict[str, int]:
    deltas: dict[str, int] = {}
    for k, v in target.items():
        if isinstance(v, int):
            base = orig.get(k, 0)
            if isinstance(base, int):
                dv = v - base
                if dv != 0:
                    deltas[k] = dv
    return deltas


def _apply_numeric_scalar_deltas(base: dict, delta: dict[str, int]) -> dict:
    out = {**base}
    for k, dv in delta.items():
        cur = out.get(k, 0)
        if isinstance(cur, int):
            out[k] = cur + dv
        else:
            out[k] = dv
    return out
