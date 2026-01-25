"""Global registry mapping table names to model classes.

This module provides a centralized registry for SQLer model classes,
allowing for efficient lookup and cross-table operations.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

_REGISTRY: dict[str, type] = {}


def register(table: str, cls: type) -> None:
    """Register a model class for a given table name.

    Args:
        table: Table name to register.
        cls: Model class associated with the table.
    """
    _REGISTRY[table] = cls
    # Clear all caches when registry changes
    resolve.cache_clear()
    _get_allowed_tables.cache_clear()


@lru_cache(maxsize=128)
def resolve(table: str) -> Optional[type]:
    """Resolve a table name to its registered model class.

    Args:
        table: Table name to look up.

    Returns:
        The model class if registered, None otherwise.
    """
    return _REGISTRY.get(table)


def tables() -> dict[str, type]:
    """Return a copy of the table->class registry.

    Returns:
        Dictionary mapping table names to model classes.
    """
    return dict(_REGISTRY)


@lru_cache(maxsize=128)
def _get_allowed_tables(target_table: str) -> frozenset[str]:
    """Get all valid table name variants for a target table (cached).

    Args:
        target_table: The canonical table name.

    Returns:
        Frozenset of all valid table name variants.
    """
    target_cls = resolve(target_table)
    allowed: set[str] = {target_table}
    if target_cls is not None:
        allowed.add(target_cls.__name__.lower())
        alias = getattr(target_cls, "__tablename__", None)
        if isinstance(alias, str):
            allowed.add(alias)
    return frozenset(allowed)


def get_allowed_tables(target_table: str) -> set[str]:
    """Get all valid table name variants for a target table.

    This is useful for referential integrity checks where a table
    might be referenced by multiple names (class name, tablename, etc).

    Args:
        target_table: The canonical table name.

    Returns:
        Set of all valid table name variants.
    """
    return set(_get_allowed_tables(target_table))
