"""Unified exception hierarchy for SQLer.

This module provides a consistent set of exceptions for all SQLer operations,
making error handling more predictable and informative.
"""

from typing import Any, Optional


class SQLerError(Exception):
    """Base exception for all SQLer errors.

    All SQLer-specific exceptions inherit from this class, allowing
    callers to catch all SQLer errors with a single except clause.
    """

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


# Connection and Adapter Errors


class ConnectionError(SQLerError):
    """Base class for connection-related errors."""

    pass


class NotConnectedError(ConnectionError):
    """Raised when attempting operations without an active connection."""

    pass


class ConnectionPoolExhaustedError(ConnectionError):
    """Raised when no connections are available in the pool."""

    pass


# Query Errors


class QueryError(SQLerError):
    """Base class for query-related errors."""

    def __init__(
        self,
        message: str,
        *,
        sql: Optional[str] = None,
        params: Optional[list[Any]] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.sql = sql
        self.params = params


class NoAdapterError(QueryError):
    """Raised when attempting to execute a query without an adapter."""

    pass


class QueryTimeoutError(QueryError):
    """Raised when a query exceeds the configured timeout."""

    pass


class InvariantViolationError(QueryError):
    """Raised when data violates expected invariants (e.g., NULL JSON)."""

    pass


# Model Errors


class ModelError(SQLerError):
    """Base class for model-related errors."""

    pass


class NotBoundError(ModelError):
    """Raised when a model class is not bound to a database."""

    pass


class ValidationError(ModelError):
    """Raised when model validation fails."""

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        value: Any = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.field = field
        self.value = value


class NotFoundError(ModelError):
    """Raised when a requested model instance is not found."""

    def __init__(
        self,
        message: str,
        *,
        model: Optional[str] = None,
        id_: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.model = model
        self.id_ = id_


# Concurrency Errors


class ConcurrencyError(SQLerError):
    """Base class for concurrency-related errors."""

    pass


class StaleVersionError(ConcurrencyError):
    """Raised when saving a model with a stale version (optimistic locking conflict)."""

    def __init__(
        self,
        message: str,
        *,
        expected_version: Optional[int] = None,
        actual_version: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.expected_version = expected_version
        self.actual_version = actual_version


class DeadlockError(ConcurrencyError):
    """Raised when a deadlock is detected."""

    pass


class LockTimeoutError(ConcurrencyError):
    """Raised when unable to acquire a lock within the timeout period."""

    pass


# Integrity Errors


class IntegrityError(SQLerError):
    """Base class for data integrity errors."""

    pass


class ReferentialIntegrityError(IntegrityError):
    """Raised when a referential integrity constraint is violated."""

    def __init__(
        self,
        message: str,
        *,
        table: Optional[str] = None,
        id_: Optional[int] = None,
        referrer_count: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.table = table
        self.id_ = id_
        self.referrer_count = referrer_count


class UniqueConstraintError(IntegrityError):
    """Raised when a unique constraint is violated."""

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        value: Any = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.field = field
        self.value = value


# Schema Errors


class SchemaError(SQLerError):
    """Base class for schema-related errors."""

    pass


class TableNotFoundError(SchemaError):
    """Raised when a referenced table does not exist."""

    def __init__(
        self,
        message: str,
        *,
        table: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.table = table


class InvalidTableNameError(SchemaError):
    """Raised when a table name is invalid (e.g., contains special characters)."""

    def __init__(
        self,
        message: str,
        *,
        table: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.table = table


# Hook Errors


class HookError(SQLerError):
    """Base class for lifecycle hook errors."""

    pass


class BeforeSaveError(HookError):
    """Raised when a before_save hook fails or returns False."""

    pass


class AfterSaveError(HookError):
    """Raised when an after_save hook fails."""

    pass


class BeforeDeleteError(HookError):
    """Raised when a before_delete hook fails or returns False."""

    pass


class AfterDeleteError(HookError):
    """Raised when an after_delete hook fails."""

    pass


# Relationship Resolution Errors


class ResolutionError(SQLerError):
    """Base class for relationship resolution errors."""

    pass


class CircularReferenceError(ResolutionError):
    """Raised when a circular reference is detected during relationship resolution.

    This error occurs when resolving relationships creates an infinite loop,
    such as A -> B -> A.
    """

    def __init__(
        self,
        message: str,
        *,
        path: Optional[list[tuple[str, int]]] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.path = path or []

    def __str__(self) -> str:
        if self.path:
            path_str = " -> ".join(f"{table}:{id_}" for table, id_ in self.path)
            return f"{self.message}: {path_str}"
        return self.message


class MaxDepthExceededError(ResolutionError):
    """Raised when relationship resolution exceeds the maximum allowed depth."""

    def __init__(
        self,
        message: str,
        *,
        max_depth: Optional[int] = None,
        current_depth: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.max_depth = max_depth
        self.current_depth = current_depth


class MissingReferenceError(ResolutionError):
    """Warning-level error for missing references during batch resolution.

    This is used when a referenced row doesn't exist but we want to continue
    resolving other references rather than failing completely.
    """

    def __init__(
        self,
        message: str,
        *,
        table: Optional[str] = None,
        id_: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.table = table
        self.id_ = id_


# Backwards compatibility alias
MissingReferenceWarning = MissingReferenceError


# Batch Operation Errors


class BatchOperationError(SQLerError):
    """Raised when a batch operation partially fails."""

    def __init__(
        self,
        message: str,
        *,
        succeeded: Optional[list[int]] = None,
        failed: Optional[list[tuple[int, str]]] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.succeeded = succeeded or []
        self.failed = failed or []
