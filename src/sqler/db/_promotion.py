import dataclasses
import re
import sqlite3
import sys
from typing import Any

from sqler._fts_sql import _follow_promotion
from sqler._stored import stored
from sqler.db._rows import _columns_sql, _layout, _promoted_in
from sqler.db._statements import _follow_indexes
from sqler.db._writes import _json_path
from sqler.exceptions import (
    SchemaError,
    TableNotFoundError,
    UnstorableValueError,
)
from sqler.query.query import _has_version, is_missing_table

# ---- a promoted column: the statements that make it, move the values into it, and record it ----
#
# Each function below that ``yield``s is a run of statements both database classes send the
# same way (``SQLerDB._run`` and its async twin): it yields ``(sql, params)``, gets back the
# rows read, or the ``sqlite3.OperationalError`` the statement raised, and returns its answer.


# The promoted columns of each table that hold their values: no row's document holds their key.
# A table of sqler's own (``SQLER_TABLES``), beside the tables, as the migrations table is.
_SETTLED = "_sqler_promoted"
_SETTLED_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_SETTLED} (tbl TEXT NOT NULL COLLATE NOCASE, "
    "col TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY (tbl, col)) WITHOUT ROWID;"
)

# a column definition ``validate_column_def`` accepted: TYPE [NOT NULL] [DEFAULT <literal>]
_DEFINITION = re.compile(
    r"\s*\w+(?:\s*\([^)]*\))?(\s+NOT\s+NULL)?(?:\s+DEFAULT\s+(.+?))?\s*$", re.IGNORECASE | re.DOTALL
)

_NO_FILL = object()  # a row without the key keeps what ADD COLUMN gave it: the DEFAULT, or NULL
_KEEP = object()  # the column keeps its values; only the key leaves the documents
_UNFILLABLE = object()  # a default no column holds: a list, a dict, a value with no stored form


def _fill_of(model: Any, name: str) -> Any:
    """What a row without the key ``name`` reads through ``model``, in stored form: its field's
    default; ``_NO_FILL`` without a model, a field or a default; ``_UNFILLABLE`` when no column
    holds that default."""
    if model is None:
        return _NO_FILL
    fields = getattr(model, "__dataclass_fields__", None)
    if fields is not None:  # a lite class
        f = fields.get(name)
        if f is None or (
            f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        ):
            return _NO_FILL
        value = f.default_factory() if f.default is dataclasses.MISSING else f.default
    elif hasattr(model, "__struct_fields__"):  # a msgspec class
        msgspec = sys.modules["msgspec"]
        f = next((f for f in msgspec.structs.fields(model) if f.name == name), None)
        if f is None or (f.default is msgspec.NODEFAULT and f.default_factory is msgspec.NODEFAULT):
            return _NO_FILL
        value = f.default_factory() if f.default is msgspec.NODEFAULT else f.default
    else:
        f = model.model_fields.get(name)
        if f is None or f.is_required():
            return _NO_FILL
        if getattr(f, "default_factory_takes_validated_data", False):  # pydantic 2.10 and later
            return _UNFILLABLE  # worked out from the row's other fields: no one value for a column
        value = f.get_default(call_default_factory=True)
    try:
        value = stored(value, field=name)
    except UnstorableValueError:
        return _UNFILLABLE
    return _UNFILLABLE if isinstance(value, (list, dict)) else value


def _tally_sql(table: str, columns: list[str], conditions: list[str]) -> tuple[str, list[Any]]:
    """One pass over ``table`` that counts, for each of ``columns``, the rows that meet its
    condition, and finds the first of them. A condition reads ``t<i>``, the JSON type of the
    i-th column's key in the document (NULL: no key), and ``v<i>``, the SQL type of its value."""
    picks = ", ".join(
        f"json_type(data, ?) AS t{i}, typeof(json_extract(data, ?)) AS v{i}"
        for i in range(len(columns))
    )
    tallies = ", ".join(
        f"COUNT(CASE WHEN {c} THEN 1 END), MIN(CASE WHEN {c} THEN _id END)" for c in conditions
    )
    params = [p for col in columns for p in (_json_path(col),) * 2]
    return f"SELECT {tallies} FROM (SELECT _id, {picks} FROM {table});", params


