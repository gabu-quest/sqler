"""Shared utility functions for SQLer.

This module provides common utilities used across the library,
reducing code duplication and ensuring consistent behavior.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional, TypeVar

if TYPE_CHECKING:
    pass

# Compiled regex for table name validation
_TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# SQLite reserved words that produce invalid SQL when used as unquoted column names.
# Source: https://www.sqlite.org/lang_keywords.html
_SQL_RESERVED_WORDS: frozenset[str] = frozenset({
    "abort", "action", "add", "after", "all", "alter", "always", "analyze",
    "and", "as", "asc", "attach", "autoincrement", "before", "begin",
    "between", "by", "cascade", "case", "cast", "check", "collate", "column",
    "commit", "conflict", "constraint", "create", "cross", "current",
    "current_date", "current_time", "current_timestamp", "database", "default",
    "deferrable", "deferred", "delete", "desc", "detach", "distinct", "do",
    "drop", "each", "else", "end", "escape", "except", "exclude", "exclusive",
    "exists", "explain", "fail", "filter", "first", "following", "for",
    "foreign", "from", "full", "generated", "glob", "group", "groups",
    "having", "if", "ignore", "immediate", "in", "index", "indexed",
    "initially", "inner", "insert", "instead", "intersect", "into", "is",
    "isnull", "join", "key", "last", "left", "like", "limit", "match",
    "materialized", "natural", "no", "not", "nothing", "notnull", "null",
    "nulls", "of", "offset", "on", "or", "order", "others", "outer", "over",
    "partition", "plan", "pragma", "preceding", "primary", "query", "raise",
    "range", "recursive", "references", "regexp", "reindex", "release",
    "rename", "replace", "restrict", "returning", "right", "rollback", "row",
    "rows", "savepoint", "select", "set", "table", "temp", "temporary",
    "then", "ties", "to", "transaction", "trigger", "unbounded", "union",
    "unique", "update", "using", "vacuum", "values", "view", "virtual",
    "when", "where", "window", "with", "without",
})

logger = logging.getLogger("sqler.utils")


def validate_table_name(table: str) -> str:
    """Validate and sanitize table name to prevent SQL injection.

    Args:
        table: Table name to validate.

    Returns:
        str: The validated table name.

    Raises:
        InvalidTableNameError: If the table name contains invalid characters.
    """
    from sqler.exceptions import InvalidTableNameError

    if not isinstance(table, str) or not table:
        raise InvalidTableNameError(
            f"Table name must be a non-empty string, got: {type(table).__name__}",
            table=table,
        )
    if not _TABLE_NAME_PATTERN.match(table):
        raise InvalidTableNameError(
            f"Invalid table name: {table!r}. Must match [a-zA-Z_][a-zA-Z0-9_]*",
            table=table,
        )
    return table


def validate_field_name(name: str) -> str:
    """Validate a dotted JSON field path (e.g. ``meta.level``).

    Each segment must match ``[a-zA-Z_][a-zA-Z0-9_]*``.

    Args:
        name: Dotted field path to validate.

    Returns:
        str: The validated field name.

    Raises:
        InvalidFieldNameError: If any segment is invalid.
    """
    from sqler.exceptions import InvalidFieldNameError

    if not isinstance(name, str) or not name:
        raise InvalidFieldNameError(
            f"Field name must be a non-empty string, got: {type(name).__name__}",
            field=name,
        )
    segments = name.split(".")
    for segment in segments:
        if not _TABLE_NAME_PATTERN.match(segment):
            raise InvalidFieldNameError(
                f"Invalid field name: {name!r}. "
                f"Each segment must match [a-zA-Z_][a-zA-Z0-9_]*",
                field=name,
            )
    return name


def validate_identifier(name: str) -> str:
    """Validate a SQL identifier (index name, column name).

    Must match ``[a-zA-Z_][a-zA-Z0-9_]*``.

    Args:
        name: Identifier to validate.

    Returns:
        str: The validated identifier.

    Raises:
        InvalidIdentifierError: If the identifier is invalid.
    """
    from sqler.exceptions import InvalidIdentifierError

    if not isinstance(name, str) or not name:
        raise InvalidIdentifierError(
            f"Identifier must be a non-empty string, got: {type(name).__name__}",
            identifier=name,
        )
    if not _TABLE_NAME_PATTERN.match(name):
        raise InvalidIdentifierError(
            f"Invalid identifier: {name!r}. Must match [a-zA-Z_][a-zA-Z0-9_]*",
            identifier=name,
        )
    return name


# Grammar for promoted-column definitions.
# Accepts: TYPE [NOT NULL] [DEFAULT literal]
#   TYPE ::= TEXT | INTEGER | REAL | BLOB | NUMERIC [(p[,s])]
#   literal ::= integer | float | 'single-quoted' | NULL | CURRENT_TIMESTAMP | CURRENT_DATE | CURRENT_TIME
# DEFAULT sub-queries, expression-valued DEFAULTs, and parenthesized expressions are intentionally rejected
# — those are DDL-injection vectors that cannot be parameterized in SQLite.
_COLUMN_DEF_PATTERN = re.compile(
    r"""
    ^\s*
    (?:TEXT|INTEGER|REAL|BLOB|NUMERIC(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)
    (?:\s+NOT\s+NULL)?
    (?:\s+DEFAULT\s+
        (?:
            -?\d+(?:\.\d+)?                # numeric literal
          | '(?:[^'\\;]|'')*'              # single-quoted string literal (SQL-style '' escape allowed)
          | NULL
          | CURRENT_TIMESTAMP
          | CURRENT_DATE
          | CURRENT_TIME
        )
    )?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Keywords that must never appear inside a CHECK expression — they belong to DDL/DML, not predicates.
