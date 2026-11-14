from __future__ import annotations

from typing import Optional, TypedDict


class SQLerRef(TypedDict):
    _table: str
    _id: int


def as_ref(model) -> SQLerRef:
    """Return a standardized reference dict for a SQLerModel instance.

    Raises:
        ValueError: If the model is unsaved (missing _id).
        InvalidTableNameError: If the class's table name is not valid.
    """
    from sqler.models._table_names import _ref_table

    _id: Optional[int] = getattr(model, "_id", None)
    if _id is None:
        raise ValueError("Cannot create ref: model must be saved")
    table: str = _ref_table(model)
    return {"_table": table, "_id": int(_id)}