def _unkept(i: int, not_null: bool, without_key: bool) -> str:
    """The rows whose value the i-th column cannot take as the row reads it: a list, a dict or a
    reference; an integer SQLite holds only as a float; JSON null in a NOT NULL column; and with
    ``without_key``, no key (the field's default does not fit the column)."""
    sql = f"t{i} IN ('array', 'object') OR (t{i} = 'integer' AND v{i} <> 'integer')"
    if not_null:
        sql += f" OR t{i} = 'null'"
    return sql + (f" OR t{i} IS NULL" if without_key else "")


def _move_sql(table: str, fills: dict[str, Any], versioned: bool = False) -> tuple[str, list[Any]]:
    """The one ``UPDATE`` that takes each column's key out of every document and puts its value
    in the column: JSON null as NULL, never the column's DEFAULT. A row without the key gets the
    column's fill (``_NO_FILL``: what it holds; ``_KEEP``: the column is not written).
    ``versioned`` (the table has ``_version``): a row whose read the move changes, a key's value
    put into a ``_NO_FILL`` column that held another, has its version moved by one."""
    sets, params = [], []
    for col, fill in fills.items():
        if fill is _KEEP:
            continue
        absent = col if fill is _NO_FILL else "?"
        sets.append(
            f"{col} = CASE WHEN json_type(data, ?) IS NULL THEN {absent} ELSE json_extract(data, ?) END"
        )
        params += [_json_path(col)] if fill is _NO_FILL else [_json_path(col), fill]
        params.append(_json_path(col))
    moved = [col for col, fill in fills.items() if fill is _NO_FILL] if versioned else []
    if moved:
        same = " AND ".join(
            f"(json_type(data, ?) IS NULL OR {col} IS json_extract(data, ?))" for col in moved
        )
        sets.append(f"_version = CASE WHEN {same} THEN _version ELSE _version + 1 END")
        params += [_json_path(col) for col in moved for _ in range(2)]
    paths = [_json_path(col) for col in fills]
    sets.append(f"data = json_remove(data{', ?' * len(paths)})")
    sql = f"UPDATE {table} SET {', '.join(sets)}"
    if all(
        fill is _NO_FILL or fill is _KEEP for fill in fills.values()
    ):  # only the rows with a key change
        sql += " WHERE " + " OR ".join(["json_type(data, ?) IS NOT NULL"] * len(paths))
        paths *= 2
    return sql + ";", params + paths


def _refused(table: str, column: str, rows: int, first_id: int) -> SchemaError:
    return SchemaError(
        f"Cannot promote {table}.{column}: {rows} row(s) hold a value the column cannot keep as the row "
        "reads it (a list, a dict or a reference; an integer past 64 bits; null, or no key and no "
        f"default the column holds, in a NOT NULL column); the first is _id {first_id}. Nothing was changed.",
        details={"table": table, "column": column, "rows": rows, "first_id": first_id},
    )


def _settled(table: Any, names: list[str]) -> Any:
    """Read which promoted columns of ``table``, whose columns are ``names``, hold their values.
    A column the database does not record as settled is read for rows whose document still
    holds its key, as an earlier version of sqler left them: none, and it is settled; some, and
    ``SchemaError`` names them, because a row does not say whether its column or its document
    is newer. Returns the columns to record."""
    promoted = _promoted_in(names)
    if not promoted:
        return []
    try:
        rows = yield f"SELECT col FROM {_SETTLED} WHERE tbl = ?;", [table]
    except sqlite3.OperationalError as exc:
        if not is_missing_table(exc, _SETTLED):
            raise
        rows = []
    settled = {row[0].lower() for row in rows}
    unsettled = [col for col in promoted if col.lower() not in settled]
    if not unsettled:
        return []
    (row,) = yield _tally_sql(
        table, unsettled, [f"t{i} IS NOT NULL" for i in range(len(unsettled))]
    )
    held = {
        c: {"rows": row[2 * i], "first_id": row[2 * i + 1]}
        for i, c in enumerate(unsettled)
        if row[2 * i]
    }
    if held:
        found = "; ".join(
            f"{c}: {h['rows']} row(s), the first _id {h['first_id']}" for c, h in held.items()
        )
        raise SchemaError(
            f"Table {table} has columns beside data whose key is still in the documents of some rows "
            f"({found}): a read gives the column's value, not the document's. An earlier version of "
            "sqler left them so when it added a column, and so does a column added by other means. "
            "Whether the column or the document is newer cannot be told from the row: settle each with "
            f"db.move_promoted({table!r}, {next(iter(held))!r}, keep='document') (the document's value "
            "goes into the column) or keep='column' (the column keeps its value); either takes the "
            "key out of the documents.",
            details={"table": table, "columns": held},
        )
    return unsettled


