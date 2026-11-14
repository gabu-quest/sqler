"""Unified exception hierarchy for SQLer.

This module provides a consistent set of exceptions for all SQLer operations,
making error handling more predictable and informative.
"""

import builtins
import re
import sqlite3
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


class NoAdapterError(QueryError, builtins.ConnectionError):
    """Raised when attempting to execute a query without an adapter.

    The one error for a sync and an async query. It is a ``SQLerError`` and also
    Python's built-in ``ConnectionError``, which the sync query's own class used to
    be, so ``except ConnectionError`` around a sync query keeps working.
    """

    def __reduce__(self):
        return (_rebuild_no_adapter_error, (self.message, self.sql, self.params, self.details))


def _rebuild_no_adapter_error(message, sql, params, details):
    return NoAdapterError(message, sql=sql, params=params, details=details)


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


class UniqueConstraintError(IntegrityError, sqlite3.IntegrityError):
    """Raised when a unique constraint (a unique index or a primary key) refuses a row.

    The sync and the async adapter raise it in place of the driver's
    ``sqlite3.IntegrityError`` when SQLite reports ``UNIQUE constraint failed``.
    It is also a ``sqlite3.IntegrityError``, so ``except sqlite3.IntegrityError``
    still catches it, and the driver error is its ``__cause__``.

    Attributes:
        table: The table SQLite named, or ``None`` for an index on an expression.
        columns: The columns SQLite named, in its order (empty for an index on an expression).
        index: The index SQLite named when it names no column, else ``None``.
        field: The column when SQLite named exactly one, else ``None``.
        value: Not known to SQLite's message; ``None`` unless the caller sets it.
    """

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        value: Any = None,
        details: Optional[dict[str, Any]] = None,
        table: Optional[str] = None,
        columns: Optional[list[str]] = None,
        index: Optional[str] = None,
    ):
        super().__init__(message, details=details)
        self.field = field
        self.value = value
        self.table = table
        self.columns = list(columns or [])
        self.index = index


_UNIQUE_PREFIX = "UNIQUE constraint failed: "
_UNIQUE_ERRORNAMES = frozenset({"SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"})
_UNIQUE_INDEX_RE = re.compile(r"index '(?P<index>.*)'")


def unique_violation_from(exc: BaseException) -> Optional[UniqueConstraintError]:
    """Return the ``UniqueConstraintError`` for a driver error that reports a unique violation.

    The one place where a SQLite error becomes a sqler error; the sync and the
    async adapter call it on every failed statement. Returns ``None`` for
    anything that is not a ``sqlite3.IntegrityError`` whose message starts with
    ``UNIQUE constraint failed:`` and whose extended code is a unique index or a
    primary key, not a trigger (NOT NULL, CHECK and FOREIGN KEY errors stay
    as they are), and for an error that is already a ``UniqueConstraintError``.
    The caller raises the result ``from exc``.
    """
    if not isinstance(exc, sqlite3.IntegrityError) or isinstance(exc, UniqueConstraintError):
        return None
    message = str(exc)
    if not message.startswith(_UNIQUE_PREFIX):
        return None
    # The message alone can be forged by a trigger (RAISE(ABORT, 'UNIQUE constraint
    # failed: ...')). SQLite's extended code tells a unique index or a primary key
    # from a trigger. ``sqlite_errorname`` exists from Python 3.11 and the project
    # requires 3.12; an error without it is left as the driver raised it.
    if getattr(exc, "sqlite_errorname", None) not in _UNIQUE_ERRORNAMES:
        return None
    named = message[len(_UNIQUE_PREFIX):]
    table: Optional[str] = None
    columns: list[str] = []
    index: Optional[str] = None
    match = _UNIQUE_INDEX_RE.fullmatch(named)
    if match:
        index = match.group("index")
    else:
        for part in named.split(", "):
            tbl, _, col = part.partition(".")
            table = tbl
            columns.append(col)
    err = UniqueConstraintError(
        message,
        field=columns[0] if len(columns) == 1 else None,
        table=table,
        columns=columns,
        index=index,
    )
    err.sqlite_errorcode = getattr(exc, "sqlite_errorcode", None)
    err.sqlite_errorname = getattr(exc, "sqlite_errorname", None)
    return err


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


class InvalidFieldNameError(SchemaError):
    """Raised when a field name / JSON path is invalid."""

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.field = field


class InvalidIdentifierError(SchemaError):
    """Raised when an identifier (index name, column name) is invalid."""

    def __init__(
        self,
        message: str,
        *,
        identifier: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.identifier = identifier


class InvalidColumnDefError(SchemaError):
    """Raised when a promoted-column definition or CHECK expression is invalid."""

    def __init__(
        self,
        message: str,
        *,
        definition: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, details=details)
        self.definition = definition


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
