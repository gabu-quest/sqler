import dataclasses
import functools
import json
import re
import sqlite3
import sys
import threading
from typing import Any, NamedTuple, Optional

from sqler._stored import dumps, stored
from sqler.adapter import SQLiteAdapter
from sqler.exceptions import (
    InvalidFieldNameError,
    InvalidIdentifierError,
    NotFoundError,
    SchemaError,
    StaleVersionError,
    TableNotFoundError,
    TransactionStateError,
    UnstorableValueError,
)
from sqler.query import SQLerQuery
from sqler.query.query import is_missing_table
from sqler.utils import (
    validate_check_expression,
    validate_column_def,
    validate_column_name,
    validate_field_name,
    validate_identifier,
    validate_table_name,
)


class _Level(NamedTuple):
    """One open transaction of a database object's own: what it learned about tables, and the
    adapter's depth right after its ``begin_transaction`` (the level is the innermost one of the
    adapter's only while the adapter's depth is still this number)."""

    versioned: set[str]
    promoted: dict[str, list[str]]
    depth: int


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
    """The statement that lists the columns of the validated ``table``: no rows when it is missing."""
    return f'PRAGMA table_info("{table}");'


def _row_columns(promoted: Any = (), versioned: bool = False) -> tuple[str, ...]:
    """The columns a stored row is read with: ``_id``, ``data``, ``_version`` for a versioned
    read, then the promoted columns."""
    return ("_id", "data", "_version", *promoted) if versioned else ("_id", "data", *promoted)


# The most variables one statement binds: the bound the bulk inserts chunk by
# (``_insert_many_chunked``), SQLite's historic default limit of host variables.
_MAX_VARIABLES = 999


@functools.lru_cache(maxsize=512)
def _rows_sql(table: str, columns: tuple[str, ...], n: int) -> str:
    """The statement that reads ``n`` ids of the validated ``table`` with ``columns``, one
    variable per id; for ``n == 0`` the ids come as one JSON array in one variable. Built once
    per shape, so a one-id read builds no text."""
    ids = "SELECT value FROM json_each(?)" if n == 0 else ",".join("?" * n)
    return f"SELECT {', '.join(columns)} FROM {table} WHERE _id IN ({ids});"


def _row_documents(rows: Any, columns: list[str]) -> list[dict[str, Any]]:
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
    try:
        return adapter.execute(_rows_sql(table, columns, n), params).fetchall()
    except sqlite3.OperationalError as exc:
        if is_missing_table(exc, table):
            return None
        raise


# ---- the write of one row: promoted columns, the version check, a write of some fields ----


# a versioned row is written only while it is at the version its writer read
_AT_VERSION = "_id = ? AND _version = ? AND COALESCE(json_extract(data, '$._version'), ?) = ?"