def _record(table: str, columns: list[str], *, made: bool = False) -> Any:
    """Record ``columns`` of ``table`` as holding their values; ``made``: the table is being
    made now, and what is recorded under its name (a table of that name dropped by plain SQL)
    is not its own."""
    if not columns:
        return
    yield _SETTLED_DDL, []
    if made:
        yield f"DELETE FROM {_SETTLED} WHERE tbl = ?;", [table]
    yield (
        f"INSERT OR IGNORE INTO {_SETTLED} (tbl, col) VALUES {', '.join(['(?, ?)'] * len(columns))};",
        [v for col in columns for v in (table, col)],
    )


def _promotion(table: str, promoted: dict[str, str], checks: dict[str, str], model: Any) -> Any:
    """Under the write lock: create ``table`` with its ``promoted`` columns, or add the missing
    ones to it and move each row's value from its document into its column (a row without the
    key gets what it read through ``model``), and follow its search indexes and the indexes sqler made on it.
    Returns its promoted columns and the names ``SELECT *`` gives for it then.

    Nothing is changed when a row's value cannot be kept (``SchemaError``)."""
    layout, names = _layout((yield _columns_sql(table), []))
    if not names:
        defs = [f"{col} {d}" for col, d in promoted.items()] + [
            f"CHECK ({c})" for c in checks.values()
        ]
        yield from _record(table, list(promoted), made=True)
        yield (
            f"CREATE TABLE IF NOT EXISTS {table} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL{''.join(', ' + d for d in defs)});",
            [],
        )
        return list(promoted), ("_id", "data", *promoted)
    added = {col: d for col, d in promoted.items() if col not in names}
    if added:
        fills, conditions = {}, []
        for i, (col, definition) in enumerate(added.items()):
            matched = _DEFINITION.match(definition)
            assert (
                matched is not None
            )  # ``validate_column_def`` accepted it, and its grammar is a part of this one
            not_null, default = matched.groups()
            if not_null and (default is None or default.upper() == "NULL"):
                raise SchemaError(
                    f"Cannot promote {table}.{col}: SQLite cannot add a NOT NULL column with no DEFAULT "
                    "to an existing table. Give it a DEFAULT, or leave out NOT NULL. Nothing was changed.",
                    details={"table": table, "column": col},
                )
            fill = _fill_of(model, col)
            # a row without the key would read a value the column cannot hold
            without_key = fill is _UNFILLABLE or (fill is None and bool(not_null))
            fills[col] = _NO_FILL if without_key else fill
            conditions.append(_unkept(i, bool(not_null), without_key))
        (row,) = yield _tally_sql(table, list(added), conditions)
        for i, col in enumerate(added):
            if row[2 * i]:
                raise _refused(table, col, row[2 * i], row[2 * i + 1])
    columns = _promoted_in(names) + list(added)
    yield from _record(table, columns)
    if added:
        for col, definition in added.items():
            yield f'ALTER TABLE "{table}" ADD COLUMN {col} {definition};', []
        yield _move_sql(table, fills)
        yield from _follow_promotion(table, list(added), columns)
        yield from _follow_indexes(table, list(added), columns)
    return columns, (*layout, *added)


