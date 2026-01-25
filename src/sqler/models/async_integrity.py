"""Async referential integrity utilities for SQLer models.

This module provides async helper functions for managing referential integrity
across JSON documents stored in SQLite. It handles finding references,
enforcing deletion policies, and validating reference consistency.
"""

import json
import re
from typing import TYPE_CHECKING

from sqler import registry

if TYPE_CHECKING:
    from sqler.db.async_db import AsyncSQLerDB


def _is_valid_table_name(table: str) -> bool:
    """Check if a table name is valid (safe for SQL use).

    Args:
        table: Table name to validate.

    Returns:
        bool: True if the table name is valid.
    """
    return bool(re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table))


async def async_find_referrers(
    db: "AsyncSQLerDB", target_table: str, target_id: int
) -> list[tuple[str, int, dict]]:
    """Find all rows across all tables that reference the target row.

    Scans all registered tables for JSON documents containing references
    (``{"_table": ..., "_id": ...}``) pointing to the specified target.

    Args:
        db: The async database instance to search.
        target_table: Table name of the target row.
        target_id: Row ``_id`` of the target.

    Returns:
        List of tuples: ``(referring_table, referring_row_id, metadata)``.
        Metadata is a dict with ``"paths"`` listing JSON paths containing
        the reference.
    """
    candidates: list[tuple[str, int, dict]] = []
    # Use cached table name resolution for better performance
    allowed_tables = registry.get_allowed_tables(target_table)
    like_id = f'%"_id":{target_id}%'

    for table in registry.tables().keys():
        # skip tables not present in this DB
        cur = await db.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table]
        )
        exists = await cur.fetchone()
        await cur.close()
        if not exists:
            continue

        cur = await db.adapter.execute(
            f"SELECT _id, data FROM {table} WHERE data LIKE ?;",
            [like_id],
        )
        rows = await cur.fetchall()
        await cur.close()

        for _id, data_json in rows:
            try:
                data = json.loads(data_json)
            except (json.JSONDecodeError, TypeError):
                # Skip rows with invalid JSON
                continue
            paths = find_ref_paths(data, allowed_tables, target_id)
            if paths:
                candidates.append((table, int(_id), {"paths": paths}))

    return candidates


def find_ref_paths(data: dict, allowed_tables: set[str], target_id: int) -> list[str]:
    """Recursively find JSON paths containing references to a target row.

    Walks through a document searching for reference dictionaries (with
    ``_table`` and ``_id`` keys) that point to the specified target.

    Args:
        data: The document to search.
        allowed_tables: Set of table names that are valid reference targets.
        target_id: The row ``_id`` we're looking for.

    Returns:
        List of JSON paths (e.g., ``['$.author', '$.comments[0].user']``)
        where references to the target were found.
    """
    paths: list[str] = []

    def walk(value, path: str):
        if isinstance(value, dict):
            if value.get("_table") in allowed_tables and int(value.get("_id", -1)) == target_id:
                paths.append(path or "$")
            for k, v in value.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")

    walk(data, "")
    return paths


async def async_set_null_referrers(
    db: "AsyncSQLerDB",
    target_table: str,
    target_id: int,
    referrers: list[tuple[str, int, dict]],
) -> None:
    """Nullify all references to the target row in referring documents.

    For each referring row, replaces reference dictionaries pointing to
    the target with ``None`` before the target is deleted.

    Args:
        db: The async database instance.
        target_table: Table name of the target being deleted.
        target_id: Row ``_id`` of the target being deleted.
        referrers: List of ``(table, row_id, metadata)`` from
            :func:`async_find_referrers`.
    """
    # Use cached table name resolution for better performance
    allowed_tables = registry.get_allowed_tables(target_table)

    for table, row_id, meta in referrers:
        cur = await db.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [row_id])
        row = await cur.fetchone()
        await cur.close()
        if not row:
            continue
        obj = json.loads(row[1])

        def replace(value):
            if (
                isinstance(value, dict)
                and value.get("_table") in allowed_tables
                and int(value.get("_id", -1)) == target_id
            ):
                return None
            if isinstance(value, dict):
                return {k: replace(v) for k, v in value.items()}
            if isinstance(value, list):
                return [replace(v) for v in value]
            return value

        new_obj = replace(obj)
        payload = json.dumps(new_obj)
        await db.adapter.execute(
            f"UPDATE {table} SET data = json(?) WHERE _id = ?;", [payload, row_id]
        )
        await db.adapter.auto_commit()


async def async_cascade_delete(
    db: "AsyncSQLerDB",
    referrers: list[tuple[str, int, dict]],
    visited: set[tuple[str, int]],
) -> None:
    """Recursively delete referring rows and their dependents.

    Implements cascade deletion by recursively finding and deleting all
    rows that reference deleted rows, preventing orphaned references.

    Args:
        db: The async database instance.
        referrers: List of ``(table, row_id, metadata)`` to delete.
        visited: Set of ``(table, row_id)`` tuples already processed to
            prevent infinite loops.
    """
    for table, row_id, _ in referrers:
        key = (table, row_id)
        if key in visited:
            continue
        visited.add(key)
        # recurse
        kid_refs = await async_find_referrers(db, table, row_id)
        await async_cascade_delete(db, kid_refs, visited)
        await db.delete_document(table, row_id)


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
