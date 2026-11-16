from typing import Any, Optional

from sqler.utils import (
    validate_check_expression,
    validate_column_def,
    validate_column_name,
    validate_field_name,
    validate_identifier,
    validate_table_name,
)

# ---- the indexes, a delete by id, and the checks of an ensure: the statements both classes send ----


_INDEX_EXISTS_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?;"

_INDEXES_OF_SQL = """
    SELECT name, tbl_name, sql
    FROM sqlite_master
    WHERE type = 'index' AND tbl_name = ? AND name NOT LIKE 'sqlite_%'
    ORDER BY name;
"""

_ALL_INDEXES_SQL = """
    SELECT name, tbl_name, sql
    FROM sqlite_master
    WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
    ORDER BY tbl_name, name;
"""


def _indexes_sql(table: Optional[str]) -> tuple[str, list[Any]]:
    """The statement ``list_indexes`` sends and its parameters: the indexes of ``table``, or
    of every table when it is empty; SQLite's own indexes are left out."""
    return (_INDEXES_OF_SQL, [table]) if table else (_ALL_INDEXES_SQL, [])


def _index_rows(rows: Any) -> list[dict[str, Any]]:
    """The rows of :func:`_indexes_sql` as ``list_indexes`` returns them."""
    indexes = []
    for row in rows:
        sql = row[2] or ""
        indexes.append({"name": row[0], "table": row[1], "sql": sql, "unique": "UNIQUE" in sql.upper() if sql else False})
    return indexes


def _create_index_sql(table: str, field: str, unique: bool, name: Optional[str], where: Optional[str]) -> str:
    """The ``CREATE INDEX IF NOT EXISTS`` of ``create_index``, its arguments checked first: a
    dotted ``field`` is a JSON path in ``data``, one that starts with ``_`` a column; the name
    is ``idx_<table>_<field>`` unless given; ``where`` makes it a partial index."""
    validate_field_name(field) if not field.startswith("_") else validate_identifier(field)
    if name is not None:
        validate_identifier(name)
    if where is not None:
        validate_check_expression(where)
    idx_name = name or f"idx_{table}_{field.replace('.', '_')}"
    unique_sql = "UNIQUE" if unique else ""
    expr = f"json_extract(data, '$.{field}')" if not field.startswith("_") else field
    where_sql = f"WHERE {where}" if where else ""
    return f"CREATE {unique_sql} INDEX IF NOT EXISTS {idx_name} ON {table} ({expr}) {where_sql};"


def _drop_index_sql(name: str) -> str:
    """The ``DROP INDEX IF EXISTS`` of ``drop_index``, its name checked first."""
    validate_identifier(name)
    return f"DROP INDEX IF EXISTS {name};"


def _delete_sql(table: str) -> str:
    """The delete of one row of ``table`` by its ``_id``."""
    return f"DELETE FROM {table} WHERE _id = ?;"


def _row_there_sql(table: str) -> str:
    """The read that tells whether ``table`` has the row of an ``_id``: a checked write that
    wrote nothing tells a gone row (``NotFoundError``) from one at another version."""
    return f"SELECT 1 FROM {table} WHERE _id = ?;"


def _checked_promotion(table: str, promoted: dict[str, str], checks: Optional[dict[str, str]]) -> tuple[str, dict[str, str]]:
    """The arguments of an ensure with promoted columns, checked before anything is sent: the
    table name, each column name and definition, each CHECK expression. Returns the table
    name and the checks (none: empty)."""
    table = validate_table_name(table)
    checks = checks or {}
    for col_name, col_def in promoted.items():
        validate_column_name(col_name)
        validate_column_def(col_def)
    for chk_expr in checks.values():
        validate_check_expression(chk_expr)
    return table, checks


def _keep_word(keep: Any) -> str:
    if keep not in ("document", "column"):
        raise ValueError(f"keep must be 'document' or 'column', not {keep!r}")
    return keep
