from .adapter import AsyncSQLiteAdapter, NotConnectedError, SQLiteAdapter
from .db import SQLerDB
from .db.async_db import AsyncSQLerDB
from .exceptions import (
    BatchOperationError,
    CircularReferenceError,
    ConcurrencyError,
    IntegrityError,
    MaxDepthExceededError,
    MissingReferenceWarning,
    ModelError,
    NotFoundError,
    QueryError,
    QueryTimeoutError,
    ResolutionError,
    SchemaError,
    SQLerError,
    UniqueConstraintError,
    ValidationError,
)
from .logging import QueryLog, QueryLogger, query_logger, timed_query
from .models import (
    DEFAULT_REBASE_CONFIG,
    NO_REBASE_CONFIG,
    PERMISSIVE_REBASE_CONFIG,
    AsyncFullMixin,
    AsyncHooksMixin,
    AsyncSQLerModel,
    AsyncSQLerQuerySet,
    AsyncSQLerSafeModel,
    FullMixin,
    HooksMixin,
    RebaseConfig,
    ReferentialIntegrityError,
    SoftDeleteMixin,
    SQLerModel,
    SQLerQuerySet,
    SQLerSafeModel,
    StaleVersionError,
    TimestampMixin,
)
from .query import F, SQLerExpression, SQLerField, SQLerQuery
from .query.query import PaginatedResult
from .registry import register, resolve, tables

__all__ = [
    # Adapters
    "SQLiteAdapter",
    "AsyncSQLiteAdapter",
    "NotConnectedError",
    # Database
    "SQLerDB",
    "AsyncSQLerDB",
    # Models
    "SQLerModel",
    "SQLerQuerySet",
    "SQLerSafeModel",
    "StaleVersionError",
    "AsyncSQLerModel",
    "AsyncSQLerQuerySet",
    "AsyncSQLerSafeModel",
    # Mixins
    "TimestampMixin",
    "SoftDeleteMixin",
    "HooksMixin",
    "AsyncHooksMixin",
    "FullMixin",
    "AsyncFullMixin",
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
    # Errors - Base
    "SQLerError",
    "ReferentialIntegrityError",
    # Errors - Query
    "QueryError",
    "QueryTimeoutError",
    # Errors - Model
    "ModelError",
    "NotFoundError",
    "ValidationError",
    # Errors - Concurrency
    "ConcurrencyError",
    # Errors - Integrity
    "IntegrityError",
    "UniqueConstraintError",
    # Errors - Schema
    "SchemaError",
    # Errors - Resolution
    "ResolutionError",
    "CircularReferenceError",
    "MaxDepthExceededError",
    "MissingReferenceWarning",
    # Errors - Batch
    "BatchOperationError",
]
