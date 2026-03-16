from .adapter import AsyncSQLiteAdapter, SQLiteAdapter
from .cache import (
    CacheAwareModel,
    CacheEntry,
    CacheStats,
    QueryCache,
    async_cached_query,
    cached_query,
    configure_cache,
    get_cache,
)
from .db import SQLerDB
from .db.async_db import AsyncSQLerDB
from .exceptions import (
    BatchOperationError,
    CircularReferenceError,
    ConcurrencyError,
    ConnectionPoolExhaustedError,
    IntegrityError,
    MaxDepthExceededError,
    MissingReferenceError,
    MissingReferenceWarning,
    ModelError,
    NoAdapterError,
    NotBoundError,
    NotConnectedError,
    NotFoundError,
    QueryError,
    QueryTimeoutError,
    ResolutionError,
    SchemaError,
    SQLerError,
    StaleVersionError,
    UniqueConstraintError,
    ValidationError,
)
from .export import (
    ExportResult,
    ImportResult,
    async_export_jsonl,
    async_import_jsonl,
    export_csv,
    export_csv_string,
    export_json,
    export_json_string,
    export_jsonl,
    import_csv,
    import_json,
    import_jsonl,
    stream_jsonl,
)
from .fts import FTSIndex, FTSStats, SearchableMixin, SearchResult
from .logging import QueryLog, QueryLogger, query_logger, timed_query
from .metrics import MetricsCollector, metrics
from .migrations import (
    AsyncMigration,
    AsyncMigrationRunner,
    Migration,
    MigrationRecord,
    MigrationResult,
    MigrationRunner,
)

# Always-available imports
from .models import (
    DEFAULT_REBASE_CONFIG,
    NO_REBASE_CONFIG,
    PERMISSIVE_REBASE_CONFIG,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    RebaseConfig,
    ReferentialIntegrityError,
    # Lite models (Pydantic-free for Pyodide)
    SQLerLiteModel,
    SQLerLiteModelBase,
    SQLerLiteSafeModel,
    # Msgspec models (optional - requires msgspec)
    SQLerMsgspecModel,
    SQLerMsgspecModelBase,
    is_lite_model,
    is_msgspec_model,
    # Compatibility helpers
    is_pydantic_model,
)

# Import compatibility helpers first (no Pydantic dependency)
from .models._compat import (
    MSGSPEC_AVAILABLE,
    MSGSPEC_VERSION,
    PYDANTIC_AVAILABLE,
    PYDANTIC_VERSION,
    get_model_backend,
    require_msgspec,
    require_pydantic,
)

# Mixins (always available — no Pydantic dependency)
from .models.mixins import (
    AsyncAuditLogMixin,
    AsyncAuditMixin,
    AsyncFullMixin,
    AsyncHooksMixin,
    AsyncSoftDeleteMixin,
    AuditLogMixin,
    AuditMixin,
    FullMixin,
    HooksMixin,
    SoftDeleteMixin,
    TimestampMixin,
)

# Conditionally import Pydantic-dependent items
if PYDANTIC_AVAILABLE:
    from .models import (
        AsyncSQLerModel,
        AsyncSQLerQuerySet,
        AsyncSQLerSafeModel,
        SQLerModel,
        SQLerQuerySet,
        SQLerSafeModel,
    )
else:
    # Provide stub classes that raise helpful errors

    def _pydantic_required_class(name: str):
        class _Stub:
            def __init__(self, *args, **kwargs):
                require_pydantic(f"{name}")

            def __init_subclass__(cls, **kwargs):
                require_pydantic(f"{name}")

        _Stub.__name__ = name
        _Stub.__qualname__ = name
        return _Stub

    SQLerModel = _pydantic_required_class("SQLerModel")
    SQLerSafeModel = _pydantic_required_class("SQLerSafeModel")
    AsyncSQLerModel = _pydantic_required_class("AsyncSQLerModel")
    AsyncSQLerSafeModel = _pydantic_required_class("AsyncSQLerSafeModel")
    SQLerQuerySet = _pydantic_required_class("SQLerQuerySet")
    AsyncSQLerQuerySet = _pydantic_required_class("AsyncSQLerQuerySet")
from .ops import (
    BackupResult,
    DatabaseStats,
    HealthStatus,
    async_backup,
    async_checkpoint,
    async_get_stats,
    async_health_check,
    async_is_healthy,
    async_vacuum,
    backup,
    checkpoint,
    get_stats,
    health_check,
    is_healthy,
    restore,
    vacuum,
)
from .pool import ConnectionPool, PooledSQLerDB, PooledSQLiteAdapter, PoolStats
from .query import F, SQLerExpression, SQLerField, SQLerQuery
from .query.query import PaginatedResult
from .registry import register, resolve, tables
from .tracking import (
    ChangeTracker,
    DiffMixin,
    FieldChange,
    PartialUpdateMixin,
    TrackedModel,
)