def _versioning(table: str) -> Any:
    """Under the write lock: read the names of ``table`` and add its ``_version`` column when
    it lacks it. Returns the names ``SELECT *`` gave before, its stored columns, and the
    columns added (``("_version",)`` or none), as ``_TableMemory._seen`` takes them."""
    layout, names = _layout((yield _columns_sql(table), []))
    if _has_version(layout):
        return layout, names, ()
    yield f'ALTER TABLE "{table}" ADD COLUMN "_version" INTEGER NOT NULL DEFAULT 0;', []
    return layout, names, ("_version",)


def _moved(table: str, column: str, keep: str) -> Any:
    """``move_promoted``: take ``column``'s key out of every document of ``table``, its value going
    into the column (``keep="document"``) or not (``keep="column"``), and record the column."""
    info = {row[1]: row for row in (yield _columns_sql(table), []) if row[6] == 0}
    if not info:
        raise TableNotFoundError(f"Table {table} does not exist", table=table)
    if column not in _promoted_in(info):
        raise SchemaError(
            f"{column} is not a promoted column of {table}",
            details={"table": table, "column": column},
        )
    if keep == "document":
        (row,) = yield _tally_sql(table, [column], [_unkept(0, bool(info[column][3]), False)])
        if row[0]:
            raise _refused(table, column, row[0], row[1])
    yield from _record(table, [column])
    yield _move_sql(
        table, {column: _NO_FILL if keep == "document" else _KEEP}, _has_version(tuple(info))
    )
    yield from _follow_promotion(table, [column], _promoted_in(info))
    yield from _follow_indexes(table, [column], _promoted_in(info))


def _unwritable(exc: sqlite3.OperationalError) -> bool:
    """Whether ``exc`` says the database takes no write now: opened read-only or ``query_only``,
    or another connection holds the write lock past the busy timeout."""
    code = getattr(exc, "sqlite_errorcode", None)  # an error sqler raised itself carries none
    return code is not None and (code & 0xFF) in (
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


def _definition(row: Any) -> str:
    """The definition of a stored column, from its row of :func:`_columns_sql`, as
    ``ensure_table`` takes it: its type, ``NOT NULL``, ``DEFAULT`` as SQLite keeps it."""
    _, _, kind, not_null, default = row[:5]
    return (
        f"{kind}{' NOT NULL' if not_null else ''}{'' if default is None else f' DEFAULT {default}'}"
    )


def _copy_layout(source: str, target: str) -> Any:
    """What ``copy_to`` must make of ``target`` for the rows of ``source``, both validated, to
    go into it as they are stored: ``None`` when ``source`` is missing (there is nothing to
    copy and nothing is made), else ``(made, promoted, versioned)``: whether ``target`` is to
    be made, the promoted columns of ``source`` it lacks (``{column: definition}``, every one
    for a new target), and whether ``source`` has ``_version``.

    Raises:
        ValueError: ``target`` has a promoted column ``source`` has not, or has ``_version``
            where ``source`` has not, or the reverse; nothing is written.
    """
    src = [row for row in (yield _columns_sql(source), []) if row[6] == 0]
    if not src:
        return None
    names = [row[1] for row in src]
    promoted = {row[1]: _definition(row) for row in src if row[1] in _promoted_in(names)}
    versioned = _has_version(tuple(names))
    dst = [row[1] for row in (yield _columns_sql(target), []) if row[6] == 0]
    if not dst:
        return True, promoted, versioned
    have = {name.lower() for name in _promoted_in(dst)}
    extra = [
        name for name in _promoted_in(dst) if name.lower() not in {c.lower() for c in promoted}
    ]
    if extra:
        what = f"the column {extra[0]}" if len(extra) == 1 else f"the columns {', '.join(extra)}"
        raise ValueError(
            f"copy_to: {target} has {what}, which {source} has not: a copied row would leave it empty"
        )
    if _has_version(tuple(dst)) != versioned:
        said = (
            f"has no _version column and {source} has: the copied rows would lose their versions"
            if versioned
            else f"has a _version column and {source} has not: the copied rows have no version to keep"
        )
        raise ValueError(f"copy_to: {target} {said}")
    return False, {c: d for c, d in promoted.items() if c.lower() not in have}, versioned
