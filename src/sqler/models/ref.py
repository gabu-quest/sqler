from __future__ import annotations

from typing import Optional, TypedDict


class SQLerRef(TypedDict):
    _table: str
    _id: int


def as_ref(model) -> SQLerRef:
    """Return a standardized reference dict for a SQLerModel instance.

    Raises:
        ValueError: If the model is unsaved (missing _id) or has no bound table.
    """
    table: Optional[str] = getattr(model.__class__, "_table", None)
    _id: Optional[int] = getattr(model, "_id", None)
    if not table or _id is None:
        raise ValueError("Cannot create ref: model must be bound and saved")
    return {"_table": table, "_id": int(_id)}