__all__ = [
    # Adapters
    "SQLiteAdapter",
    "AsyncSQLiteAdapter",
    # Database
    "SQLerDB",
    "AsyncSQLerDB",
    # Models (Pydantic)
    "SQLerModel",
    "SQLerQuerySet",
    "SQLerSafeModel",
    "AsyncSQLerModel",
    "AsyncSQLerQuerySet",
    "AsyncSQLerSafeModel",
    # Lite models (Pydantic-free for Pyodide)
    "SQLerLiteModel",
    "SQLerLiteModelBase",
    "SQLerLiteSafeModel",
    "AsyncSQLerLiteModel",
    "AsyncSQLerLiteSafeModel",
    # Msgspec models (optional)
    "SQLerMsgspecModel",
    "SQLerMsgspecModelBase",
    "MSGSPEC_AVAILABLE",
    "MSGSPEC_VERSION",
    "require_msgspec",
    "is_msgspec_model",
    # Compatibility helpers
    "PYDANTIC_AVAILABLE",
    "PYDANTIC_VERSION",
    "require_pydantic",
    "get_model_backend",
    "is_pydantic_model",
    "is_lite_model",
    # Mixins
    "TimestampMixin",
    "SoftDeleteMixin",
    "AsyncSoftDeleteMixin",
    "HooksMixin",
    "AsyncHooksMixin",
    "FullMixin",
    "AsyncFullMixin",
    "AuditMixin",
    "AsyncAuditMixin",
    "AuditLogMixin",
    "AsyncAuditLogMixin",
    # Rebase configuration
    "RebaseConfig",
    "DEFAULT_REBASE_CONFIG",
    "PERMISSIVE_REBASE_CONFIG",
    "NO_REBASE_CONFIG",
    # Query
    "SQLerQuery",
    "SQLerExpression",
    "SQLerField",
    "F",  # Alias for SQLerField (Django-like syntax)
    "PaginatedResult",
    # Registry
    "register",
    "resolve",
    "tables",
    # Logging
    "QueryLogger",
    "QueryLog",
    "query_logger",
    "timed_query",
    # Metrics
    "MetricsCollector",
    "metrics",
    # Migrations
    "Migration",
    "AsyncMigration",
    "MigrationRunner",
    "AsyncMigrationRunner",
    "MigrationRecord",
    "MigrationResult",
    # Operations (backup, health, etc.)
    "backup",
    "restore",
    "async_backup",
    "health_check",
    "is_healthy",
    "async_health_check",
    "async_is_healthy",
    "get_stats",
    "async_get_stats",
    "vacuum",
    "async_vacuum",
    "checkpoint",
    "async_checkpoint",
    "HealthStatus",
    "BackupResult",
    "DatabaseStats",
    # Connection Pool
    "ConnectionPool",
    "PooledSQLiteAdapter",
    "PooledSQLerDB",
    "PoolStats",
    # Errors - Base
    "SQLerError",
    "ReferentialIntegrityError",
    # Errors - Connection
    "NotConnectedError",
    "NoAdapterError",
    # Errors - Query
    "QueryError",
    "QueryTimeoutError",
    # Errors - Model
    "ModelError",
    "NotFoundError",
    "NotBoundError",
    "ValidationError",
    # Errors - Concurrency
    "ConcurrencyError",
    "StaleVersionError",
    # Errors - Integrity
    "IntegrityError",
    "UniqueConstraintError",
    # Errors - Schema
    "SchemaError",
    # Errors - Resolution
    "ResolutionError",
    "CircularReferenceError",
    "MaxDepthExceededError",
    "MissingReferenceError",
    "MissingReferenceWarning",
    # Errors - Batch
    "BatchOperationError",
    # Errors - Pool
    "ConnectionPoolExhaustedError",
    # Export/Import
    "export_csv",
    "export_csv_string",
    "export_json",
    "export_json_string",
    "export_jsonl",
    "import_csv",
    "import_json",
    "import_jsonl",
    "stream_jsonl",
    "async_export_jsonl",
    "async_import_jsonl",
    "ExportResult",
    "ImportResult",
    # Cache
    "QueryCache",
    "CacheEntry",
    "CacheStats",
    "CacheAwareModel",
    "cached_query",
    "async_cached_query",
    "get_cache",
    "configure_cache",
    # Full-Text Search
    "FTSIndex",
    "FTSStats",
    "SearchResult",
    "SearchableMixin",
    # Change Tracking
    "TrackedModel",
    "ChangeTracker",
    "FieldChange",
    "PartialUpdateMixin",
    "DiffMixin",
]
