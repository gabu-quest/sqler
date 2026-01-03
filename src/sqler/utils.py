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
