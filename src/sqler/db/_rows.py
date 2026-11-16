import functools
import json
import sqlite3
from typing import Any, Optional, Sequence

from sqler.db._guard import _and_guard
from sqler.exceptions import (
    InvalidIdentifierError,
)
from sqler.query.query import end_failed, is_missing_table
from sqler.utils import (
    validate_column_name,
)

# ---- a stored row read by its id: the one column list and the one row-to-document rule ----


_NOT_PROMOTED = frozenset({"_id", "data", "_version"})


def _promoted_in(names: Any) -> list[str]:
    """The promoted columns among a table's column ``names``: every column but ``_id``,
    ``data`` and ``_version`` whose name sqler accepts for a promoted column. A column
    added by hand under another name is never read."""
    out = []
    for name in names:
        if name.lower() in _NOT_PROMOTED:
            continue
        try:
            validate_column_name(name)
        except InvalidIdentifierError:
            continue
        out.append(name)
    return out


def _columns_sql(table: str) -> str:
    """The statement that lists the columns of the validated ``table``, generated ones too
    (:func:`_layout`): no rows when it is missing."""
    return f'PRAGMA table_xinfo("{table}");'


def _layout(rows: Any) -> tuple[tuple[str, ...], list[str]]:
    """The rows of :func:`_columns_sql` as the names ``SELECT *`` gives (all but a hidden
    column of a virtual table) and the names of the stored columns, those a statement can
    write (a generated column is not one)."""
    return tuple(row[1] for row in rows if row[6] != 1), [row[1] for row in rows if row[6] == 0]


def _row_columns(promoted: Any = (), versioned: bool = False) -> tuple[str, ...]:
    """The columns a stored row is read with: ``_id``, ``data``, ``_version`` for a versioned
    read, then the promoted columns."""
    return ("_id", "data", "_version", *promoted) if versioned else ("_id", "data", *promoted)


# The most variables one statement binds: the bound the bulk inserts chunk by
# (``_insert_many_chunked``), SQLite's historic default limit of host variables.
_MAX_VARIABLES = 999


@functools.lru_cache(maxsize=512)
def _rows_sql(table: str, columns: tuple[str, ...], n: int, names: Optional[tuple[str, ...]] = None) -> str:
    """The statement that reads ``n`` ids of the validated ``table`` with ``columns``, one
    variable per id; for ``n == 0`` the ids come as one JSON array in one variable; with the
    guard of the remembered ``names`` (:func:`_guard`) when given. Built once per shape, so a
    one-id read builds no text."""
    ids = "SELECT value FROM json_each(?)" if n == 0 else ",".join("?" * n)
    return f"SELECT {', '.join(columns)} FROM {table} WHERE _id IN ({ids}){_and_guard(table, names)};"


def _row_documents(rows: Any, columns: Sequence[str]) -> list[dict[str, Any]]:
    """Each row read with ``columns`` as a document: its JSON, then every column but ``data``
    set on it by name (a column wins over a JSON key of its name)."""
    loads = json.loads
    docs = []
    for row in rows:
        doc = loads(row[1])
        doc["_id"] = row[0]
        docs.append(doc)
    extra = columns[2:]
    if extra:
        for doc, row in zip(docs, rows):
            doc.update(zip(extra, row[2:]))
    return docs


def _in_order(rows: Any, ids: list) -> list:
    """The rows of ``ids`` in the order of ``ids``: an id asked twice gives its row twice, an
    id with no row gives nothing."""
    by_id = {row[0]: row for row in rows}
    return [by_id[i] for i in ids if i in by_id]


def _id_params(ids: list) -> tuple[int, list]:
    """The ``n`` of :func:`_rows_sql` and the variables for ``ids``: one per id up to
    ``_MAX_VARIABLES``, past it the whole list as one JSON array, so a read is one statement
    whatever the number of ids."""
    if len(ids) <= _MAX_VARIABLES:
        return len(ids), ids
    return 0, [json.dumps(ids)]


def _select_rows(adapter: Any, table: str, columns: tuple[str, ...], ids: list) -> Optional[list]:
    """The rows of ``ids`` read with ``columns``, in stored order; ``None`` when ``table`` is
    missing (a read creates no table)."""
    n, params = _id_params(ids)
    sql = _rows_sql(table, columns, n)
    try:
        try:
            return adapter._execute_judged(sql, params).fetchall()
        except Exception as exc:
            end_failed(adapter, exc, table, sql)
    except sqlite3.OperationalError as exc:
        if is_missing_table(exc, table):
            return None
        raise
