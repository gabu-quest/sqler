from dataclasses import dataclass

from sqler.exceptions import ReferentialIntegrityError, StaleVersionError

from .async_integrity import (
    async_cascade_delete,
    async_find_referrers,
    async_set_null_referrers,
    async_validate_references,
)
from .async_model import AsyncSQLerModel
from .async_queryset import AsyncSQLerQuerySet
from .async_safe import AsyncSQLerSafeModel
from .mixins import (
    AsyncFullMixin,
    AsyncHooksMixin,
    AsyncSoftDeleteMixin,
    FullMixin,
    HooksMixin,
    SoftDeleteMixin,
    TimestampMixin,
)
from .model import SQLerModel
from .model_field import SQLerModelField
from .queryset import SQLerQuerySet
from .ref import SQLerRef, as_ref
from .safe import SQLerSafeModel
from .utils import (
    DEFAULT_REBASE_CONFIG,
    NO_REBASE_CONFIG,
    PERMISSIVE_REBASE_CONFIG,
    RebaseConfig,
)


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
    # Mixins
    "TimestampMixin",
    "SoftDeleteMixin",
    "AsyncSoftDeleteMixin",
    "HooksMixin",
    "AsyncHooksMixin",
    "FullMixin",
    "AsyncFullMixin",
    # Rebase configuration
    "RebaseConfig",
    "DEFAULT_REBASE_CONFIG",
    "PERMISSIVE_REBASE_CONFIG",
    "NO_REBASE_CONFIG",
    # Async integrity helpers
    "async_find_referrers",
    "async_set_null_referrers",
    "async_cascade_delete",
    "async_validate_references",
]