_CHECK_EXPR_BANNED_KEYWORDS = re.compile(
    r"\b(?:DROP|CREATE|ALTER|ATTACH|DETACH|PRAGMA|INSERT|UPDATE|DELETE|REPLACE|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)


def validate_column_def(col_def: str) -> str:
    """Validate a promoted-column definition string (e.g. ``TEXT NOT NULL DEFAULT 'pending'``).

    Promoted column *definitions* are interpolated into DDL unparameterized (SQLite DDL does
    not accept bound parameters), so the accepted grammar is strict: a SQLite storage type,
    optional ``NOT NULL``, and optional ``DEFAULT`` of a literal value. Anything richer —
    subqueries, parenthesised expressions, function calls — is rejected.

    Args:
        col_def: The definition to validate.

    Returns:
        str: The validated definition, unchanged.

    Raises:
        InvalidColumnDefError: If the definition contains anything outside the accepted grammar.
    """
    from sqler.exceptions import InvalidColumnDefError

    if not isinstance(col_def, str) or not col_def.strip():
        raise InvalidColumnDefError(
            f"Column definition must be a non-empty string, got: {type(col_def).__name__}",
            definition=col_def,
        )
    if not _COLUMN_DEF_PATTERN.match(col_def):
        raise InvalidColumnDefError(
            f"Invalid column definition: {col_def!r}. "
            "Accepted grammar: "
            "TYPE [NOT NULL] [DEFAULT <literal>] where TYPE is TEXT|INTEGER|REAL|BLOB|NUMERIC "
            "and <literal> is a number, quoted string, NULL, or CURRENT_TIMESTAMP/CURRENT_DATE/CURRENT_TIME.",
            definition=col_def,
        )
    return col_def


def validate_check_expression(expr: str) -> str:
    """Validate a CHECK-constraint expression.

    CHECK expressions are interpolated raw into DDL, so we forbid anything that could escape
    the constraint context: semicolons, SQL comment markers, and DDL/DML keywords.

    This is not a full SQL parser — it's an allowlist of the *shape* we expect (a predicate),
    rejecting the common injection vectors. Users who need richer CHECK expressions should
    construct the table DDL directly rather than relying on promoted-column convenience.

    Args:
        expr: The CHECK expression to validate.

    Returns:
        str: The validated expression, unchanged.

    Raises:
        InvalidColumnDefError: If the expression contains a forbidden token.
    """
    from sqler.exceptions import InvalidColumnDefError

    if not isinstance(expr, str) or not expr.strip():
        raise InvalidColumnDefError(
            f"CHECK expression must be a non-empty string, got: {type(expr).__name__}",
            definition=expr,
        )
    if ";" in expr:
        raise InvalidColumnDefError(
            f"CHECK expression may not contain ';': {expr!r}",
            definition=expr,
        )
    if "--" in expr or "/*" in expr or "*/" in expr:
        raise InvalidColumnDefError(
            f"CHECK expression may not contain SQL comment markers: {expr!r}",
            definition=expr,
        )
    match = _CHECK_EXPR_BANNED_KEYWORDS.search(expr)
    if match:
        raise InvalidColumnDefError(
            f"CHECK expression may not contain keyword {match.group(0)!r}: {expr!r}",
            definition=expr,
        )
    return expr


def validate_column_name(name: str) -> str:
    """Validate a SQL column name — format check plus reserved-word rejection.

    Promoted columns appear unquoted in DDL and DML, so SQLite reserved words
    like ``index``, ``order``, or ``group`` would produce invalid SQL.

    Args:
        name: Column name to validate.

    Returns:
        str: The validated column name.

    Raises:
        InvalidIdentifierError: If the name is invalid or a reserved word.
    """
    from sqler.exceptions import InvalidIdentifierError

    validate_identifier(name)
    if name.lower() in _SQL_RESERVED_WORDS:
        raise InvalidIdentifierError(
            f"'{name}' is a SQLite reserved word and cannot be used as a column name. "
            f"Choose a different name (e.g. '{name}_val', '{name}_col').",
            identifier=name,
        )
    return name


def is_ref_dict(value: object) -> bool:
    """Check if a value is a reference dictionary.

    A reference dictionary has the form {"_table": str, "_id": int}.

    Args:
        value: Value to check.

    Returns:
        bool: True if value is a reference dictionary.
    """
    return isinstance(value, dict) and "_table" in value and "_id" in value


T = TypeVar("T")


class CircularReferenceError(Exception):
    """Raised when a circular reference is detected during relationship resolution."""

    def __init__(
        self,
        message: str,
        *,
        path: Optional[list[tuple[str, int]]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.path = path or []

    def __str__(self) -> str:
        if self.path:
            path_str = " -> ".join(f"{table}:{id_}" for table, id_ in self.path)
            return f"{self.message}: {path_str}"
        return self.message


class ReferenceTracker:
    """Track references during resolution to detect circular references.

    This class provides a context manager pattern for tracking which references
    have been visited during relationship resolution, preventing infinite loops.

    Usage::

        tracker = ReferenceTracker(max_depth=5)
        with tracker.visit("users", 1) as can_proceed:
            if can_proceed:
                # Safe to resolve this reference
                pass
            else:
                # Circular reference or max depth reached
                pass
    """

    def __init__(self, max_depth: int = 10, raise_on_cycle: bool = False):
        """Initialize the reference tracker.

        Args:
            max_depth: Maximum recursion depth allowed.
            raise_on_cycle: If True, raise CircularReferenceError on cycles.
        """
        self.max_depth = max_depth
        self.raise_on_cycle = raise_on_cycle
        self._visited: set[tuple[str, int]] = set()
        self._path: list[tuple[str, int]] = []
        self._depth = 0

    def visit(self, table: str, id_: int) -> "_ReferenceContext":
        """Create a context manager for visiting a reference.

        Args:
            table: Table name of the reference.
            id_: ID of the referenced row.

        Returns:
            _ReferenceContext: Context manager that tracks the visit.
        """
        return _ReferenceContext(self, table, id_)

    def is_visited(self, table: str, id_: int) -> bool:
        """Check if a reference has been visited.

        Args:
            table: Table name.
            id_: Row ID.

        Returns:
            bool: True if already visited.
        """
        return (table, int(id_)) in self._visited

    def at_max_depth(self) -> bool:
        """Check if maximum depth has been reached.

        Returns:
            bool: True if at max depth.
        """
        return self._depth >= self.max_depth

    def get_path(self) -> list[tuple[str, int]]:
        """Get the current resolution path.

        Returns:
            list[tuple[str, int]]: List of (table, id) tuples.
        """
        return list(self._path)

    def reset(self) -> None:
        """Reset the tracker state."""
        self._visited.clear()
        self._path.clear()
        self._depth = 0


class _ReferenceContext:
    """Context manager for tracking a single reference visit."""

    def __init__(self, tracker: ReferenceTracker, table: str, id_: int):
        self.tracker = tracker
        self.key = (table, int(id_))
        self._can_proceed = False

    def __enter__(self) -> bool:
        """Enter the context and check if we can proceed.

        Returns:
            bool: True if safe to resolve, False otherwise.
        """
        # Check max depth first
        if self.tracker._depth >= self.tracker.max_depth:
            logger.debug(
                f"Max depth {self.tracker.max_depth} reached at {self.key[0]}:{self.key[1]}"
            )
            return False

        # Check for cycle
        if self.key in self.tracker._visited:
            if self.tracker.raise_on_cycle:
                raise CircularReferenceError(
                    f"Circular reference detected at {self.key[0]}:{self.key[1]}",
                    path=self.tracker.get_path() + [self.key],
                )
            logger.debug(f"Circular reference avoided at {self.key[0]}:{self.key[1]}")
            return False

        # Safe to proceed
        self.tracker._visited.add(self.key)
        self.tracker._path.append(self.key)
        self.tracker._depth += 1
        self._can_proceed = True
        return True

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit the context and clean up tracking state."""
        if self._can_proceed:
            self.tracker._path.pop()
            self.tracker._depth -= 1
            # Note: we don't remove from _visited to prevent re-resolution
        return False


def collect_references(
    data: Any, refs: Optional[dict[str, set[int]]] = None
) -> dict[str, set[int]]:
    """Collect all references from a document or value.

    Args:
        data: The data to scan for references.
        refs: Optional dict to collect into (for recursive calls).

    Returns:
        dict[str, set[int]]: Mapping of table names to sets of referenced IDs.
    """
    if refs is None:
        refs = {}

    if is_ref_dict(data):
        table = data["_table"]
        id_ = data["_id"]
        if isinstance(table, str) and id_ is not None:
            refs.setdefault(table, set()).add(int(id_))
    elif isinstance(data, dict):
        for value in data.values():
            collect_references(value, refs)
    elif isinstance(data, list):
        for item in data:
            collect_references(item, refs)

    return refs


class ResolutionWarning:
    """Warning about an issue during relationship resolution."""

    def __init__(
        self,
        message: str,
        *,
        table: Optional[str] = None,
        id_: Optional[int] = None,
        path: Optional[list[tuple[str, int]]] = None,
    ):
        self.message = message
        self.table = table
        self.id_ = id_
        self.path = path or []

    def __str__(self) -> str:
        parts = [self.message]
        if self.table:
            parts.append(f"table={self.table}")
        if self.id_ is not None:
            parts.append(f"id={self.id_}")
        return " ".join(parts)


class ResolutionResult:
    """Result of relationship resolution with warnings and metadata."""

    def __init__(
        self,
        data: Any,
        *,
        warnings: Optional[list[ResolutionWarning]] = None,
        resolved_count: int = 0,
        skipped_count: int = 0,
    ):
        self.data = data
        self.warnings = warnings or []
        self.resolved_count = resolved_count
        self.skipped_count = skipped_count

    @property
    def has_warnings(self) -> bool:
        """Check if there were any warnings during resolution."""
        return len(self.warnings) > 0

    @property
    def success(self) -> bool:
        """Check if resolution completed without critical issues."""
        return True  # Currently always true; could be extended


def format_model_context(
    *,
    model: Optional[str] = None,
    table: Optional[str] = None,
    id_: Optional[int] = None,
    operation: Optional[str] = None,
) -> str:
    """Format context information for error messages.

    Args:
        model: Model class name.
        table: Table name.
        id_: Row ID.
        operation: Operation being performed (e.g., "save", "delete").

    Returns:
        str: Formatted context string.
    """
    parts = []
    if operation:
        parts.append(f"during {operation}")
    if model:
        parts.append(f"model={model}")
    if table:
        parts.append(f"table={table}")
    if id_ is not None:
        parts.append(f"id={id_}")
    return " ".join(parts) if parts else ""
