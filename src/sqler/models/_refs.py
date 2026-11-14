"""Walking a document for references, shared by the four relation resolvers.

A resolver collects every reference in a document first, so that the rows of one table
are read with one statement instead of one statement per reference, then puts what it
loaded in place of the references.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from sqler import registry
from sqler.models._table_names import _is_missing_table

# A reference whose row could not be hydrated: it stays the reference it was.
KEEP = object()


def ref_key(value: object) -> Optional[tuple[str, int]]:
    """``(table, id)`` of a reference dict a resolver can follow, else ``None``."""
    if not isinstance(value, dict) or "_table" not in value or "_id" not in value:
        return None
    table, rid = value["_table"], value["_id"]
    if isinstance(table, str) and isinstance(rid, int) and not isinstance(rid, bool):
        return table, rid
    return None


def collect(value: Any, out: dict[tuple[str, int], None]) -> None:
    """Add every reference found in ``value`` (in order, once each) to ``out``."""
    if isinstance(value, dict):
        key = ref_key(value)
        if key is not None:
            out.setdefault(key, None)
            return
        for v in value.values():
            collect(v, out)
    elif isinstance(value, list):
        for v in value:
            collect(v, out)


def plan(data: dict, db: Any, seen: frozenset) -> tuple[dict, list]:
    """Sort the references of ``data`` into what to load.

    Returns ``(groups, unbound)``: ``groups`` maps a table to ``(model, ids)`` for classes
    that are bound; ``unbound`` lists ``(model, table, id)`` for classes never bound,
    which are read from the parent's database. A reference already being loaded further
    up (``seen``), or to a table no class owns, is left out and stays a reference.
    """
    found: dict[tuple[str, int], None] = {}
    collect(data, found)
    groups: dict[str, tuple[Any, list[int]]] = {}
    unbound: list[tuple[Any, str, int]] = []
    for table, rid in found:
        if (table, rid) in seen:
            continue
        mdl = registry.resolve(table)
        if mdl is None:
            continue
        if getattr(mdl, "_db", None) is None and db is not None:
            unbound.append((mdl, table, rid))
        elif hasattr(mdl, "from_id"):
            groups.setdefault(table, (mdl, []))[1].append(rid)
    return groups, unbound


def replace(value: Any, loaded: dict[tuple[str, int], Any]) -> Any:
    """Put the loaded rows in place of their references."""
    if isinstance(value, dict):
        key = ref_key(value)
        if key is not None:
            got = loaded.get(key, KEEP)
            return value if got is KEEP else got
        return {k: replace(v, loaded) for k, v in value.items()}
    if isinstance(value, list):
        return [replace(v, loaded) for v in value]
    return value


def settle(table: str, ids: list[int], found: dict[int, Any], loaded: dict) -> None:
    """Record the result of loading ``ids``: a row that is gone reads as ``None``."""
    for rid in ids:
        got = found.get(rid, None)
        loaded[(table, rid)] = got


def _versioned_sql(db: Any, table: str, ids: list) -> tuple[str, list]:
    promoted = list(db._promoted_columns.get(table, []))
    cols = ", ".join(["_id", "data", "_version", *promoted])
    marks = ",".join("?" for _ in ids)
    return f"SELECT {cols} FROM {table} WHERE _id IN ({marks});", promoted


def _versioned_doc(row: Any, promoted: list) -> dict:
    obj = json.loads(row[1])
    obj["_id"] = row[0]
    obj["_version"] = row[2]
    for i, col in enumerate(promoted):
        obj[col] = row[3 + i]
    return obj


def read_versioned(db: Any, table: str, ids: list) -> list:
    """Documents of ``ids`` with ``_version``, one statement; a missing table has no rows."""
    if not ids:
        return []
    sql, promoted = _versioned_sql(db, table, ids)
    try:
        cur = db.adapter.execute(sql, list(ids))
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc, table):
            return []
        raise
    return [_versioned_doc(row, promoted) for row in cur.fetchall()]


async def aread_versioned(db: Any, table: str, ids: list) -> list:
    """Async twin of :func:`read_versioned`."""
    if not ids:
        return []
    sql, promoted = _versioned_sql(db, table, ids)
    try:
        cur = await db.adapter.execute(sql, list(ids))
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc, table):
            return []
        raise
    try:
        rows = await cur.fetchall()
    finally:
        await cur.close()
    await db.adapter.auto_commit()
    return [_versioned_doc(row, promoted) for row in rows]


def table_is_bound(table: str) -> bool:
    """True when the class that owns ``table`` is bound to a database.

    A reference into a missing table of a bound class reads as ``None``, as it does when
    one row is read; for a class never bound the reference stays as it was.
    """
    mdl = registry.resolve(table)
    return mdl is not None and getattr(mdl, "_db", None) is not None
