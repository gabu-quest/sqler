"""Async referential integrity utilities for SQLer models.

The rule of which rows refer to a row is :mod:`sqler.models.integrity`'s; these read and
write through the async adapter.
"""

import json
import sqlite3
from typing import TYPE_CHECKING

from sqler import registry
from sqler.models import _write_rules, integrity

# find_ref_paths stays importable from here, as it was
from sqler.models.integrity import (  # noqa: F401
    Read,
    Referrer,
    Rows,
    _by_table,
    _look,
    _nulled,
    find_ref_paths,
)
from sqler.utils import validate_table_name

if TYPE_CHECKING:
    from sqler.db.async_db import AsyncSQLerDB

_is_valid_table_name = integrity._is_valid_table_name


async def _aread(db: "AsyncSQLerDB", read: Read) -> Rows:
    table, sql, params = read
    try:
        cur = await db.adapter.execute(sql, params)
        try:
            return await cur.fetchall()  # SQLite reads the rows as they are fetched
        finally:
            await cur.close()
    except sqlite3.OperationalError as exc:
        return integrity.failed_read(exc, table)


async def async_find_referrers(db: "AsyncSQLerDB", target_table: str, target_id) -> list[Referrer]:
    """Async twin of :func:`sqler.models.integrity.find_referrers`: the same look, read
    through the async adapter.

    Args:
        db: The async database instance to search.
        target_table: Table name of the target rows.
        target_id: Row ``_id`` of the target, or a collection of them.

    Returns:
        List of tuples: ``(referring_table, referring_row_id, metadata)``.
        Metadata is a dict with ``"paths"`` listing JSON paths containing
        the reference.
    """
    look = _look(target_table, target_id)
    try:
        read = next(look)
        while True:
            read = look.send(await _aread(db, read))
    except StopIteration as done:
        return done.value


async def async_set_null_referrers(
    db: "AsyncSQLerDB", target_table: str, target_id, referrers: list[Referrer]
) -> None:
    """Async twin of :func:`sqler.models.integrity.set_null_referrers`.

    Args:
        db: The async database instance.
        target_table: Table name of the targets being deleted.
        target_id: Row ``_id`` of the target being deleted, or a collection of them.
        referrers: List of ``(table, row_id, metadata)`` from :func:`async_find_referrers`.
    """
    for table, _row_id, _meta in referrers:
        validate_table_name(table)
    null = _nulled(target_table, target_id)
    for table, row_id, _meta in referrers:
        cur = await db.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [row_id])
        row = await cur.fetchone()
        await cur.close()
        if row:
            await _write_rules.anull_referrer(db, table, row_id, null(table, json.loads(row[1])))


async def async_cascade_delete(
    db: "AsyncSQLerDB", referrers: list[Referrer], visited: set[tuple[str, int]]
) -> None:
    """Async twin of :func:`sqler.models.integrity.cascade_delete`.

    Args:
        db: The async database instance.
        referrers: List of ``(table, row_id, metadata)`` to delete.
        visited: Set of ``(table, row_id)`` already deleted or being deleted; it grows.
    """
    for table, _row_id, _ in referrers:
        validate_table_name(table)
    level = referrers
    while level:
        rows = _by_table(level, visited)
        for table, ids in rows.items():
            for row_id in ids:
                await _write_rules.adelete_referrer(db, table, row_id)
        level = [ref for table, ids in rows.items() for ref in await async_find_referrers(db, table, ids)]


async def async_validate_references(db: "AsyncSQLerDB"):
    """Validate all references across all tables in the database.

    Scans all registered tables and checks that every reference
    (``{"_table": ..., "_id": ...}``) points to an existing row.

    Args:
        db: The async database instance to validate.

    Returns:
        List of :class:`BrokenRef` instances describing dangling references.
    """
    from . import BrokenRef

    broken: list[BrokenRef] = []

    for table in registry.tables().keys():
        cur = await db.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table]
        )
        exists = await cur.fetchone()
        await cur.close()
        if not exists:
            continue

        cur = await db.adapter.execute(f"SELECT _id, data FROM {table};")
        rows = await cur.fetchall()
        await cur.close()

        for _id, data_json in rows:
            try:
                data = json.loads(data_json)
            except (json.JSONDecodeError, TypeError):
                # Skip rows with invalid JSON
                continue

            async def check_refs(value, path: str):
                if isinstance(value, dict) and "_table" in value and "_id" in value:
                    t = value.get("_table")
                    i = int(value.get("_id", -1))
                    # Validate table name to prevent SQL injection
                    if not isinstance(t, str) or not _is_valid_table_name(t):
                        # Invalid table name in reference - treat as broken
                        broken.append(
                            BrokenRef(
                                table=table,
                                row_id=int(_id),
                                path=path or "$",
                                target_table=str(t),
                                target_id=i,
                            )
                        )
                        return
                    # check existence
                    cur2 = await db.adapter.execute(f"SELECT 1 FROM {t} WHERE _id = ?;", [i])
                    row = await cur2.fetchone()
                    await cur2.close()
                    if not row:
                        broken.append(
                            BrokenRef(
                                table=table,
                                row_id=int(_id),
                                path=path or "$",
                                target_table=t,
                                target_id=i,
                            )
                        )
                    return
                if isinstance(value, dict):
                    for k, v in value.items():
                        await check_refs(v, f"{path}.{k}" if path else k)
                elif isinstance(value, list):
                    for idx, v in enumerate(value):
                        await check_refs(v, f"{path}[{idx}]")

            await check_refs(data, "")

    return broken
