"""Global registry mapping table names to model classes.

This module provides a centralized registry for SQLer model classes,
allowing for efficient lookup and cross-table operations.
"""

from __future__ import annotations

import weakref
from functools import lru_cache
from typing import Optional

_REGISTRY: dict[str, type] = {}
# table -> every class whose rows may live in it: the class registered for it, each class
# defined or bound with it as its table (a subclass that inherits its base's table too), and
# each class read or written there through a queryset. Held weakly: a class nothing else
# holds leaves it.
_HOLDERS: dict[str, "weakref.WeakSet[type]"] = {}
_STATE = 0


def _hold(table: str, cls: type) -> bool:
    """Add ``cls`` to the classes of ``table``; True when it was not there."""
    held = _HOLDERS.get(table)
    if held is None:
        held = _HOLDERS[table] = weakref.WeakSet()
    elif cls in held:
        return False
    held.add(cls)
    return True


def _hold_at_own_table(cls: type) -> None:
    """Add ``cls`` to the classes of the table its rows go to now, if that is a valid name."""
    from sqler.exceptions import InvalidTableNameError
    from sqler.models._table_names import _model_table_name

    try:
        table = _model_table_name(cls)
    except InvalidTableNameError:
        return  # refused when it is first used: it writes no row
    _hold(table, cls)


def register(table: str, cls: type) -> None:
    """Register a model class for a given table name.

    ``cls`` becomes the class :func:`resolve` gives for ``table``, and one of the classes
    :func:`holders` gives for it. Each subclass of ``cls`` that inherits its table is held at
    the table its rows go to now, so a base bound to a table brings its subclasses there;
    a table a class was held at before stays (its rows are still there).

    Args:
        table: Table name to register.
        cls: Model class associated with the table.
    """
    global _STATE
    _REGISTRY.pop(table, None)  # a re-registered table moves to the end (latest wins)
    _REGISTRY[table] = cls
    _hold(table, cls)
    pending = list(type.__subclasses__(cls))
    while pending:
        sub = pending.pop()
        _hold_at_own_table(sub)
        pending.extend(type.__subclasses__(sub))
    _STATE += 1
    # Clear all caches when registry changes
    resolve.cache_clear()
    _get_allowed_tables.cache_clear()


def define(cls: type) -> None:
    """Note a newly defined model class: its rows go to its table (its own or the one it
    inherits), which :func:`holders` then gives it for."""
    global _STATE
    _hold_at_own_table(cls)
    _STATE += 1


def note(table: str, cls: type) -> None:
    """Note that rows of ``cls`` are read or written in ``table`` (a queryset on a table
    other than the class's own). Free when it is noted already."""
    global _STATE
    held = _HOLDERS.get(table)
    if held is not None and cls in held:
        return
    from sqler.exceptions import InvalidTableNameError
    from sqler.utils import validate_table_name

    try:
        validate_table_name(table)
    except InvalidTableNameError:
        return  # a query on it is refused: no row of it is read or written
    _hold(table, cls)
    _STATE += 1


def holders() -> dict[str, tuple[type, ...]]:
    """``{table: classes}``: for each table, every class whose rows may live in it, in no
    particular order."""
    return {table: tuple(held) for table, held in _HOLDERS.items() if len(held)}


@lru_cache(maxsize=128)
def resolve(table: str) -> Optional[type]:
    """Resolve a table name to its registered model class.

    Args:
        table: Table name to look up.

    Returns:
        The model class if registered, None otherwise.
    """
    return _REGISTRY.get(table)


def resolve_class_name(name: str) -> Optional[type]:
    """Return the registered model class called ``name`` (the latest one registered), or None.

    Used to turn a string annotation such as ``"Address"`` into its model class
    when the class's table name is not derived from the annotation text.
    """
    for cls in reversed(list(_REGISTRY.values())):
        if cls.__name__ == name:
            return cls
    return None


def state() -> int:
    """A number that changes with every :func:`register`, :func:`define` and new
    :func:`note`: an answer worked out from the registry holds while it is the same."""
    return _STATE


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
        from sqler.exceptions import InvalidTableNameError
        from sqler.models._table_names import _model_table_name

        allowed.add(target_cls.__name__.lower())
        alias = getattr(target_cls, "__tablename__", None)
        if isinstance(alias, str):
            allowed.add(alias)
        try:
            allowed.add(_model_table_name(target_cls))
        except InvalidTableNameError:
            pass  # a bad class table never named a stored reference
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
