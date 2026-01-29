"""SQLer models - ORM models with optional Pydantic dependency.

This module provides two model backends:

1. **Pydantic models** (default): Full-featured models with type validation.
   - SQLerModel, SQLerSafeModel, AsyncSQLerModel, AsyncSQLerSafeModel

2. **Lite models** (Pydantic-free): Dataclass-based models for Pyodide compatibility.
   - SQLerLiteModel, SQLerLiteSafeModel, AsyncSQLerLiteModel, AsyncSQLerLiteSafeModel

Usage with Pydantic (default):
    from sqler import SQLerModel, SQLerDB

    class User(SQLerModel):
        name: str
        email: str

Usage without Pydantic (Pyodide/lite mode):
    from dataclasses import dataclass
    from sqler import SQLerLiteModel, SQLerDB

    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"
        name: str
        email: str
"""

from dataclasses import dataclass

from sqler.exceptions import ReferentialIntegrityError, StaleVersionError

# Compatibility layer
from ._compat import (
    PYDANTIC_AVAILABLE,
    PYDANTIC_VERSION,
    get_model_backend,
    is_lite_model,
    is_pydantic_model,
    require_pydantic,
)

# Lite models (always available - no Pydantic dependency)
from .lite import (
    SQLerLiteModel,
    SQLerLiteModelBase,
    SQLerLiteSafeModel,
)

# Always-available imports (no Pydantic dependency)
from .utils import (
    DEFAULT_REBASE_CONFIG,
    NO_REBASE_CONFIG,
    PERMISSIVE_REBASE_CONFIG,
    RebaseConfig,
)


@dataclass
class BrokenRef:
    """Represents a broken reference found during validation."""

    table: str
    row_id: int
    path: str
    target_table: str
    target_id: int


# Conditionally import Pydantic-based models
if PYDANTIC_AVAILABLE:
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
else:
    # Provide stub classes that raise helpful errors when Pydantic is not installed

    def _pydantic_required_class(name: str):
        """Create a stub class that raises ImportError on instantiation."""

        class _Stub:
            def __init__(self, *args, **kwargs):
                require_pydantic(f"{name}")

            def __init_subclass__(cls, **kwargs):
                require_pydantic(f"{name}")

        _Stub.__name__ = name
        _Stub.__qualname__ = name
        return _Stub

    SQLerModel = _pydantic_required_class("SQLerModel")  # type: ignore[misc]
    SQLerSafeModel = _pydantic_required_class("SQLerSafeModel")  # type: ignore[misc]
    AsyncSQLerModel = _pydantic_required_class("AsyncSQLerModel")  # type: ignore[misc]
    AsyncSQLerSafeModel = _pydantic_required_class("AsyncSQLerSafeModel")  # type: ignore[misc]

    # QuerySets and helpers
    SQLerQuerySet = _pydantic_required_class("SQLerQuerySet")  # type: ignore[misc]
    AsyncSQLerQuerySet = _pydantic_required_class("AsyncSQLerQuerySet")  # type: ignore[misc]
    SQLerModelField = _pydantic_required_class("SQLerModelField")  # type: ignore[misc]
    SQLerRef = _pydantic_required_class("SQLerRef")  # type: ignore[misc]

    def as_ref(*args, **kwargs):
        require_pydantic("as_ref")

    # Mixins
    TimestampMixin = _pydantic_required_class("TimestampMixin")  # type: ignore[misc]
    SoftDeleteMixin = _pydantic_required_class("SoftDeleteMixin")  # type: ignore[misc]
    AsyncSoftDeleteMixin = _pydantic_required_class("AsyncSoftDeleteMixin")  # type: ignore[misc]
    HooksMixin = _pydantic_required_class("HooksMixin")  # type: ignore[misc]
    AsyncHooksMixin = _pydantic_required_class("AsyncHooksMixin")  # type: ignore[misc]
    FullMixin = _pydantic_required_class("FullMixin")  # type: ignore[misc]
    AsyncFullMixin = _pydantic_required_class("AsyncFullMixin")  # type: ignore[misc]

    # Async integrity helpers
    async def async_find_referrers(*args, **kwargs):
        require_pydantic("async_find_referrers")

    async def async_set_null_referrers(*args, **kwargs):
        require_pydantic("async_set_null_referrers")

    async def async_cascade_delete(*args, **kwargs):
        require_pydantic("async_cascade_delete")

    async def async_validate_references(*args, **kwargs):
        require_pydantic("async_validate_references")


# Async lite models (try import, may fail if aiosqlite not available)
try:
    from .lite import AsyncSQLerLiteModel, AsyncSQLerLiteSafeModel

    _ASYNC_LITE_AVAILABLE = True
except ImportError:
    _ASYNC_LITE_AVAILABLE = False

    def _async_lite_required_class(name: str):
        class _Stub:
            def __init__(self, *args, **kwargs):
                raise ImportError(
                    f"{name} requires aiosqlite.\n"
                    f"Install with: pip install aiosqlite"
                )

        _Stub.__name__ = name
        _Stub.__qualname__ = name
        return _Stub

    AsyncSQLerLiteModel = _async_lite_required_class("AsyncSQLerLiteModel")  # type: ignore[misc]
    AsyncSQLerLiteSafeModel = _async_lite_required_class("AsyncSQLerLiteSafeModel")  # type: ignore[misc]


__all__ = [
    # Pydantic models (require pydantic)
    "SQLerModel",
    "SQLerQuerySet",
    "SQLerSafeModel",
    "AsyncSQLerModel",
    "AsyncSQLerQuerySet",
    "AsyncSQLerSafeModel",
    "SQLerModelField",
    "SQLerRef",
    "as_ref",
    # Lite models (no pydantic required)
    "SQLerLiteModel",
    "SQLerLiteModelBase",
    "SQLerLiteSafeModel",
    "AsyncSQLerLiteModel",
    "AsyncSQLerLiteSafeModel",
    # Exceptions
    "StaleVersionError",
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
    # Compatibility helpers
    "PYDANTIC_AVAILABLE",
    "PYDANTIC_VERSION",
    "require_pydantic",
    "get_model_backend",
    "is_pydantic_model",
    "is_lite_model",
]
