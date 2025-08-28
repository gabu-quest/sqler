from dataclasses import dataclass

from .async_model import AsyncSQLerModel
from .async_queryset import AsyncSQLerQuerySet
from .async_safe import AsyncSQLerSafeModel
from .model import SQLerModel
from .model_field import SQLerModelField
from .queryset import SQLerQuerySet
from .ref import SQLerRef, as_ref
from .safe import SQLerSafeModel, StaleVersionError


class ReferentialIntegrityError(RuntimeError):
    """Raised when delete(on_delete='restrict') hits referencing rows."""


@dataclass
class BrokenRef:
    table: str
    row_id: int
    path: str
    target_table: str
    target_id: int


__all__ = [
    "SQLerModel",
    "SQLerQuerySet",
    "SQLerSafeModel",
    "StaleVersionError",
    "AsyncSQLerModel",
    "AsyncSQLerQuerySet",
    "AsyncSQLerSafeModel",
    "SQLerModelField",
    "SQLerRef",
    "as_ref",
    "ReferentialIntegrityError",
    "BrokenRef",
]
