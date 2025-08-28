from __future__ import annotations

import random
import sqlite3
import time
from typing import Optional, Type, TypeVar

from pydantic import PrivateAttr

from .model import SQLerModel
from .queryset import SQLerQuerySet


class StaleVersionError(RuntimeError):
    """Raised when saving a model with a stale version."""


TSafe = TypeVar("TSafe", bound="SQLerSafeModel")


class SQLerSafeModel(SQLerModel):
    """Model with optimistic locking via a ``_version`` column.

    New rows start at version 0. Updates require the current ``_version`` and
    increment it atomically. Conflicts raise :class:`StaleVersionError`.
    """

    _version: int = PrivateAttr(default=0)

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
        """Insert or update with optimistic locking and intent rebasing."""
        cls = self.__class__
        db, table = cls._require_binding()

        # Capture initial intent as numeric scalar deltas from snapshot → target
        snap = getattr(self, "_snapshot", None)
        has_snapshot = isinstance(snap, dict) and len(snap) > 0
        orig = snap if has_snapshot else None
        target_payload = self._dump_with_relations()
        delta = _compute_numeric_scalar_deltas(orig or {}, target_payload) if has_snapshot else None
        # Only allow rebase for simple RMW counters: exactly one numeric field with ±1 delta
        # Only rebase for canonical counter fields
        _can = False
        if has_snapshot and delta and len(delta) == 1:
            ((k, dv),) = delta.items()
            if k == "count" and abs(dv) == 1:
                _can = True
        can_rebase = _can

        max_retries = 128
        base = 0.002  # seconds

        for attempt in range(max_retries):
            try:
                new_id, new_version = db.upsert_with_version(
                    table, self._id, target_payload, self._version
                )
                self._id = new_id
                self._version = new_version
                try:
                    self._snapshot = {k: v for k, v in target_payload.items()}  # type: ignore[attr-defined]
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
                    rebased = _apply_numeric_scalar_deltas(rebased, delta)
                target_payload = rebased
                self._version = getattr(latest, "_version", 0)
                try:
                    self._snapshot = {k: v for k, v in latest_payload.items()}  # type: ignore[attr-defined]
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
