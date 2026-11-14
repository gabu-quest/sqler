"""References between documents: the one walk every resolver uses.

A stored reference is a dict holding ``_table`` and ``_id``. Two resolvers follow them:

- the batch resolver behind ``query().all()``, ``first()`` and ``from_ids`` puts the
  stored documents in place of the references, read from the queryset's own database,
  level by level to a fixed depth;
- the per-row resolver behind ``from_id`` and ``refresh`` asks the class that owns each
  table and puts model instances in place.

Both collect every reference of what they are given first, so that the rows of one
table are read with one statement, then put what they loaded in place.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Generator, Optional

from sqler import registry
from sqler.db.async_db import _aselect_rows
from sqler.db.sqler_db import _in_order, _row_columns, _row_documents, _select_rows
from sqler.exceptions import InvalidTableNameError
from sqler.models._table_names import _aload_unbound, _load_unbound
from sqler.utils import validate_table_name

# A reference whose row could not be hydrated: it stays the reference it was.
KEEP = object()

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def ref_key(value: dict) -> Optional[tuple[str, int]]:
    """``(table, id)`` of a dict holding ``_table`` and ``_id``, or None if it is not usable.

    Only the shape is decided here: ``_table`` a string, ``_id`` an integer (not a bool,
    float, string or list) that fits SQLite's 64-bit INTEGER. A dict that is not usable is
    left as it is, and not walked into. The name is not checked: it is data we did not
    write, and :func:`batch` checks it once per table where it puts it into SQL.
    """
    table, rid = value["_table"], value["_id"]
    if not isinstance(table, str) or isinstance(rid, bool) or not isinstance(rid, int):
        return None
    if not _SQLITE_INT_MIN <= rid <= _SQLITE_INT_MAX:
        return None  # sqlite3 raises OverflowError when binding it
    return table, rid


def _is_table_name(table: str) -> bool:
    try:
        validate_table_name(table)
    except InvalidTableNameError:
        return False
    return True


def collect(value: Any, out: dict[tuple[str, int], None]) -> None:
    """Add every reference found in ``value`` (in order, once each) to ``out``."""
    if isinstance(value, dict):
        if "_table" in value and "_id" in value:
            key = ref_key(value)
            if key is not None:
                out[key] = None
            return
        for v in value.values():
            collect(v, out)
    elif isinstance(value, list):
        for v in value:
            collect(v, out)


def replace(value: Any, get: Callable[[tuple[str, int], Any], Any]) -> Any:
    """Put ``get(key, KEEP)`` in place of each reference; ``KEEP`` leaves it as it is."""
    if isinstance(value, dict):
        if "_table" in value and "_id" in value:
            key = ref_key(value)
            if key is None:
                return value
            got = get(key, KEEP)
            return value if got is KEEP else got
        return {k: replace(v, get) for k, v in value.items()}
    if isinstance(value, list):
        return [replace(v, get) for v in value]
    return value


# ---- the batch resolver -------------------------------------------------------------


def batch(
    docs: list[dict], max_depth: int
) -> Generator[tuple[str, list], Any, tuple[list[dict], bool]]:
    """Resolve the references of ``docs`` into stored documents, to ``max_depth`` levels.

    Yields ``(table, ids)`` for each read and is sent the documents :func:`read_rows`
    gives, or ``None`` when the table does not exist; one read per table per level. A table whose
    name is not an identifier is never read, and its references stay as they are.
    Returns ``(documents, found)``: ``found`` is False when no document holds a
    reference, and then the documents are ``docs`` itself; else they are new ones in
    which a reference that is being replaced further up stays a reference, a row that is
    gone stays a reference, and one into a missing table of a bound class reads as
    ``None``.
    """
    found: dict[tuple[str, int], None] = {}
    if max_depth > 0:
        for d in docs:
            collect(d, found)
    if not found:
        return docs, False
    resolved: dict[tuple[str, int], dict] = {}
    missing: set[str] = set()
    for _ in range(max_depth):
        wanted: dict[str, list[int]] = {}
        refused: set[str] = set()
        for table, rid in found:
            if (table, rid) in resolved or table in refused:
                continue
            ids = wanted.get(table)
            if ids is None:
                if not _is_table_name(table):
                    refused.add(table)
                    continue
                ids = wanted[table] = []
            ids.append(rid)
        if not wanted:
            break
        level: list[dict] = []
        for table, ids in wanted.items():
            got = yield table, ids
            if got is None:
                missing.add(table)
                continue
            for obj in got:
                resolved[(table, int(obj["_id"]))] = obj
                level.append(obj)
        found = {}
        for d in level:
            collect(d, found)

    processing: set[tuple[str, int]] = set()

    def expand(key: tuple[str, int], keep: Any) -> Any:
        if key in processing:
            return keep  # a cycle: the row is being replaced further up
        doc = resolved.get(key)
        if doc is None:
            return None if key[0] in missing and table_is_bound(key[0]) else keep
        processing.add(key)
        out = {k: replace(v, expand) for k, v in doc.items()}
        processing.discard(key)
        return out

    return [replace(d, expand) for d in docs], True


def read_rows(adapter: Any, table: str, ids: list, db: Any = None) -> Optional[list]:
    """The documents of the rows ``ids``, or ``None`` when ``table`` does not exist.

    Read through ``db``'s by-id reader, as ``db.find_documents`` reads them. Without a
    database that has one (a queryset built by hand, ``PooledSQLerDB``) ``adapter`` reads
    ``_id`` and ``data`` only.
    """
    reader = getattr(db, "_read_rows", None)
    if reader is not None:
        rows, columns = reader(table, ids)
    else:
        columns = _row_columns()
        rows = _select_rows(adapter, table, columns, ids)
    return None if rows is None else _row_documents(rows, columns)


async def aread_rows(adapter: Any, table: str, ids: list, db: Any = None) -> Optional[list]:
    """Async twin of :func:`read_rows`."""
    if db is not None:
        rows, columns = await db._read_rows(table, ids)
    else:
        columns = _row_columns()
        rows = await _aselect_rows(adapter, table, columns, ids)
    return None if rows is None else _row_documents(rows, columns)


# ---- the per-row resolver -----------------------------------------------------------


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


def resolve(data: dict, db: Any, seen: frozenset) -> dict:
    """Replace the references in ``data`` by model instances of the classes that own them.

    The rows of one table are read with one statement, by that class's ``_load_refs``. A
    reference to a row being loaded further up (``seen``) stays a reference, which cuts a
    cycle; a row that is gone reads as ``None``. ``data`` with nothing to load is
    returned as it is.
    """
    groups, unbound = plan(data, db, seen)
    if not groups and not unbound:
        return data
    loaded: dict = {}
    for mdl, table, rid in unbound:
        # a class never bound: read the row from the parent's db; only
        # a missing table or row is forgiven (it stays a reference)
        loaded[(table, rid)] = _load_unbound(mdl, db, table, rid)
    for table, (mdl, ids) in groups.items():
        loader = getattr(mdl, "_load_refs", None)
        found: dict = {}
        if loader is not None:
            try:
                found = loader(ids, seen)
            except (LookupError, RuntimeError, ValueError):
                found = dict.fromkeys(ids, KEEP)
        else:
            for rid in ids:
                try:
                    found[rid] = mdl.from_id(rid)
                except (LookupError, RuntimeError, ValueError):
                    found[rid] = KEEP
        for rid in ids:
            loaded[(table, rid)] = found.get(rid)
    return {k: replace(v, loaded.get) for k, v in data.items()}


async def aresolve(data: dict, db: Any, seen: frozenset) -> dict:
    """Async twin of :func:`resolve`, through each class's ``_aload_refs``."""
    groups, unbound = plan(data, db, seen)
    if not groups and not unbound:
        return data
    loaded: dict = {}
    for mdl, table, rid in unbound:
        loaded[(table, rid)] = await _aload_unbound(mdl, db, table, rid)
    for table, (mdl, ids) in groups.items():
        loader = getattr(mdl, "_aload_refs", None)
        found: dict = {}
        if loader is not None:
            try:
                found = await loader(ids, seen)
            except Exception:
                found = dict.fromkeys(ids, KEEP)
        else:
            for rid in ids:
                try:
                    fn = mdl.from_id
                    found[rid] = await fn(rid) if inspect.iscoroutinefunction(fn) else fn(rid)
                except Exception:
                    found[rid] = KEEP
        for rid in ids:
            loaded[(table, rid)] = found.get(rid)
    return {k: replace(v, loaded.get) for k, v in data.items()}


# ---- versioned rows for the safe bases ----------------------------------------------


def read_versioned(db: Any, table: str, ids: list) -> list:
    """Documents of ``ids`` with ``_version``, in the order of ``ids``, one statement; a
    missing table has no rows."""
    if not ids:
        return []
    rows, columns = db._read_rows(table, ids, versioned=True)
    return _row_documents(_in_order(rows or [], ids), columns)


async def aread_versioned(db: Any, table: str, ids: list) -> list:
    """Async twin of :func:`read_versioned`."""
    if not ids:
        return []
    rows, columns = await db._read_rows(table, ids, versioned=True)
    return _row_documents(_in_order(rows or [], ids), columns)


def table_is_bound(table: str) -> bool:
    """True when the class that owns ``table`` is bound to a database.

    A reference into a missing table of a bound class reads as ``None``, as it does when
    one row is read; for a class never bound the reference stays as it was.
    """
    mdl = registry.resolve(table)
    return mdl is not None and getattr(mdl, "_db", None) is not None
