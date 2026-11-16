import re
from typing import Any, Optional, Union

from sqler.db._rows import _columns_sql, _layout, _promoted_in
from sqler.exceptions import SQLerError
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


# what ``create_index`` takes as its fields: one name, or a list of names and (name, direction)
IndexFields = Union[str, list[Union[str, tuple[str, str]]]]

# the direction of an index entry: whether it is descending
_DIRECTIONS = {"asc": False, "desc": True}


def _index_entries(fields: Any) -> list[tuple[str, bool]]:
    """The fields of ``create_index`` as ``(name, descending)``, each checked: ``fields`` is one
    name or a list whose entries are a name or ``(name, "asc" | "desc")`` (either word in any
    case). A name that starts with ``_`` is a column, any other a dotted path."""
    if isinstance(fields, str):
        given: list[Any] = [fields]
    elif isinstance(fields, list):
        given = fields
    else:
        raise TypeError(f"the fields of an index are a name or a list, not {type(fields).__name__}")
    if not given:
        raise ValueError("an index needs at least one field")
    entries: list[tuple[str, bool]] = []
    for entry in given:
        if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], str):
            field, direction = entry
            if direction.lower() not in _DIRECTIONS:
                raise ValueError(f"the direction of an index field is 'asc' or 'desc', not {direction!r}")
            descending = _DIRECTIONS[direction.lower()]
        elif isinstance(entry, str):
            field, descending = entry, False
        else:
            raise ValueError(f"an index field is a name or (name, 'asc' | 'desc'), not {entry!r}")
        if not isinstance(field, str):
            raise ValueError(f"an index field is a name or (name, 'asc' | 'desc'), not {entry!r}")
        validate_field_name(field) if not field.startswith("_") else validate_identifier(field)
        if any(field == seen for seen, _ in entries):
            raise ValueError(f"the field {field!r} is in the index twice")
        entries.append((field, descending))
    return entries


def _index_name(table: str, entries: list[tuple[str, bool]]) -> str:
    """The name of an index made without ``name=``: ``idx_<table>_<field>`` for one ascending
    field (its dots as ``_``), else each field so and ``_desc`` after a descending one, joined
    by ``__``, so two lists that differ in order or in a direction name two indexes."""
    if len(entries) == 1 and not entries[0][1]:
        return f"idx_{table}_{entries[0][0].replace('.', '_')}"
    return f"idx_{table}_" + "__".join(f.replace(".", "_") + ("_desc" if d else "") for f, d in entries)


def _create_index_sql(
    table: str, fields: Any, unique: bool, name: Optional[str], where: Optional[str], columns: Any = ()
) -> str:
    """The ``CREATE INDEX IF NOT EXISTS`` of ``create_index``, its arguments checked first.

    ``fields``: see :func:`_index_entries`. Each is indexed where the table stores it: a name
    that starts with ``_`` or is one of ``columns`` (the table's promoted columns) as that
    column, any other as ``json_extract(data, '$.<field>')``; ``DESC`` after a descending one.
    The name is :func:`_index_name` unless given; ``where`` makes it a partial index."""
    table = validate_table_name(table)
    entries = _index_entries(fields)
    if name is not None:
        validate_identifier(name)
    if where is not None:
        validate_check_expression(where)
    idx_name = name or _index_name(table, entries)
    unique_sql = "UNIQUE" if unique else ""
    expr = ", ".join(
        (f if f.startswith("_") or f in columns else f"json_extract(data, '$.{f}')") + (" DESC" if d else "")
        for f, d in entries
    )
    where_sql = f"WHERE {where}" if where else ""
    return f"CREATE {unique_sql} INDEX IF NOT EXISTS {idx_name} ON {table} ({expr}) {where_sql};"


def _index_steps(table: str, fields: Any, unique: bool, name: Optional[str], where: Optional[str]) -> Any:
    """``create_index`` as a run of statements (``SQLerDB._run``), in a transaction that holds
    the write lock: read where ``table`` stores each field now, then make the index there. A
    promotion that comes later re-makes it (:func:`_follow_indexes`)."""
    _, names = _layout((yield _columns_sql(table), []))
    yield _create_index_sql(table, fields, unique, name, where, _promoted_in(names)), []


# what SQLite keeps of a ``CREATE INDEX`` sqler sent: its head, then each indexed term
_MADE_HEAD = re.compile(r"CREATE (UNIQUE )?INDEX ([A-Za-z_][A-Za-z0-9_]*) ON ([A-Za-z_][A-Za-z0-9_]*) \(")
_MADE_TERM = re.compile(r"(?:json_extract\(data, '\$\.([A-Za-z0-9_.]+)'\)|([A-Za-z_][A-Za-z0-9_]*))( DESC)?(, |\) )")


def _kept_text(statement: str) -> str:
    """The text ``sqlite_master`` keeps for a ``CREATE INDEX IF NOT EXISTS`` sent: ``CREATE``,
    ``UNIQUE`` when given and ``INDEX`` one space apart, then the statement from the index name
    on, as sent, without its ``;``."""
    head, _, tail = statement.partition(" IF NOT EXISTS ")
    return " ".join(head.split()) + " " + tail[:-1]


def _made_by_sqler(sql: str) -> Optional[tuple[bool, str, str, list[Any], Optional[str], set[str]]]:
    """The arguments of the ``create_index`` whose statement SQLite keeps as ``sql``: unique,
    name, table, fields, where, and the fields it indexed as columns; ``None`` when ``sql`` is
    not, byte for byte, a text :func:`_create_index_sql` makes (an index written by hand)."""
    head = _MADE_HEAD.match(sql)
    if head is None:
        return None
    fields: list[Any] = []
    columns: set[str] = set()
    pos = head.end()
    while True:
        term = _MADE_TERM.match(sql, pos)
        if term is None:
            return None
        path, column, desc, end = term.groups()
        if column is not None:
            columns.add(column)
        fields.append((path or column, "desc") if desc else (path or column))
        pos = term.end()
        if end == ") ":
            break
    rest = sql[pos:]
    if rest and not rest.startswith("WHERE "):
        return None
    unique, name, table = bool(head.group(1)), head.group(2), head.group(3)
    where = rest[len("WHERE "):] or None
    try:
        statement = _create_index_sql(table, fields, unique, name, where, columns)
    except (SQLerError, ValueError, TypeError):
        return None
    return (unique, name, table, fields, where, columns) if _kept_text(statement) == sql else None


def _follow_indexes(table: str, moved: list[str], promoted: list[str]) -> Any:
    """After the values of ``moved`` left the documents of ``table`` for their columns: each
    index sqler made on ``table`` (:func:`_made_by_sqler`) that reads one of them from ``data``
    is made again, under its name, with its ``unique`` and its ``where`` as written, reading
    each field where the table stores it now (``promoted``: every promoted column of it now).

    A run of statements the database classes send (``sqler.db._promotion._promotion``), in the
    transaction of the promotion: an index made before it would index NULL for a moved field."""
    rows = yield "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? COLLATE NOCASE AND sql IS NOT NULL;", [table]
    moving = set(moved)
    for (sql,) in rows:
        made = _made_by_sqler(sql)
        if made is None:
            continue
        unique, name, made_on, fields, where, columns = made
        if not any((f[0] if isinstance(f, tuple) else f) in moving - columns for f in fields):
            continue
        yield f"DROP INDEX {name};", []
        yield _create_index_sql(made_on, fields, unique, name, where, promoted), []


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