def _split_promoted(doc: dict[str, Any], promoted_fields: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """``doc`` as a row holds it: the JSON document, and each of the ``promoted_fields`` in
    stored form in its column. Every promoted name is checked, so it can go into SQL."""
    for field in promoted_fields:
        validate_column_name(field)
    json_doc: dict[str, Any] = {}
    columns: dict[str, Any] = {}
    for k, v in doc.items():
        if k in promoted_fields:
            columns[k] = stored(v, field=k)
        else:
            json_doc[k] = v
    return json_doc, columns


def _json_path(key: str) -> str:
    """The JSON path of the top-level ``key`` of a document, to bind as a parameter.

    The key is quoted, so a dot, a space or a bracket in it is part of the name. A key with
    a double quote or a backslash is refused: SQLite reads an escape in a path only since
    3.45, so no path names such a key on every SQLite this library runs on."""
    if '"' in key or "\\" in key:
        raise InvalidFieldNameError(
            f"Field {key!r} cannot be written by itself: its name holds a double quote or a "
            "backslash, which a JSON path cannot name. Save the whole row with save().",
            field=key,
        )
    return f'$."{key}"'


def _fields_update(
    table: str, _id: Any, doc: dict[str, Any], fields: Any, promoted: Any, expected_version: Optional[int]
) -> Optional[tuple[str, list[Any]]]:
    """The ``UPDATE`` of row ``_id`` of the validated ``table`` that writes the ``fields`` of
    ``doc`` and leaves the row's other fields as they are, with its parameters; ``None`` when
    an unversioned write has nothing to write.

    A JSON field is set in ``data`` from its JSON text, a promoted one in its column, all in
    one statement; with ``expected_version`` the row is written only at that version and its
    ``_version`` goes up by one. A name in ``fields`` that ``doc`` does not hold (a key of
    the row the model has no field for) is not written and not removed. A JSON field's name
    is bound, never put into the SQL text."""
    json_doc, columns = _split_promoted({k: v for k, v in doc.items() if k in fields}, promoted)
    sets: list[str] = []
    params: list[Any] = []
    if json_doc:
        paths = [_json_path(k) for k in json_doc]
        try:
            texts = [dumps(v) for v in json_doc.values()]
        except UnstorableValueError:
            dumps(json_doc)  # the same refusal, named by the field it is in
            raise
        for path, text in zip(paths, texts):
            params += (path, text)
        sets.append(f"data = json_set(data{', ?, json(?)' * len(paths)})")
    for col, val in columns.items():
        sets.append(f"{col} = ?")
        params.append(val)
    if expected_version is None:
        return (f"UPDATE {table} SET {', '.join(sets)} WHERE _id = ?;", [*params, _id]) if sets else None
    sets.append("_version = _version + 1")
    v = expected_version
    return f"UPDATE {table} SET {', '.join(sets)} WHERE {_AT_VERSION};", [*params, _id, v, v, v]


def _refuse_unwritten(gone: bool, table: str, _id: Any) -> None:
    """The error of a write of some fields that wrote no row: the row is gone, or at another version."""
    if gone:
        raise NotFoundError(
            f"Row {_id} of {table} is gone: a write of some of its fields cannot recreate it",
            id_=_id,
        )
    raise StaleVersionError("Stale version: update rejected")


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
_DEFINITION = re.compile(r"\s*\w+(?:\s*\([^)]*\))?(\s+NOT\s+NULL)?(?:\s+DEFAULT\s+(.+?))?\s*$", re.IGNORECASE | re.DOTALL)

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
        if f is None or (f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING):
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
    picks = ", ".join(f"json_type(data, ?) AS t{i}, typeof(json_extract(data, ?)) AS v{i}" for i in range(len(columns)))
    tallies = ", ".join(f"COUNT(CASE WHEN {c} THEN 1 END), MIN(CASE WHEN {c} THEN _id END)" for c in conditions)
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


def _move_sql(table: str, fills: dict[str, Any]) -> tuple[str, list[Any]]:
    """The one ``UPDATE`` that takes each column's key out of every document and puts its value
    in the column: JSON null as NULL, never the column's DEFAULT. A row without the key gets the
    column's fill (``_NO_FILL``: what it holds; ``_KEEP``: the column is not written)."""
    sets, params = [], []
    for col, fill in fills.items():
        if fill is _KEEP:
            continue
        absent = col if fill is _NO_FILL else "?"
        sets.append(f"{col} = CASE WHEN json_type(data, ?) IS NULL THEN {absent} ELSE json_extract(data, ?) END")
        params += [_json_path(col)] if fill is _NO_FILL else [_json_path(col), fill]
        params.append(_json_path(col))
    paths = [_json_path(col) for col in fills]
    sets.append(f"data = json_remove(data{', ?' * len(paths)})")
    sql = f"UPDATE {table} SET {', '.join(sets)}"
    if all(fill is _NO_FILL or fill is _KEEP for fill in fills.values()):  # only the rows with a key change
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


def _settled(table: Any) -> Any:
    """Read which promoted columns of ``table`` hold their values. A column the database does not
    record as settled is read for rows whose document still holds its key, as an earlier version
    of sqler left them: none, and it is settled; some, and ``SchemaError`` names them, because a
    row does not say whether its column or its document is newer. Returns ``(column names of the
    table, the columns to record)``."""
    names = [row[1] for row in (yield _columns_sql(table), [])]
    promoted = _promoted_in(names)
    if not promoted:
        return names, []
    try:
        rows = yield f"SELECT col FROM {_SETTLED} WHERE tbl = ?;", [table]
    except sqlite3.OperationalError as exc:
        if not is_missing_table(exc, _SETTLED):
            raise _not_the_record(exc) or exc
        rows = []
    settled = {row[0].lower() for row in rows}
    unsettled = [col for col in promoted if col.lower() not in settled]
    if not unsettled:
        return names, []
    (row,) = yield _tally_sql(table, unsettled, [f"t{i} IS NOT NULL" for i in range(len(unsettled))])
    held = {c: {"rows": row[2 * i], "first_id": row[2 * i + 1]} for i, c in enumerate(unsettled) if row[2 * i]}
    if held:
        found = "; ".join(f"{c}: {h['rows']} row(s), the first _id {h['first_id']}" for c, h in held.items())
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
    return names, unsettled


def _record(table: str, columns: list[str], *, made: bool = False) -> Any:
    """Record ``columns`` of ``table`` as holding their values; ``made``: the table is being
    made now, and what is recorded under its name (a table of that name dropped by plain SQL)
    is not its own."""
    if not columns:
        return
    yield _SETTLED_DDL, []
    try:
        if made:
            yield f"DELETE FROM {_SETTLED} WHERE tbl = ?;", [table]
        yield f"INSERT OR IGNORE INTO {_SETTLED} (tbl, col) VALUES {', '.join(['(?, ?)'] * len(columns))};", [
            v for col in columns for v in (table, col)
        ]
    except sqlite3.OperationalError as exc:
        raise _not_the_record(exc) or exc


def _not_the_record(exc: sqlite3.OperationalError) -> Optional[SchemaError]:
    """The error for a table named ``_sqler_promoted`` that is not sqler's record, when ``exc``
    (from a statement on it) says its columns are not the record's; else ``None``."""
    if "no such column" not in str(exc) and "has no column named" not in str(exc):
        return None
    return SchemaError(
        f"{_SETTLED} is not sqler's record of settled columns: a table of that name with other "
        "columns is in the database. sqler keeps that name for itself; rename that table.",
        details={"table": _SETTLED},
    )


def _promotion(table: str, promoted: dict[str, str], checks: dict[str, str], model: Any) -> Any:
    """Under the write lock: create ``table`` with its ``promoted`` columns, or add the missing
    ones to it and move each row's value from its document into its column (a row without the
    key gets what it read through ``model``), and follow its search indexes. The record is
    written before any change, so a foreign ``_sqler_promoted`` refuses with nothing made.
    Returns its promoted columns.

    Nothing is changed when a row's value cannot be kept (``SchemaError``)."""
    names = [row[1] for row in (yield _columns_sql(table), [])]
    if not names:
        defs = [f"{col} {d}" for col, d in promoted.items()] + [f"CHECK ({c})" for c in checks.values()]
        yield from _record(table, list(promoted), made=True)  # first: a foreign record table changes nothing
        yield f"CREATE TABLE IF NOT EXISTS {table} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL{''.join(', ' + d for d in defs)});", []
        return list(promoted)
    added = {col: d for col, d in promoted.items() if col not in names}
    if added:
        fills, conditions = {}, []
        for i, (col, definition) in enumerate(added.items()):
            not_null, default = _DEFINITION.match(definition).groups()
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
    yield from _record(table, columns)  # before any change: a foreign record table changes nothing
    if added:
        for col, definition in added.items():
            yield f'ALTER TABLE "{table}" ADD COLUMN {col} {definition};', []
        yield _move_sql(table, fills)
        from sqler.fts import _follow_promotion  # fts imports this module

        yield from _follow_promotion(table, list(added), columns)
    return columns


def _moved(table: str, column: str, keep: str) -> Any:
    """``move_promoted``: take ``column``'s key out of every document of ``table``, its value going
    into the column (``keep="document"``) or not (``keep="column"``), and record the column."""
    info = {row[1]: row for row in (yield _columns_sql(table), [])}
    if not info:
        raise TableNotFoundError(f"Table {table} does not exist", table=table)
    if column not in _promoted_in(info):
        raise SchemaError(
            f"{column} is not a promoted column of {table}", details={"table": table, "column": column}
        )
    if keep == "document":
        (row,) = yield _tally_sql(table, [column], [_unkept(0, bool(info[column][3]), False)])
        if row[0]:
            raise _refused(table, column, row[0], row[1])
    yield from _record(table, [column])
    yield _move_sql(table, {column: _NO_FILL if keep == "document" else _KEEP})
    from sqler.fts import _follow_promotion  # fts imports this module

    yield from _follow_promotion(table, [column], _promoted_in(info))


def _keep_word(keep: Any) -> str:
    if keep not in ("document", "column"):
        raise ValueError(f"keep must be 'document' or 'column', not {keep!r}")
    return keep


def _unwritable(exc: sqlite3.OperationalError) -> bool:
    """Whether ``exc`` says the database takes no write now: opened read-only or ``query_only``,
    or another connection holds the write lock past the busy timeout."""
    code = getattr(exc, "sqlite_errorcode", None)  # an error sqler raised itself carries none
    return code is not None and (code & 0xFF) in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


class SQLerDB:
    """Document store for JSON blobs on SQLite.

    SQLerDB persists Python dicts as JSON in a table with schema
    ``(_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)``. The API
    is table-agnostic: pass the table name on each call. Tables are created
    on demand as you insert or query.
    """

    @classmethod
    def in_memory(cls, shared: bool = True, *, name: Optional[str] = None, timeout_ms: int = 5000) -> "SQLerDB":
        """Create a SQLerDB backed by an in-memory SQLite database.

        Args:
            shared: When True, use a shared-cache URI so multiple connections
                see the same in-memory database.
            name: Optional shared cache identifier; use the same name to
                connect multiple adapters to a shared in-memory database.
            timeout_ms: How long a statement waits for a lock, in milliseconds
                (see ``SQLiteAdapter``).

        Returns:
            SQLerDB: Connected database instance.
        """
        adapter = SQLiteAdapter.in_memory(shared=shared, name=name, timeout_ms=timeout_ms)
        return cls(adapter)

    @classmethod
    def on_disk(cls, path: str = "sqler.db", *, timeout_ms: int = 5000) -> "SQLerDB":
        """Create a SQLerDB backed by a persistent file on disk.

        Args:
            path: Path to the SQLite database file (created if missing).
            timeout_ms: How long a statement waits for a lock, in milliseconds
                (see ``SQLiteAdapter``).

        Returns:
            SQLerDB: Connected database instance.
        """
        adapter = SQLiteAdapter.on_disk(path, timeout_ms=timeout_ms)
        return cls(adapter)

    def __init__(self, adapter: SQLiteAdapter):
        self.adapter = adapter
        self.adapter.connect()
        # serialize DDL operations (e.g., adding _version) across threads
        self._ddl_lock = threading.RLock()
        # what is remembered about committed tables: versioned tables, and the promoted columns
        # of each table this object has seen settled (:func:`_settled`)
        self._versioned_tables: set[str] = set()
        self._promoted_columns: dict[str, list[str]] = {}
        # tables seen settled whose record could not be written then: the next ensure writes it
        self._unrecorded: set[str] = set()
        # what each open level of this object's own transactions learned, innermost last; per
        # thread, like the adapter's depth
        self._open = _OpenLevels()

    def ensure_table(
        self,
        table: str,
        *,
        promoted: Optional[dict[str, str]] = None,
        checks: Optional[dict[str, str]] = None,
        versioned: bool = False,
    ) -> None:
        """Create a table if it is missing, with promoted columns, CHECK constraints and a version.

        Safe to call again: on an existing table it adds the promoted columns
        and the ``_version`` column that are missing, and changes nothing else
        (CHECK constraints are only written when the table is created). A
        column added to a table with rows takes each row's value out of its
        document, in the same transaction; a row without the key gets the
        column's DEFAULT, or NULL.

        Args:
            table: Table name (``[a-zA-Z_][a-zA-Z0-9_]*``).
            promoted: ``{column: definition}`` for real columns beside ``data``,
                e.g. ``{"status": "TEXT NOT NULL DEFAULT 'pending'"}``.
            checks: ``{name: expression}`` CHECK constraints for a new table.
            versioned: Add the ``_version INTEGER NOT NULL DEFAULT 0`` column
                used by the versioned writes.

        Raises:
            InvalidTableNameError: The table name is not accepted.
            InvalidIdentifierError: A promoted column name is not accepted.
            InvalidColumnDefError: A column definition or CHECK expression is not accepted.
            SchemaError: A promoted column cannot be added and nothing was changed: a row's
                value cannot be kept in it, it is NOT NULL with no DEFAULT, or an earlier
                version left its values in the documents (see :meth:`move_promoted`).
        """
        if promoted or checks:
            self._ensure_table_with_promoted(table, promoted or {}, checks)
        else:
            self._ensure_table(table)
        if versioned:
            self._ensure_versioned_table(table)

    def _ensure_table(self, table: str) -> None:
        """Create the target table if it doesn't exist.

        Args:
            table: Table name to ensure.
        """
        table = validate_table_name(table)
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            data JSON NOT NULL
        );
        """
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def _ensure_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
        *,
        model: Any = None,
    ) -> None:
        """Ensure ``table`` for a promoted write; nothing is sent when it is remembered here
        with every column of ``promoted`` and its record written."""
        known = self._promoted_of(table)
        if known is None or any(col not in known for col in promoted) or table in self._unrecorded:
            self._ensure_table_with_promoted(table, promoted, checks, model=model)

    def _ensure_table_with_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
        *,
        model: Any = None,
    ) -> None:
        """Create or upgrade a table with promoted columns.

        Promoted columns are real SQLite columns that live alongside the JSON
        ``data`` blob. They get CHECK constraints on fresh tables. Added to a
        table that has rows, a column takes each row's value out of its document,
        all in one transaction, and a row without the key gets what it read
        before: the default of ``model``'s field, else the column's DEFAULT.

        Args:
            table: Table name.
            promoted: ``{column_name: column_def}`` mapping, e.g.
                ``{"status": "TEXT NOT NULL DEFAULT 'pending'"}``.
            checks: Optional ``{name: expression}`` CHECK constraints.
            model: The model class whose rows the table holds, if any.

        Raises:
            SchemaError: A row's value cannot be kept, a NOT NULL column has no DEFAULT,
                or a column an earlier version promoted is not settled (``move_promoted``).
        """
        table = validate_table_name(table)
        checks = checks or {}
        for col_name, col_def in promoted.items():
            validate_column_name(col_name)
            validate_column_def(col_def)
        for chk_expr in checks.values():
            validate_check_expression(chk_expr)

        with self._ddl_lock:
            names, unsettled = self._run(_settled(table))
            unrecorded = False
            if not names or any(col not in names for col in promoted):
                with self:  # one transaction: the column, its values and the record, or nothing
                    columns = self._run(_promotion(table, promoted, checks, model))
            else:
                columns = _promoted_in(names)
                if unsettled:
                    try:
                        with self:
                            self._run(_record(table, unsettled))
                    except sqlite3.OperationalError as exc:
                        if not _unwritable(exc):
                            raise
                        unrecorded = True  # settled all the same: there is nothing to add or move
            self._remember(table, promoted=columns)
            (self._unrecorded.add if unrecorded else self._unrecorded.discard)(table)

    def move_promoted(self, table: str, column: str, *, keep: str) -> None:
        """Settle a promoted column whose values an earlier version of sqler left in the documents.

        That version added the column and left each row's value in its document; since then a
        write may have changed either, and a row does not say which is newer. This takes the
        key out of every document, in one transaction, and records the column as settled.

        Args:
            table: Table name.
            column: The promoted column.
            keep: ``"document"``: a row whose document holds the key gets that value in the
                column. ``"column"``: the column keeps its values.

        Raises:
            ValueError: ``keep`` is neither word.
            TableNotFoundError: The table does not exist.
            SchemaError: The column is not a promoted column of the table, or (``keep="document"``)
                a document's value cannot go into the column; nothing is changed.
        """
        keep = _keep_word(keep)
        table = validate_table_name(table)
        validate_column_name(column)
        with self._ddl_lock, self:
            self._run(_moved(table, column, keep))

    def _run(self, steps: Any) -> Any:
        """Send each statement ``steps`` yields, answer it with its rows or its error, and
        return what ``steps`` returns."""
        try:
            step = next(steps)
            while True:
                try:
                    rows = self.adapter.execute(*step).fetchall()
                except sqlite3.OperationalError as exc:
                    step = steps.throw(exc)
                else:
                    step = steps.send(rows)
        except StopIteration as done:
            return done.value

    def _learned(self) -> Optional[tuple[set[str], dict[str, list[str]]]]:
        """Where an ensure call keeps what it learned about a table.

        The innermost open level of this object's own transactions, if the adapter's depth is
        still that level's; with no level open, the shared memory, at depth 0. Anywhere else
        the transaction is not this object's (the adapter used directly, another thread's on
        the single ``":memory:"`` connection): nowhere. Forgetting costs a repeated
        ``CREATE TABLE IF NOT EXISTS``, remembering wrongly loses a write.
        """
        depth = self.adapter._txn_depth
        levels = self._open.levels
        if levels:
            level = levels[-1]
            return (level.versioned, level.promoted) if level.depth == depth else None
        return (self._versioned_tables, self._promoted_columns) if depth == 0 else None

    def _remember(self, table: str, *, promoted: Optional[list[str]] = None) -> None:
        """Remember ``table``'s ``promoted`` columns, or without them that it has ``_version``."""
        learned = self._learned()
        if learned is None:
            return
        if promoted is None:
            learned[0].add(table)
        else:
            learned[1][table] = promoted

    def _is_versioned(self, table: str) -> bool:
        """Whether ``table`` is remembered to have its ``_version`` column, here and now."""
        return table in self._versioned_tables or any(table in level.versioned for level in self._open.levels)

    def _promoted_of(self, table: str) -> Optional[list[str]]:
        """The promoted columns remembered for ``table`` here and now; ``None`` if it is not remembered."""
        for level in reversed(self._open.levels):
            if table in level.promoted:
                return level.promoted[table]
        return self._promoted_columns.get(table)

    def _columns_of(self, table: str) -> list[str]:
        """The promoted columns of the validated ``table``, which every reader sets over the
        document: remembered, or else checked once as an ensure checks them (:func:`_settled`,
        which writes nothing) and remembered (a table without any as having none). A missing
        table has none and is not remembered.

        Raises:
            SchemaError: A document still holds the key of a column, which would hide its value.
        """
        promoted = self._promoted_of(table)
        if promoted is None:
            names, unsettled = self._run(_settled(table))
            promoted = _promoted_in(names)
            learned = self._learned() if names else None
            if learned is not None:
                # an entry made since the statements above (another thread's ensure) is newer
                promoted = learned[1].setdefault(table, promoted)
                if unsettled:
                    self._unrecorded.add(table)
        return promoted

    def upsert_document_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        promoted_fields: list[str],
    ) -> int:
        """Insert or update a document, splitting promoted fields into real columns.

        Args:
            table: Table name.
            _id: Existing id to update, or ``None`` to insert.
            doc: Full document payload.
            promoted_fields: Keys to extract from doc into real columns.

        Returns:
            int: The existing or newly assigned ``_id``.
        """
        table = validate_table_name(table)
        # Split promoted from JSON
        json_doc, promoted_vals = _split_promoted(doc, promoted_fields)

        payload = dumps(json_doc)

        if _id is None:
            # INSERT
            cols = ["data"] + list(promoted_vals.keys())
            placeholders = ["json(?)"] + ["?" for _ in promoted_vals]
            params = [payload] + list(promoted_vals.values())
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)});"
            cursor = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
            return cursor.lastrowid
        else:
            # UPDATE
            set_parts = ["data = json(?)"]
            params: list[Any] = [payload]
            for col, val in promoted_vals.items():
                set_parts.append(f"{col} = ?")
                params.append(val)
            params.append(_id)
            sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE _id = ?;"
            self.adapter.execute(sql, params)
            self.adapter.auto_commit()
            return _id

    def upsert_with_version_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        expected_version: Optional[int],
        promoted_fields: list[str],
    ) -> tuple[int, int]:
        """Insert or update with optimistic locking, splitting promoted columns.

        Args:
            table: Table name.
            _id: Existing row id for update; ``None`` to insert.
            doc: Document to write.
            expected_version: Version expected by the caller.
            promoted_fields: Keys to extract into real columns.

        Returns:
            tuple[int, int]: The row id and new version.
        """
        table = validate_table_name(table)
        json_doc, promoted_vals = _split_promoted(doc, promoted_fields)
        if not self._is_versioned(table):
            self._ensure_versioned_table(table)

        payload = dumps(json_doc)

        if _id is None:
            cols = ["data", "_version"] + list(promoted_vals.keys())
            placeholders = ["json(?)", "0"] + ["?" for _ in promoted_vals]
            params = [payload] + list(promoted_vals.values())
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)});"
            cur = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
            return cur.lastrowid, 0

        if expected_version is None:
            raise ValueError("expected_version required for update")

        set_parts = ["data = json(?)", "_version = _version + 1"]
        params_list: list[Any] = [payload]
        for col, val in promoted_vals.items():
            set_parts.append(f"{col} = ?")
            params_list.append(val)
        params_list.extend([_id, expected_version, expected_version, expected_version])

        sql = (
            f"UPDATE {table} SET {', '.join(set_parts)} "
            f"WHERE {_AT_VERSION};"
        )
        cur = self._versioned_update(sql, params_list)
        rc = getattr(cur, "rowcount", -1)
        if rc <= 0:
            self.adapter.execute(
                f"SELECT _version FROM {table} WHERE _id = ?;", [_id]
            ).fetchone()
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

    def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        """Insert a document.

        Args:
            table: Table name.
            doc: JSON-serializable dict to persist.

        Returns:
            int: Newly assigned ``_id``.
        """
        self._ensure_table(table)
        payload = dumps(doc)
        cursor = self.adapter.execute(f"INSERT INTO {table} (data) VALUES (json(?));", [payload])
        self.adapter.auto_commit()
        return cursor.lastrowid

    def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        """Insert or update a document.

        Args:
            table: Table name.
            _id: Existing id to update, or ``None`` to insert.
            doc: Document to write.

        Returns:
            int: The existing or newly assigned ``_id``.
        """
        self._ensure_table(table)
        payload = dumps(doc)
        if _id is None:
            return self.insert_document(table, doc)
        self.adapter.execute(f"UPDATE {table} SET data = json(?) WHERE _id = ?;", [payload, _id])
        self.adapter.auto_commit()
        return _id

    def _update_fields(
        self,
        table: str,
        _id: int,
        doc: dict[str, Any],
        fields: Any,
        promoted: Any = (),
        expected_version: Optional[int] = None,
    ) -> Optional[int]:
        """Write the ``fields`` of ``doc`` into row ``_id`` and leave its other fields as they
        are, in one statement: the JSON ones in ``data``, the ``promoted`` ones in their columns.
        With ``expected_version`` the row is written only at that version, as
        ``upsert_with_version`` writes it, and the new version is returned.

        Raises:
            NotFoundError: The row is gone: a write of some fields cannot recreate it.
            StaleVersionError: The row is at another version.
        """
        table = validate_table_name(table)
        if expected_version is None:
            self._ensure_table(table)
        elif not self._is_versioned(table):
            self._ensure_versioned_table(table)
        update = _fields_update(table, _id, doc, fields, promoted, expected_version)
        if update is None:
            return None
        if expected_version is None:
            cur = self.adapter.execute(*update)
            self.adapter.auto_commit()
            if cur.rowcount > 0:
                return None
            _refuse_unwritten(True, table, _id)
        cur = self._versioned_update(*update)
        if cur.rowcount > 0:
            return expected_version + 1
        row = self.adapter.execute(f"SELECT 1 FROM {table} WHERE _id = ?;", [_id]).fetchone()
        _refuse_unwritten(row is None, table, _id)

    # ---- batch insert helpers ----

    def insert_many(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
        """Multi-row INSERT for plain JSON documents.

        All documents must be new (no ``_id``). Uses chunked multi-row INSERT
        statements to stay within SQLite's 999-parameter limit.

        Args:
            table: Table name.
            docs: List of JSON-serializable dicts to insert.

        Returns:
            list[int]: Assigned ``_id`` for each document, preserving order.
        """
        if not docs:
            return []
        self._ensure_table(table)
        return self._insert_many_chunked(table, docs, cols=["data"], versioned=False)

    def insert_many_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[int]:
        """Multi-row INSERT splitting promoted fields into real columns.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Keys to extract from each doc into real columns.

        Returns:
            list[int]: Assigned ``_id`` for each document, preserving order.
        """
        if not docs:
            return []
        table = validate_table_name(table)
        for field in promoted_fields:
            validate_identifier(field)
        cols = ["data"] + promoted_fields
        return self._insert_many_chunked(table, docs, cols=cols, versioned=False, promoted_fields=promoted_fields)

    def insert_many_with_version(
        self, table: str, docs: list[dict[str, Any]]
    ) -> list[tuple[int, int]]:
        """Multi-row INSERT with ``_version=0`` for versioned tables.

        Args:
            table: Table name.
            docs: List of JSON-serializable dicts.

        Returns:
            list[tuple[int, int]]: ``(id, 0)`` pairs for each document.
        """
        if not docs:
            return []
        self._ensure_versioned_table(table)
        ids = self._insert_many_chunked(table, docs, cols=["data"], versioned=True)
        return [(id_, 0) for id_ in ids]

    def insert_many_with_version_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[tuple[int, int]]:
        """Multi-row INSERT with ``_version=0`` splitting promoted fields.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Keys to extract into real columns.

        Returns:
            list[tuple[int, int]]: ``(id, 0)`` pairs for each document.
        """
        if not docs:
            return []
        for field in promoted_fields:
            validate_identifier(field)
        self._ensure_versioned_table(table)
        cols = ["data"] + promoted_fields
        ids = self._insert_many_chunked(table, docs, cols=cols, versioned=True, promoted_fields=promoted_fields)
        return [(id_, 0) for id_ in ids]

    def _insert_many_chunked(
        self,
        table: str,
        docs: list[dict[str, Any]],
        *,
        cols: list[str],
        versioned: bool,
        promoted_fields: list[str] | None = None,
    ) -> list[int]:
        """Internal: chunked multi-row INSERT with transaction wrapping.

        Builds ``INSERT INTO t (col1, ...) VALUES (...), (...), ...`` statements,
        chunking to stay within SQLite's 999-parameter limit.
        """
        promoted_fields = promoted_fields or []
        params_per_row = 1 + len(promoted_fields)
        max_rows_per_chunk = max(1, _MAX_VARIABLES // params_per_row)

        all_ids: list[int] = []
        all_col_names = list(cols)
        if versioned:
            all_col_names = ["data", "_version"] + promoted_fields

        with self.transaction():
            for offset in range(0, len(docs), max_rows_per_chunk):
                chunk = docs[offset : offset + max_rows_per_chunk]
                params: list[Any] = []
                value_groups: list[str] = []

                for doc in chunk:
                    if promoted_fields:
                        promoted_vals = {k: stored(doc[k], field=k) for k in promoted_fields if k in doc}
                        json_doc = {k: v for k, v in doc.items() if k not in promoted_fields}
                    else:
                        json_doc = doc
                        promoted_vals = {}

                    payload = dumps(json_doc)
                    row_placeholders = ["json(?)"]
                    params.append(payload)

                    if versioned:
                        row_placeholders.append("0")

                    for pf in promoted_fields:
                        row_placeholders.append("?")
                        params.append(promoted_vals.get(pf))

                    value_groups.append(f"({', '.join(row_placeholders)})")

                col_list = ", ".join(all_col_names)
                values_sql = ", ".join(value_groups)
                sql = f"INSERT INTO {table} ({col_list}) VALUES {values_sql};"

                cursor = self.adapter.execute(sql, params)
                try:
                    last_id = cursor.lastrowid
                finally:
                    cursor.close()

                # ID range is contiguous because BEGIN IMMEDIATE (from
                # self.transaction()) holds an exclusive write lock, preventing
                # concurrent inserts from interleaving AUTOINCREMENT IDs.
                chunk_size = len(chunk)
                first_id = last_id - chunk_size + 1
                all_ids.extend(range(first_id, last_id + 1))

        return all_ids

    def bulk_upsert(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
        """Upsert multiple documents efficiently.

        New docs (without ``_id``) are inserted and receive ids. Existing docs
        (with ``_id``) are updated.

        Uses chunked multi-row INSERT statements (same pattern as
        ``_insert_many_chunked``) to minimize per-row Python overhead.

        Args:
            table: Table name.
            docs: List of documents. If an element contains ``_id``, it is
                treated as an update; otherwise, an insert.

        Returns:
            list[int]: The ``_id`` for each input document, preserving order.
        """
        self._ensure_table(table)
        if not docs:
            return []

        # Split into inserts (no _id) and updates (has _id), tracking
        # original indices so we can return IDs in input order.
        inserts: list[tuple[int, dict]] = []
        updates: list[tuple[int, dict]] = []
        for i, doc in enumerate(docs):
            if doc.get("_id") is None:
                inserts.append((i, doc))
            else:
                updates.append((i, doc))

        assigned = [0] * len(docs)

        with self.adapter as adapter:
            # Batch inserts: chunked multi-row INSERT, 1 param per row.
            if inserts:
                for offset in range(0, len(inserts), 999):
                    chunk = inserts[offset : offset + 999]
                    params = [
                        dumps({k: v for k, v in doc.items() if k != "_id"})
                        for _, doc in chunk
                    ]
                    values_sql = ", ".join("(json(?))" for _ in chunk)
                    sql = f"INSERT INTO {table} (data) VALUES {values_sql};"
                    cursor = adapter.execute(sql, params)
                    last_id = cursor.lastrowid
                    # ID range is contiguous because BEGIN IMMEDIATE (from
                    # self.adapter context) holds an exclusive write lock.
                    first_id = last_id - len(chunk) + 1
                    for j, (orig_idx, doc) in enumerate(chunk):
                        new_id = first_id + j
                        assigned[orig_idx] = new_id
                        doc["_id"] = new_id

            # Batch updates: chunked multi-row INSERT ON CONFLICT, 2 params
            # per row.  Chunk size 499 keeps params under sqlite's 999 limit.
            if updates:
                for offset in range(0, len(updates), 499):
                    chunk = updates[offset : offset + 499]
                    params = []
                    for _, doc in chunk:
                        params.extend([
                            int(doc["_id"]),
                            dumps({k: v for k, v in doc.items() if k != "_id"}),
                        ])
                    values_sql = ", ".join("(?, json(?))" for _ in chunk)
                    sql = (
                        f"INSERT INTO {table} (_id, data) VALUES {values_sql} "
                        "ON CONFLICT(_id) DO UPDATE SET data = excluded.data;"
                    )
                    adapter.execute(sql, params)
                    for orig_idx, doc in chunk:
                        assigned[orig_idx] = int(doc["_id"])

        return assigned

    def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by id.

        Args:
            table: Table name.
            _id: Row id to fetch.

        Returns:
            dict | None: Decoded document with ``_id`` merged in, or ``None``
            if not found.
        """
        rows, columns = self._read_rows(table, [_id])
        return _row_documents(rows, columns)[0] if rows else None

    def find_documents(self, table: str, ids: list[int]) -> list[dict[str, Any]]:
        """Fetch multiple documents by id list (batch operation).

        Documents are returned in the same order as the input ids. If an id
        is not found, it is omitted from the result (no None placeholder).

        Args:
            table: Table name.
            ids: List of row ids to fetch.

        Returns:
            list[dict]: Decoded documents with ``_id`` and the promoted columns merged
            in, as :meth:`find_document` gives each.
        """
        if not ids:
            return []
        rows, columns = self._read_rows(table, ids)
        return _row_documents(_in_order(rows or [], ids), columns)

    def _read_rows(
        self, table: str, ids: list, *, versioned: bool = False, alter: bool = True
    ) -> tuple[Optional[list], list[str]]:
        """The by-id read every reader of this database goes through.

        Returns the stored rows of ``ids`` (stored order) and the columns they were read
        with: the promoted columns of ``table`` (:meth:`_columns_of`), and ``_version`` when
        ``versioned``. A missing table gives ``None`` rows and is not created; a table made
        before versioning gets its ``_version`` column on a versioned read, as it always did.
        With ``alter=False`` it gets none, and nothing is sent that fails for want of it: a
        table not remembered as versioned is asked for its columns; with ``_version`` that is
        remembered, without it the rows are read at version 0 and nothing is remembered.
        """
        table = validate_table_name(table)
        promoted = self._columns_of(table)
        columns = _row_columns(promoted, versioned)
        if versioned and not alter and not self._is_versioned(table):
            names = [row[1] for row in self.adapter.execute(_columns_sql(table)).fetchall()]
            if "_version" not in names:
                plain = _select_rows(self.adapter, table, _row_columns(promoted), ids)
                if plain is None:  # a missing table stays a missing table
                    return None, columns
                return [(r[0], r[1], 0, *r[2:]) for r in plain], columns
            self._remember(table)
        try:
            rows = _select_rows(self.adapter, table, columns, ids)
        except sqlite3.OperationalError as exc:
            if not versioned or "no such column: _version" not in str(exc):
                raise
            self._ensure_versioned_table(table)
            rows = _select_rows(self.adapter, table, columns, ids)
        return rows, columns

    def delete_document(self, table: str, _id: int) -> None:
        """Delete a document by id.

        Args:
            table: Table name.
            _id: Row id to delete.
        """
        self._ensure_table(table)
        self.adapter.execute(f"DELETE FROM {table} WHERE _id = ?;", [_id])
        self.adapter.auto_commit()

    def execute_sql(self, query: str, params: Optional[list[Any]] = None) -> list[dict[str, Any]]:
        """Run a custom SELECT and return lightweight row mappings.

        Only read-only statements (SELECT, EXPLAIN, PRAGMA, WITH) are allowed.

        When the result set exposes a ``data`` column alongside ``_id``, the
        JSON payload is decoded and merged with ``_id``. For ad-hoc projections
        (e.g. ``SELECT _id``) the method returns simple dicts keyed by the
        selected columns so callers can hydrate with :meth:`SQLerModel.from_id`.

        Args:
            query: SQL SELECT statement.
            params: Optional parameter list.

        Returns:
            list[dict[str, Any]]: Decoded documents with ``_id`` included.

        Raises:
            ValueError: If the query is not a read-only statement.
        """
        stripped = query.strip()
        first_word = stripped.split()[0].upper() if stripped else ""
        if first_word not in ("SELECT", "EXPLAIN", "PRAGMA", "WITH"):
            raise ValueError(
                "execute_sql only accepts read-only queries (SELECT/EXPLAIN/PRAGMA/WITH)"
            )
        # Reject multi-statement queries (semicolon before the end)
        if ";" in stripped.rstrip(";"):
            raise ValueError(
                "execute_sql does not allow multi-statement queries"
            )
        cursor = self.adapter.execute(query, params or [])
        rows = cursor.fetchall()
        docs: list[dict[str, Any]] = []
        for row in rows:
            mapping = None
            try:
                mapping = row.keys()  # type: ignore[attr-defined]
            except AttributeError:
                mapping = None  # Row doesn't support dict-like access
            if mapping:
                keys = list(mapping)
                if "data" in keys:
                    raw = row["data"]
                    obj = json.loads(raw)
                    if "_id" in keys:
                        obj["_id"] = int(row["_id"])
                    docs.append(obj)
                    continue
                if keys == ["_id"]:
                    docs.append({"_id": int(row["_id"])})
                    continue
                docs.append({k: row[k] for k in keys})
                continue
            if len(row) >= 2:
                obj = json.loads(row[1])
                obj["_id"] = int(row[0])
                docs.append(obj)
            elif len(row) == 1:
                docs.append({"_id": int(row[0])})
            else:
                docs.append({})
        return docs

    def query(self, table: str) -> SQLerQuery:
        """Return a SQLerQuery bound to this DB's adapter.

        Args:
            table: Table name.

        Returns:
            SQLerQuery: Query object you can chain and execute.
        """
        return SQLerQuery(table=validate_table_name(table), adapter=self.adapter)

    def close(self):
        """Close the underlying adapter connection."""
        self.adapter.close()

    def connect(self):
        """Connect the underlying adapter if not already connected."""
        self.adapter.connect()

    def transaction(self) -> "Transaction":
        """Return a context manager for explicit transactions.

        Usage::

            with db.transaction():
                db.insert_document("users", {"name": "Alice"})
                db.insert_document("users", {"name": "Bob"})
                # commits on exit, rolls back on exception

        Returns:
            Transaction: Context manager for transaction scope.
        """
        return Transaction(self.adapter, self)

    def create_index(
        self,
        table: str,
        field: str,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ):
        """Create an index on a JSON field or literal column.

        For JSON paths, pass dotted paths like ``"meta.level"``. These are
        compiled into ``json_extract(data, '$.meta.level')``. Literal columns
        (e.g., ``_id``) should be prefixed with ``_`` and are used as-is.

        Args:
            table: Table name.
            field: Dotted JSON path or literal column.
            unique: Enforce uniqueness of the index.
            name: Optional index name; autogenerated if omitted.
            where: Optional partial-index WHERE clause.
        """
        self._ensure_table(table)
        validate_field_name(field) if not field.startswith("_") else validate_identifier(field)
        if name is not None:
            validate_identifier(name)
        if where is not None:
            validate_check_expression(where)
        idx_name = name or f"idx_{table}_{field.replace('.', '_')}"
        unique_sql = "UNIQUE" if unique else ""
        expr = f"json_extract(data, '$.{field}')" if not field.startswith("_") else field
        where_sql = f"WHERE {where}" if where else ""
        ddl = f"CREATE {unique_sql} INDEX IF NOT EXISTS {idx_name} ON {table} ({expr}) {where_sql};"
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def drop_index(self, name: str):
        """Drop an index by name.

        Args:
            name: Index name.
        """
        validate_identifier(name)
        ddl = f"DROP INDEX IF EXISTS {name};"
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def list_indexes(self, table: Optional[str] = None) -> list[dict[str, Any]]:
        """List indexes in the database.

        Args:
            table: Optional table name to filter by. If None, lists all indexes.

        Returns:
            List of dicts with index info: name, table, sql, unique.
        """
        if table:
            query = """
                SELECT name, tbl_name, sql
                FROM sqlite_master
                WHERE type = 'index' AND tbl_name = ? AND name NOT LIKE 'sqlite_%'
                ORDER BY name;
            """
            cur = self.adapter.execute(query, [table])
        else:
            query = """
                SELECT name, tbl_name, sql
                FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
                ORDER BY tbl_name, name;
            """
            cur = self.adapter.execute(query)

        indexes = []
        for row in cur.fetchall():
            name = row[0]
            tbl_name = row[1]
            sql = row[2] or ""
            indexes.append(
                {
                    "name": name,
                    "table": tbl_name,
                    "sql": sql,
                    "unique": "UNIQUE" in sql.upper() if sql else False,
                }
            )
        return indexes

    def index_exists(self, name: str) -> bool:
        """Check if an index exists by name.

        Args:
            name: Index name to check.

        Returns:
            True if the index exists, False otherwise.
        """
        cur = self.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?;", [name]
        )
        return cur.fetchone() is not None

    def __enter__(self):
        """Enter context manager; begin transaction."""
        self._begin_level()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager; commit, or roll back while the caller's exception leaves."""
        self._end_level(exc_type is None, leaving=exc_val)
        return False

    def _begin_level(self) -> None:
        self.adapter.begin_transaction()
        self._open.levels.append(_Level(set(), {}, self.adapter._txn_depth))

    def _end_level(self, commit: bool, *, leaving: Optional[BaseException] = None) -> None:
        """End this thread's innermost level; what it learned about tables goes with it.

        A commit moves it to the level above, or at the outermost level to the shared memory;
        a rollback, or a commit that fails, drops it. That runs no statement and raises
        nothing. ``leaving``: the caller's exception on its way out (see ``_end_transaction``).
        """
        levels = self._open.levels
        level = levels.pop() if levels else None  # none: begun on another thread; nothing to merge
        _end_transaction(self.adapter, commit, leaving)
        if commit and level is not None and (above := self._learned()) is not None:
            above[0].update(level.versioned)
            above[1].update(level.promoted)

    # ---- versioned (optimistic locking) helpers ----

    def _ensure_versioned_table(self, table: str) -> None:
        """Ensure the target table exists and has a ``_version`` column.

        This upgrades an existing non-versioned table by adding the column.

        Args:
            table: Table name.
        """
        self._ensure_table(table)
        # fast path: check without lock
        cols = [row[1] for row in self.adapter.execute(_columns_sql(table)).fetchall()]
        if self._promoted_of(table) is None and not _promoted_in(cols):
            self._remember(table, promoted=[])  # promoted columns are remembered once checked
        if "_version" in cols:
            self._remember(table)
            return
        # serialize DDL; re-check inside lock
        with self._ddl_lock:
            cols = [row[1] for row in self.adapter.execute(_columns_sql(table)).fetchall()]
            if "_version" not in cols:
                self.adapter.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "_version" INTEGER NOT NULL DEFAULT 0;'
                )
                self.adapter.auto_commit()
            self._remember(table)

    def _versioned_update(self, sql: str, params: list[Any]) -> Any:
        """Run a versioned ``UPDATE`` and commit it unless an explicit transaction is open.

        Outside an explicit transaction the write lock is taken first with
        ``BEGIN IMMEDIATE`` (less live-lock under contention), and that
        transaction is ended on every path: committed on success, rolled back
        when anything raises before the commit.
        """
        if self.adapter.in_transaction:
            cur = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
            return cur
        try:
            self.adapter.execute("BEGIN IMMEDIATE;")
        except (RuntimeError, ValueError):
            pass  # an adapter that cannot run BEGIN here: the UPDATE runs in its own transaction
        try:
            cur = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
        except BaseException:
            try:
                self.adapter.execute("ROLLBACK;")
            except sqlite3.Error:
                pass  # already rolled back by the adapter, or nothing open
            raise
        return cur

    def upsert_with_version(
        self, table: str, _id: Optional[int], doc: dict[str, Any], expected_version: Optional[int]
    ) -> tuple[int, int]:
        """Insert or update a document with optimistic locking.

        On insert, ``_version`` is set to 0. On update, the row is updated only
        if the stored version matches ``expected_version``; on success, ``_version``
        is incremented by 1.

        Args:
            table: Table name.
            _id: Existing row id for update; ``None`` to insert.
            doc: Document to write.
            expected_version: Version expected by the caller for update; ignored on insert.

        Returns:
            tuple[int, int]: The row id and the new version after the operation.

        Raises:
            ValueError: If updating with ``_id`` but ``expected_version`` is None.
            RuntimeError: On stale version conflicts (no rows updated).
        """
        table = validate_table_name(table)
        if not self._is_versioned(table):
            self._ensure_versioned_table(table)
        payload = dumps(doc)
        if _id is None:
            cur = self.adapter.execute(
                f"INSERT INTO {table} (data, _version) VALUES (json(?), 0);",
                [payload],
            )
            self.adapter.auto_commit()
            return cur.lastrowid, 0
        if expected_version is None:
            raise ValueError("expected_version required for update")
        cur = self._versioned_update(
            f"UPDATE {table} SET data = json(?), _version = _version + 1 "
            f"WHERE {_AT_VERSION};",
            [payload, _id, expected_version, expected_version, expected_version],
        )
        rc = getattr(cur, "rowcount", -1)
        if rc <= 0:
            # treat non-positive as conflict
            # double-check via select
            _ = self.adapter.execute(
                f"SELECT _version FROM {table} WHERE _id = ?;", [_id]
            ).fetchone()
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

    def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by id including ``_version`` in the result dict.

        Args:
            table: Table name.
            _id: Row id to fetch.

        Returns:
            dict | None: Decoded document with ``_id`` and ``_version`` keys, or None.
        """
        rows, columns = self._read_rows(table, [_id], versioned=True)
        return _row_documents(rows, columns)[0] if rows else None


class _OpenLevels(threading.local):
    """The open levels of a database object's own transactions on this thread."""

    def __init__(self):
        self.levels: list[_Level] = []


def _end_transaction(adapter, commit: bool, leaving: Optional[BaseException]) -> None:
    """End the adapter's innermost transaction.

    While the caller's exception ``leaving`` is on its way out, an error of the end is not
    raised (the adapter closed inside the block, say): that exception is the one that
    leaves, and when the end says the transaction was rolled back
    (``TransactionStateError``) it goes on with that sentence as a note.
    """
    try:
        adapter.end_transaction(commit=commit)
    except Exception as ended:
        _ended_while_leaving(ended, leaving)


def _ended_while_leaving(ended: Exception, leaving: Optional[BaseException]) -> None:
    """The end of a transaction raised ``ended``: raise it, or, while ``leaving`` is on its way
    out, name it on ``leaving`` as a note, once (a nested block it left already may have
    added it). A ``TransactionStateError`` is the sentence of a rolled-back transaction;
    any other error is named with its type and message."""
    if leaving is None:
        raise ended
    if isinstance(ended, TransactionStateError):
        if isinstance(leaving, TransactionStateError):
            return
        note = str(ended)
    else:
        note = f"ending the transaction failed: {type(ended).__name__}: {ended}"
    if note not in getattr(leaving, "__notes__", ()):
        leaving.add_note(note)


class Transaction:
    """Context manager for explicit database transactions.

    When inside a Transaction context, model.save() and other document
    operations will NOT auto-commit. The transaction commits only when
    the context exits successfully, or rolls back on exception.
    """

    def __init__(self, adapter, db: SQLerDB):
        self.adapter = adapter
        self._db = db
        self._active = False

    def _end(self, commit: bool, leaving: Optional[BaseException] = None) -> None:
        self._active = False
        self._db._end_level(commit, leaving=leaving)

    def __enter__(self):
        """Begin the transaction."""
        self._db._begin_level()
        self._active = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Commit on success, rollback on exception.

        When the block raised and the transaction was rolled back for a reason of its own
        (a nested end that failed), the block's exception goes on with that sentence as a note.
        """
        if self._active:
            self._end(exc_type is None, leaving=exc_val)
        return False

    def commit(self):
        """Explicitly commit the transaction."""
        if self._active:
            self._end(True)

    def rollback(self):
        """Explicitly rollback the transaction."""
        if self._active:
            self._end(False)
