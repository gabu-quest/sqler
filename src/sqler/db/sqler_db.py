import dataclasses
import functools
import json
import re
import sqlite3
import sys
import threading
from typing import Any, NamedTuple, Optional

from sqler import cache
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
from sqler.query.query import _MOVE_VERSION, _has_version, end_failed, is_missing_table
from sqler.utils import (
    validate_check_expression,
    validate_column_def,
    validate_column_name,
    validate_field_name,
    validate_identifier,
    validate_table_name,
)


class _Level(NamedTuple):
    """One open transaction of a database object's own: what it learned about tables, the
    adapter's depth right after its ``begin_transaction`` (the level is the innermost one of the
    adapter's only while the adapter's depth is still this number), and the tables of cached
    results written in it (``sqler.cache``), whose results are not cached until it ends."""

    versioned: set[str]
    promoted: dict[str, list[str]]
    layouts: dict[str, tuple[str, ...]]
    depth: int
    written: set[str]


class _TableMemory:
    """What a database object remembers of its tables: the names ``SELECT *`` gives for each
    (``_layouts``), which of them have ``_version`` (``_versioned_tables``), and the promoted
    columns of each it has seen settled (``_promoted_columns``); each open level of its own
    transactions remembers what it learned in it (``_levels()``).

    The promoted columns and the version of a table hold for the names remembered beside them.
    A statement built from them carries the guard of those names (:func:`_guard`), so SQLite
    refuses it once the table has other columns; only then is the table learned again.
    The class provides ``_levels()`` and ``_adapter_depth()``.
    """

    def _init_memory(self) -> None:
        self._versioned_tables: set[str] = set()
        self._promoted_columns: dict[str, list[str]] = {}
        self._layouts: dict[str, tuple[str, ...]] = {}
        # tables seen settled whose record could not be written then: the next ensure writes it
        self._unrecorded: set[str] = set()

    def _learned(self) -> Optional[tuple[set[str], dict[str, list[str]], dict[str, tuple[str, ...]]]]:
        """Where a statement's lesson about a table is kept.

        The innermost open level of this object's own transactions, if the adapter's depth is
        still that level's; with no level open, the shared memory, at depth 0. Anywhere else
        the transaction is not this object's (the adapter used directly, another thread's on
        the single ``":memory:"`` connection): nowhere. That transaction holds the write lock,
        so a statement built from a table read in it is not refused for a change made since.
        """
        depth = self._adapter_depth()
        levels = self._levels()
        if levels:
            level = levels[-1]
            return (level.versioned, level.promoted, level.layouts) if level.depth == depth else None
        return (self._versioned_tables, self._promoted_columns, self._layouts) if depth == 0 else None

    def _remember(self, table: str, names: tuple[str, ...], *, promoted: Optional[list[str]] = None) -> None:
        """Remember that ``table`` has the columns ``names`` (as ``SELECT *`` gives them), and,
        when given, its ``promoted`` columns. Promoted columns remembered for other names are
        forgotten."""
        learned = self._learned()
        if learned is not None:
            _put(learned, table, names, promoted)

    def _forget(self, table: str) -> None:
        """Forget everything remembered about ``table``, in every level and in the shared memory."""
        places = [(level.versioned, level.promoted, level.layouts) for level in self._levels()]
        for versioned, promoted, layouts in [*places, (self._versioned_tables, self._promoted_columns, self._layouts)]:
            versioned.discard(table)
            promoted.pop(table, None)
            layouts.pop(table, None)

    def _merge(self, level: _Level) -> None:
        """``level`` committed: what it learned goes to the level above, or at the outermost
        level to the shared memory."""
        if not level.layouts:
            return  # it learned nothing: nothing to move
        above = self._learned()
        if above is not None:
            for table, names in level.layouts.items():
                _put(above, table, names, level.promoted.get(table))

    def _written_level(self) -> Optional[set[str]]:
        """The tables of cached results written in this object's innermost open transaction
        (``_Level.written``); ``None`` with none open. A level whose depth is above the
        adapter's has ended: a task started inside a transaction keeps that transaction's
        levels after it."""
        levels = self._levels()
        if levels and levels[-1].depth <= self._adapter_depth():
            return levels[-1].written
        return None

    def _known(self, table: str) -> Optional[tuple[tuple[str, ...], Optional[list[str]], bool]]:
        """The names, promoted columns (``None``: not checked yet) and version remembered for
        ``table`` here and now, from the innermost place that remembers its names."""
        levels = self._levels()
        for level in reversed(levels) if levels else ():
            names = level.layouts.get(table)
            if names is not None:
                return names, level.promoted.get(table), table in level.versioned
        names = self._layouts.get(table)
        return None if names is None else (names, self._promoted_columns.get(table), table in self._versioned_tables)

    def _is_versioned(self, table: str) -> bool:
        """Whether ``table`` is remembered to have its ``_version`` column, here and now."""
        known = self._known(table)
        return known is not None and known[2]

    def _promoted_of(self, table: str) -> Optional[list[str]]:
        """The promoted columns remembered for ``table`` here and now; ``None`` if it is not remembered."""
        known = self._known(table)
        return None if known is None else known[1]

    def _layout_of(self, table: str) -> Optional[tuple[str, ...]]:
        """The names ``SELECT *`` gives for ``table``, as remembered here and now; ``None`` if not remembered."""
        known = self._known(table)
        return None if known is None else known[0]

    def _seen(self, table: str, layout: tuple[str, ...], names: list[str], added: tuple[str, ...] = ()) -> None:
        """Remember ``table`` as just read (``_names``), with the columns ``added`` since by
        this object: the promoted columns remembered for that layout stay, and a table with
        none is remembered as having none (an ensure's check needs no statement for it)."""
        promoted = self._promoted_of(table) if self._layout_of(table) == layout else None
        if promoted is None and not _promoted_in(names):
            promoted = []
        self._remember(table, (*layout, *added), promoted=promoted)

    def _guard_of(self, table: str, names: Optional[tuple[str, ...]]) -> str:
        """The guard (:func:`_guard`) of ``names``, those a statement on ``table`` is built
        from; empty for none. The caller passes the names it read, so the guard and what the
        statement is kept under cannot differ."""
        return "" if names is None else _guard(table, names)


def _put(place: Any, table: str, names: tuple[str, ...], promoted: Optional[list[str]]) -> None:
    """Remember in ``place`` (a level's or the shared memory's sets) that ``table`` has the
    columns ``names``, and its ``promoted`` columns when given; promoted columns remembered
    for other names are dropped."""
    versioned, known, layouts = place
    if layouts.get(table) != names:
        known.pop(table, None)
    layouts[table] = names
    (versioned.add if _has_version(names) else versioned.discard)(table)
    if promoted is not None:
        known[table] = promoted


def _quoted(name: str) -> str:
    """``name`` as an identifier only: a double-quoted name SQLite cannot resolve would be read
    as a string, and the guard would hold for a column that is gone."""
    return "`" + name.replace("`", "``") + "`"


@functools.lru_cache(maxsize=512)
def _guard(table: str, names: tuple[str, ...], value: bool = False) -> str:
    """The guard a statement built from what is remembered of the validated ``table`` carries:
    a ``CASE`` whose ``ELSE`` never runs and reads the row value of ``names`` against
    ``SELECT *`` of the table. SQLite checks it each time it prepares the statement, so the
    statement is refused once the table has other columns (a name it lacks by its name,
    another count as "sub-select returns N columns"), and runs as before while it has these.

    ``IN``, not ``=``: SQLite resolves the names before it compares the counts, and a name a
    connection does not find makes it reload a schema another connection changed, so a
    connection that has not seen a column this object learned elsewhere runs the statement.

    A term of a ``WHERE``, which is 1; with ``value``, an expression to put in place of one
    ``?`` of an ``INSERT``, which is that variable."""
    row = ", ".join(_quoted(name) for name in names)
    return f"CASE WHEN 1 THEN {'?' if value else '1'} ELSE (SELECT ({row}) IN (SELECT * FROM {table}) FROM {table}) END"


def _and_guard(table: str, names: Any) -> str:
    """`` AND `` and the guard of ``names`` (:func:`_guard`), to end a ``WHERE``; empty without ``names``."""
    return f" AND {_guard(table, names)}" if names else ""


@functools.lru_cache(maxsize=512)
def _check_sql(table: str, names: tuple[str, ...]) -> str:
    """The guard of ``names`` (:func:`_guard`) as a statement of its own, which reads no row:
    sent first in a transaction, it holds for the statements after it there that are built
    from those names and carry no guard (the bulk writes, the triggers of a search index)."""
    return f"SELECT {_guard(table, names)};"


def _and_fields(columns: list[str], fields: Any) -> list[str]:
    """The promoted ``columns`` of a table, then each of the caller's ``fields`` they lack: a
    write names that one as a column, so SQLite refuses it while the table lacks it."""
    missing = [f for f in fields if f not in columns]
    return [*columns, *missing] if missing else columns


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


# ---- the write of one row: promoted columns, the version check, a write of some fields ----


# a checked write of a versioned row is made only while its column is at the version its writer read
_AT_VERSION = "_id = ? AND _version = ?"


def _unversioned(doc: dict[str, Any]) -> dict[str, Any]:
    """``doc`` as a row of a table with the ``_version`` column stores it: without a ``_version``
    key, which is the row's column and is moved by the write itself, never set from a document."""
    return {k: v for k, v in doc.items() if k != "_version"} if "_version" in doc else doc


def _split_promoted(doc: dict[str, Any], promoted_fields: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """``doc`` as a row holds it: the JSON document, and each of the ``promoted_fields`` in
    stored form in its column. Every promoted name is checked, so it can go into SQL."""
    if not promoted_fields:
        return doc, {}
    for field in promoted_fields:
        validate_column_name(field)
    return _split(doc, promoted_fields)


def _split(doc: dict[str, Any], columns: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """:func:`_split_promoted` with ``columns`` already checked: each key of ``doc`` that names
    one of them in stored form, the other keys as the JSON document. A column whose key ``doc``
    does not hold is not in the result, so a write leaves it as it is."""
    json_doc: dict[str, Any] = {}
    values: dict[str, Any] = {}
    for k, v in doc.items():
        if k in columns:
            values[k] = stored(v, field=k)
        else:
            json_doc[k] = v
    return json_doc, values


def _row(doc: dict[str, Any], columns: Any, versioned: bool = False) -> tuple[tuple[str, ...], str, list[Any]]:
    """``doc`` as one row of a table with the checked ``columns`` beside ``data`` (``versioned``:
    and ``_version``, :func:`_unversioned`): the columns whose key it holds, in the table's
    order; its JSON text; their values in stored form."""
    if versioned:
        doc = _unversioned(doc)
    if not columns:
        return (), dumps(doc), []
    json_doc, values = _split(doc, columns)
    held = tuple(c for c in columns if c in values)
    return held, dumps(json_doc), [values[c] for c in held]


def _runs(docs: list[dict[str, Any]], columns: Any, fixed: int, versioned: bool = False) -> Any:
    """The rows of ``docs`` (:func:`_row`) for multi-row statements: each run of consecutive
    documents that hold the same column keys, as ``(those columns, [(index in docs, JSON text,
    values)])``, cut so that one statement binds at most ``_MAX_VARIABLES`` with ``fixed``
    variables a row besides the JSON text and the values, and holds one row at the least."""
    keys: Optional[tuple[str, ...]] = None
    run: list[tuple[int, str, list[Any]]] = []
    size = 0
    for i, doc in enumerate(docs):
        held, text, values = _row(doc, columns, versioned)
        if held != keys or len(run) == size:
            if run:
                yield keys, run
            keys, run, size = held, [], max(1, _MAX_VARIABLES // (fixed + 1 + len(held)))
        run.append((i, text, values))
    if run:
        yield keys, run


@functools.lru_cache(maxsize=512)
def _insert_sql(
    table: str, held: Any, n: int, *, versioned: bool = False, upsert: bool = False, guard: Optional[tuple[str, ...]] = None
) -> str:
    """The ``INSERT`` of ``n`` rows into the validated ``table`` that names ``data`` and the
    columns ``held``, nothing else (a column it does not name gets its DEFAULT, or keeps its
    value on an update). ``upsert``: each row leads with its ``_id`` and an existing row takes
    ``data`` and ``held``. ``versioned`` (the table has ``_version``): a new row is at 0, and an
    existing row's version moves by one. ``guard``: the remembered names whose guard
    (:func:`_guard`) the first row carries (:func:`_first`). Built once per shape."""
    names = [*["_id"] * upsert, "data", *["_version"] * versioned, *held]
    lead = [*["?"] * upsert, "json(?)", *["0"] * versioned]
    mark = f"({', '.join([*lead, *['?'] * len(held)])})"
    marks = [mark] * n
    if guard:
        marks[0] = mark.replace("json(?)", f"json({_guard(table, guard, True)})", 1)
    sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES {', '.join(marks)}"
    if upsert:
        sets = ", ".join([*(f"{c} = excluded.{c}" for c in ("data", *held)), *[_MOVE_VERSION] * versioned])
        sql += f" ON CONFLICT(_id) DO UPDATE SET {sets}"
    return sql + ";"


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
    table: str,
    _id: Any,
    doc: dict[str, Any],
    fields: Any,
    promoted: Any,
    expected_version: Optional[int],
    layout: Any = None,
    versioned: bool = False,
) -> Optional[tuple[str, list[Any], Any]]:
    """The ``UPDATE`` of row ``_id`` of the validated ``table`` that writes the ``fields`` of
    ``doc`` and leaves the row's other fields as they are, with its parameters and ``layout``;
    ``None`` when an unchecked write has nothing to write.

    A JSON field is set in ``data`` from its JSON text, a promoted one in its column, all in
    one statement. ``versioned`` (the table has ``_version``): the row's version moves by one,
    and a ``_version`` key of ``doc`` is not written; with ``expected_version`` the row is
    written only at that version. With ``layout``, the guard of those remembered names. A name
    in ``fields`` that ``doc`` does not hold (a key of the row the model has no field for) is
    not written and not removed. A JSON field's name is bound, never put into the SQL text."""
    versioned = versioned or expected_version is not None
    json_doc, columns = _split_promoted({k: v for k, v in doc.items() if k in fields and not (versioned and k == "_version")}, promoted)
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
    guard = _and_guard(table, layout)
    if expected_version is None:
        if not sets:
            return None
        sets += [_MOVE_VERSION] * versioned
        return f"UPDATE {table} SET {', '.join(sets)} WHERE _id = ?{guard};", [*params, _id], layout
    sets.append(_MOVE_VERSION)
    return f"UPDATE {table} SET {', '.join(sets)} WHERE {_AT_VERSION}{guard};", [*params, _id, expected_version], layout


def _refuse_unwritten(gone: bool, table: str, _id: Any) -> None:
    """The error of a write by ``_id`` that wrote no row: the row is not there, or at another version."""
    if gone:
        raise NotFoundError(
            f"Row {_id} of {table} is not there: a write by _id does not create it; to put a copy "
            "into another database, save a new instance without the _id",
            id_=_id,
        )
    raise StaleVersionError("Stale version: update rejected")


def _create_sql(table: str) -> str:
    """The ``CREATE TABLE IF NOT EXISTS`` of the validated ``table`` with no column beside ``data``."""
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            data JSON NOT NULL
        );
        """


def _upsert_sql(
    table: str,
    _id: Any,
    doc: dict[str, Any],
    fields: Any,
    layout: Any = None,
    versioned: bool = False,
    expected_version: Optional[int] = None,
) -> tuple[str, list[Any], Any]:
    """The ``INSERT`` of ``doc`` into the validated ``table``, or the ``UPDATE`` of row ``_id``
    with it, each key of ``fields`` in its column, with its parameters and ``layout``, the
    remembered names whose guard it carries (:func:`_guard`). ``versioned`` (the table has
    ``_version``): a new row is at version 0, an update moves the row's version by one, and
    ``doc``'s ``_version`` key is not stored (:func:`_unversioned`); with ``expected_version``
    an update writes the row only at that version."""
    checked = _id is not None and expected_version is not None
    versioned = versioned or checked
    json_doc, values = _split_promoted(_unversioned(doc) if versioned else doc, fields)
    params = [dumps(json_doc), *values.values()]
    if _id is not None:
        params += (_id, expected_version) if checked else (_id,)
    return _write_text(table, _id is None, versioned, tuple(values), layout, checked), params, layout


@functools.lru_cache(maxsize=512)
def _write_text(table: str, insert: bool, versioned: bool, held: tuple[str, ...], layout: Any, checked: bool = False) -> str:
    """The text of :func:`_upsert_sql` for the columns ``held``, built once per table,
    statement, columns, remembered names and version (``checked``: written only at it)."""
    if insert:
        lead = ["data", "_version"] if versioned else ["data"]
        marks = ", ".join([f"json({_guard(table, layout, True) if layout else '?'})", *["0"] * versioned, *["?"] * len(held)])
        return f"INSERT INTO {table} ({', '.join([*lead, *held])}) VALUES ({marks});"
    sets = ", ".join(["data = json(?)", *[_MOVE_VERSION] * versioned, *(f"{col} = ?" for col in held)])
    return f"UPDATE {table} SET {sets} WHERE {_AT_VERSION if checked else '_id = ?'}{_and_guard(table, layout)};"


# Each function below that ``yield``s is a run of statements both database classes send the
# same way (``SQLerDB._sent_all`` and its async twin), in one transaction: it yields
# ``(sql, params, names)``, gets back the statement's ``lastrowid``, and returns its answer.
# Its first statement, the one the sender may build again, is guarded by ``layout``, the names
# remembered beside ``columns``, and is yielded with those names (:func:`_first`); the
# statements after it carry no guard: the transaction's write lock keeps the table as it is.


def _first(table: str, layout: Any, n: int) -> Any:
    """How the first INSERT of a run, of ``n`` rows, is guarded by the remembered ``layout``
    (``None`` for a later INSERT, which is not): returns the names its own text is to carry
    (:func:`_insert_sql`), or yields the check (:func:`_check_sql`) to send before it and
    returns ``None``. An INSERT of one row carries the guard, as a single write does; a guard
    in any row of a many-row ``VALUES`` takes the whole statement off SQLite's fast path,
    which costs more from the second row on than the check as a statement of its own."""
    if layout and n > 1:
        yield _check_sql(table, layout), [], layout
        return None
    return layout or None


def _inserts(table: str, docs: list[dict[str, Any]], versioned: bool, columns: Any, layout: Any = None) -> Any:
    """The multi-row INSERTs of ``docs`` into the validated ``table``: each run of consecutive
    documents that hold the same keys of the checked ``columns`` is one statement
    (:func:`_runs`), within SQLite's variable limit; a column a document does not hold is not
    named, so it gets its DEFAULT. Returns the ids, in order: the ids of one statement are
    contiguous, because the transaction holds the write lock from its ``BEGIN IMMEDIATE``."""
    ids: list[int] = []
    for held, rows in _runs(docs, columns or (), 0, versioned):
        guard, layout = (yield from _first(table, layout, len(rows))), None
        params = [p for _, text, values in rows for p in (text, *values)]
        last = yield _insert_sql(table, held, len(rows), versioned=versioned, guard=guard), params, guard
        ids.extend(range(last - len(rows) + 1, last + 1))
    return ids


def _bulk_upserts(table: str, docs: list[dict[str, Any]], versioned: bool, columns: Any, layout: Any = None) -> Any:
    """``bulk_upsert``: the documents without an ``_id`` inserted as :func:`_inserts` inserts
    them, then those with one written into their rows, made if missing (``versioned``: each
    write of a row moves its version, :func:`_insert_sql`). Returns the ``_id`` of each
    document, in input order; ``docs`` are not written to (:func:`_hand_ids`)."""
    # by their index in ``docs``, each without its ``_id`` key, which is the row's column
    inserts = [i for i, doc in enumerate(docs) if doc.get("_id") is None]
    updates = [i for i, doc in enumerate(docs) if doc.get("_id") is not None]
    bodies = [{k: v for k, v in doc.items() if k != "_id"} for doc in docs]
    assigned = [0] * len(docs)
    for held, rows in _runs([bodies[i] for i in inserts], columns, 0, versioned):
        guard, layout = (yield from _first(table, layout, len(rows))), None
        params = [p for _, text, values in rows for p in (text, *values)]
        last = yield _insert_sql(table, held, len(rows), versioned=versioned, guard=guard), params, guard
        for j, (k, _, _) in enumerate(rows):
            assigned[inserts[k]] = last - len(rows) + 1 + j
    for held, rows in _runs([bodies[i] for i in updates], columns, 1, versioned):
        guard, layout = (yield from _first(table, layout, len(rows))), None
        params = [p for k, text, values in rows for p in (int(docs[updates[k]]["_id"]), text, *values)]
        yield _insert_sql(table, held, len(rows), versioned=versioned, upsert=True, guard=guard), params, guard
        for k, _, _ in rows:
            assigned[updates[k]] = int(docs[updates[k]]["_id"])
    return assigned


def _hand_ids(docs: list[dict[str, Any]], ids: list[int]) -> list[int]:
    """Give each of the caller's ``docs`` that had no ``_id`` its new one, once the bulk upsert
    that made the rows has succeeded: after a failure the documents are as they were passed,
    so none holds the id of a row that is not there. Returns ``ids``."""
    new = [doc.get("_id") is None for doc in docs]
    for doc, _id, was_new in zip(docs, ids, new):
        if was_new:  # a dict passed twice gets the id of its last row
            doc["_id"] = _id
    return ids


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
        sets.append(f"{col} = CASE WHEN json_type(data, ?) IS NULL THEN {absent} ELSE json_extract(data, ?) END")
        params += [_json_path(col)] if fill is _NO_FILL else [_json_path(col), fill]
        params.append(_json_path(col))
    moved = [col for col, fill in fills.items() if fill is _NO_FILL] if versioned else []
    if moved:
        same = " AND ".join(f"(json_type(data, ?) IS NULL OR {col} IS json_extract(data, ?))" for col in moved)
        sets.append(f"_version = CASE WHEN {same} THEN _version ELSE _version + 1 END")
        params += [_json_path(col) for col in moved for _ in range(2)]
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
            raise _not_the_record(exc) or exc
        rows = []
    settled = {row[0].lower() for row in rows}
    unsettled = [col for col in promoted if col.lower() not in settled]
    if not unsettled:
        return []
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
    return unsettled


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
    Returns its promoted columns and the names ``SELECT *`` gives for it then.

    Nothing is changed when a row's value cannot be kept (``SchemaError``)."""
    layout, names = _layout((yield _columns_sql(table), []))
    if not names:
        defs = [f"{col} {d}" for col, d in promoted.items()] + [f"CHECK ({c})" for c in checks.values()]
        yield from _record(table, list(promoted), made=True)  # first: a foreign record table changes nothing
        yield f"CREATE TABLE IF NOT EXISTS {table} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL{''.join(', ' + d for d in defs)});", []
        return list(promoted), ("_id", "data", *promoted)
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
    return columns, (*layout, *added)


def _moved(table: str, column: str, keep: str) -> Any:
    """``move_promoted``: take ``column``'s key out of every document of ``table``, its value going
    into the column (``keep="document"``) or not (``keep="column"``), and record the column."""
    info = {row[1]: row for row in (yield _columns_sql(table), []) if row[6] == 0}
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
    yield _move_sql(table, {column: _NO_FILL if keep == "document" else _KEEP}, _has_version(tuple(info)))
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


class SQLerDB(_TableMemory):
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
        # what is remembered about committed tables (``_TableMemory``): versioned tables, and the
        # promoted columns of each table this object has seen settled (:func:`_settled`)
        self._init_memory()
        # what each open level of this object's own transactions learned, innermost last; per
        # thread, like the adapter's depth
        self._open = _OpenLevels()

    def _levels(self) -> list[_Level]:
        return self._open.levels

    def _adapter_depth(self) -> int:
        return self.adapter._txn_depth

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
        self.adapter.execute(_create_sql(table))
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
            layout, names = self._names(table)
            unsettled = self._run(_settled(table, names))
            unrecorded = False
            if not names or any(col not in names for col in promoted):
                with self:  # one transaction: the column, its values and the record, or nothing
                    columns, layout = self._run(_promotion(table, promoted, checks, model))
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
            self._remember(table, layout, promoted=columns)
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
        """Send each statement ``steps`` yields, answer it with its rows or its
        ``sqlite3.OperationalError``, and return what ``steps`` returns. A failure ends what a
        failed statement ends only when ``steps`` raises it (or its own error for it): one it
        reads as an answer (the record of settled columns not there) ends nothing."""
        adapter = self.adapter
        try:
            step = next(steps)
            while True:
                try:
                    rows = adapter._execute_judged(*step).fetchall()
                except sqlite3.OperationalError as exc:
                    try:
                        step = steps.throw(exc)
                    except StopIteration:
                        raise
                    except Exception as raised:
                        adapter._end_failed_statement(raised)
                except Exception as exc:
                    adapter._end_failed_statement(exc)
                else:
                    step = steps.send(rows)
        except StopIteration as done:
            return done.value

    def _names(self, table: str) -> tuple[tuple[str, ...], list[str]]:
        """The names ``SELECT *`` gives for the validated ``table`` and the names of its stored
        columns (:func:`_layout`); none when it is missing."""
        return _layout(self.adapter.execute(_columns_sql(table)).fetchall())

    def _sent(self, table: str, make: Any, *, immediate: bool = False) -> Any:
        """Send the statement ``make()`` builds from what this object remembers of ``table``
        and return its cursor; ``None`` when ``make()`` returns ``None`` (nothing to send).
        ``make()`` gives ``(sql, params, names)``: ``names`` are the remembered names the
        statement was built from, whose guard it carries (:func:`_guard`), ``None`` for none.

        SQLite refuses the statement when the table no longer has those names. When the names
        the table has now differ (:meth:`_has_moved`), nothing has been ended and the statement is
        built and sent again; any other failure ends what a failed statement ends (all that
        was sent since the last commit, outside a transaction) and is raised as it is, but a
        read of a missing ``table``, which ends nothing (:func:`end_failed`).

        ``immediate``: outside a transaction the write lock is taken first with
        ``BEGIN IMMEDIATE`` (less live-lock under contention), and that transaction is ended
        on every path: committed on success, rolled back by the adapter's rule when anything
        raises before the commit, with the error that raised leaving.
        """
        adapter = self.adapter
        while True:
            step = make()
            if step is None:
                return None
            sql, params, names = step
            try:
                if immediate and not adapter.in_transaction:
                    return self._sent_immediate(sql, params)
                return adapter._execute_judged(sql, params)
            except Exception as exc:
                if isinstance(exc, sqlite3.OperationalError) and self._has_moved(table, names):
                    continue
                end_failed(adapter, exc, table, sql)

    def _sent_immediate(self, sql: str, params: list[Any]) -> Any:
        """The statement sent in a transaction of its own begun with ``BEGIN IMMEDIATE`` and committed."""
        try:
            self.adapter.execute("BEGIN IMMEDIATE;")
        except (RuntimeError, ValueError):
            pass  # an adapter that cannot run BEGIN here: the statement runs in its own transaction
        try:
            cursor = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
        except BaseException as exc:
            self.adapter.__exit__(type(exc), exc, exc.__traceback__)  # rolls back what is still open
            raise
        return cursor

    def _has_moved(self, table: str, names: Optional[tuple[str, ...]]) -> bool:
        """Whether ``table`` has other names now than ``names``, those a refused statement was
        built from: the names remembered now when they are others (another statement learned
        the table since), else the names read now (:meth:`_learn`). A remembered table that is
        gone has none now and is forgotten: the statement built again is built from nothing
        remembered, so a plain insert makes its table again (:meth:`_upsert`) and anything else
        is refused for the missing table. A statement built from nothing remembered, or a
        table that cannot be read now, has nothing to build again from."""
        if names is None:
            return False  # SQLite refused it for itself
        now = self._layout_of(table)
        if now is None or now == names:
            try:
                now = self._learn(table)[0]
            except sqlite3.OperationalError:
                return False
        return now != names

    def _memory_of(self, table: str, fields: Any = ()) -> tuple[list[str], Optional[tuple[str, ...]]]:
        """The promoted columns and remembered names of :meth:`_shape_of`."""
        return self._shape_of(table, fields)[:2]

    def _shape_of(self, table: str, fields: Any = ()) -> tuple[list[str], Optional[tuple[str, ...]], bool]:
        """The promoted columns of the validated ``table`` (:meth:`_columns_of`), the names
        remembered beside them, whose guard a statement built from those columns carries
        (``None``: none remembered, as where nothing can be remembered, a transaction that is
        not this object's), and whether the table has ``_version`` (:func:`_has_version`), from
        those names or the names just read.

        A writer splits a document by these columns. ``fields``: the columns a caller says
        the table has (a class's promoted fields); one the table lacks is added
        (:func:`_and_fields`), so its write is refused rather than put into ``data``."""
        if self._open.levels:
            known = self._known(table)
            layout, columns = (None, None) if known is None else known[:2]
        else:  # no transaction of this object's is open: the shared memory, read here (every call comes through)
            layout = self._layouts.get(table)
            columns = None if layout is None else self._promoted_columns.get(table)
        names = layout
        if columns is None:
            names, columns = self._learn(table)
            layout = self._layout_of(table)
        return (_and_fields(columns, fields) if fields else columns), layout, _has_version(names)

    def _columns_of(self, table: str) -> list[str]:
        """The promoted columns of the validated ``table``, which every reader sets over the
        document: remembered, or else checked once as an ensure checks them (:func:`_settled`,
        which writes nothing) and remembered with the table's names (a table without any as
        having none). A missing table has none and is not remembered.

        Raises:
            SchemaError: A document still holds the key of a column, which would hide its value.
        """
        promoted = self._promoted_of(table)
        return self._learn(table)[1] if promoted is None else promoted

    def _learn(self, table: str) -> tuple[tuple[str, ...], list[str]]:
        """Read the names ``SELECT *`` gives for the validated ``table`` and its promoted columns,
        checked as an ensure checks them (:func:`_settled`, which writes nothing), and remember
        them; returns both, none for a missing table, which is forgotten. Where nothing can be
        remembered (a transaction that is not this object's), names other than those
        remembered are forgotten, so no statement is built from them again.

        Raises:
            SchemaError: A document still holds the key of a column, which would hide its value.
        """
        layout, names = self._names(table)
        unsettled = self._run(_settled(table, names))
        promoted = _promoted_in(names)
        if not names:
            self._forget(table)
        elif self._learned() is not None:
            self._remember(table, layout, promoted=promoted)
            if unsettled:
                self._unrecorded.add(table)
        elif layout != self._layout_of(table):
            self._forget(table)
        return layout, promoted

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
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            int: The existing or newly assigned ``_id``.

        Raises:
            NotFoundError: There is no row ``_id``, or no such table; nothing is written.
        """
        return self._upsert(validate_table_name(table), _id, doc, promoted_fields)

    def _upsert(self, table: str, _id: Optional[int], doc: dict[str, Any], fields: Any = ()) -> int:
        """Write ``doc`` as a new row, or into row ``_id``, each key of the table's columns in
        its column (:meth:`_memory_of`, with the caller's ``fields``), with the guard of what is
        remembered of it (:meth:`_sent`). A new row with no ``fields`` makes its table, for
        which nothing is sent once the table is remembered; a write by ``_id`` makes no table."""

        def make() -> tuple[str, list[Any], Any]:
            if _id is None and not fields and self._layout_of(table) is None:
                self._ensure_table(table)
            columns, layout, has = self._shape_of(table, fields)
            return _upsert_sql(table, _id, doc, columns, layout, has)

        try:
            cursor = self._sent(table, make)
        except sqlite3.OperationalError as exc:
            if _id is None or not is_missing_table(exc, table):
                raise
            _refuse_unwritten(True, table, _id)
        self.adapter.auto_commit()
        if _id is None:
            return cursor.lastrowid
        if cursor.rowcount > 0:
            return _id
        _refuse_unwritten(True, table, _id)

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
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            tuple[int, int]: The row id and new version.
        """
        return self._upsert_versioned(validate_table_name(table), _id, doc, expected_version, promoted_fields)

    def _upsert_versioned(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        expected_version: Optional[int],
        fields: Any = (),
    ) -> tuple[int, int]:
        """``upsert_with_version`` of ``doc``, split as :meth:`_upsert` splits it. The table
        gets its ``_version`` column first; a write by ``_id`` makes no table (``NotFoundError``)."""
        if _id is not None and expected_version is None:
            raise ValueError("expected_version required for update")

        def make() -> tuple[str, list[Any], Any]:
            self._columns_of(table)  # an unsettled table is refused before it is given ``_version``
            if not self._is_versioned(table) and (_id is None or self._has_version_column(table) is not None):
                self._ensure_versioned_table(table)  # a write by ``_id`` makes no table
            columns, layout, has = self._shape_of(table, fields)
            return _upsert_sql(table, _id, doc, columns, layout, has, expected_version)

        if _id is None:
            cursor = self._sent(table, make)
            self.adapter.auto_commit()
            return cursor.lastrowid, 0
        try:
            cursor = self._sent(table, make, immediate=True)
        except sqlite3.OperationalError as exc:
            if not is_missing_table(exc, table):
                raise
            _refuse_unwritten(False, table, _id)  # a row at another version, as with the table there
        if cursor.rowcount <= 0:
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

    def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        """Insert a document: a key that names a column beside ``data`` goes into the column, the
        rest into ``data``; a column whose key it does not hold is left out, so it gets its DEFAULT.

        Args:
            table: Table name.
            doc: JSON-serializable dict to persist.

        Returns:
            int: Newly assigned ``_id``.
        """
        return self._upsert(validate_table_name(table), None, doc)

    def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        """Insert or update a document, as :meth:`insert_document` writes it; an update leaves a
        column whose key the document does not hold as it is.

        Args:
            table: Table name.
            _id: Existing id to update, or ``None`` to insert.
            doc: Document to write.

        Returns:
            int: The existing or newly assigned ``_id``.

        Raises:
            NotFoundError: The table has no row ``_id``, or there is no such table; nothing is
                written and no table is made.
        """
        return self._upsert(validate_table_name(table), _id, doc)

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
        are, in one statement: a field that names a column beside ``data`` in its column, the
        rest in ``data``. The columns are the table's, with the caller's ``promoted``
        (:meth:`_memory_of`). With ``expected_version`` the row is written only at
        that version, as ``upsert_with_version`` writes it, and the new version is returned.

        Raises:
            NotFoundError: The row is gone, or the table: a write of some fields cannot
                recreate it, and makes no table.
            StaleVersionError: The row is at another version.
        """
        table = validate_table_name(table)

        def make() -> Optional[tuple[str, list[Any]]]:
            if expected_version is not None and not self._is_versioned(table) and self._has_version_column(table) is not None:
                self._ensure_versioned_table(table)  # a write by ``_id`` makes no table
            columns, layout, has = self._shape_of(table, promoted)
            return _fields_update(table, _id, doc, fields, columns, expected_version, layout, has)

        try:
            cur = self._sent(table, make, immediate=expected_version is not None)
        except sqlite3.OperationalError as exc:
            if not is_missing_table(exc, table):
                raise
            _refuse_unwritten(True, table, _id)
        if cur is None:
            return None
        if expected_version is None:
            self.adapter.auto_commit()
            if cur.rowcount > 0:
                return None
            _refuse_unwritten(True, table, _id)
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
        table = validate_table_name(table)
        self._ensure_table(table)
        return self._insert_many_chunked(table, docs)

    def insert_many_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[int]:
        """Multi-row INSERT splitting promoted fields into real columns.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            list[int]: Assigned ``_id`` for each document, preserving order.
        """
        if not docs:
            return []
        table = validate_table_name(table)
        for field in promoted_fields:
            validate_identifier(field)
        return self._insert_many_chunked(table, docs, promoted_fields=promoted_fields)

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
        table = validate_table_name(table)
        self._columns_of(table)  # an unsettled table is refused before it is given ``_version``
        self._ensure_versioned_table(table)
        ids = self._insert_many_chunked(table, docs)
        return [(id_, 0) for id_ in ids]

    def insert_many_with_version_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[tuple[int, int]]:
        """Multi-row INSERT with ``_version=0`` splitting promoted fields.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            list[tuple[int, int]]: ``(id, 0)`` pairs for each document.
        """
        if not docs:
            return []
        for field in promoted_fields:
            validate_identifier(field)
        self._ensure_versioned_table(table)
        ids = self._insert_many_chunked(table, docs, promoted_fields=promoted_fields)
        return [(id_, 0) for id_ in ids]

    def _insert_many_chunked(
        self,
        table: str,
        docs: list[dict[str, Any]],
        *,
        promoted_fields: list[str] | None = None,
    ) -> list[int]:
        """Internal: the multi-row INSERTs of ``docs`` into the validated ``table``, in one
        transaction (:func:`_inserts`), split as :meth:`_upsert` splits a document.
        """

        def make() -> Any:
            columns, layout, has = self._shape_of(table, promoted_fields or ())
            return _inserts(table, docs, has, columns, layout)

        with self.transaction():
            return self._sent_all(table, make)

    def _sent_all(self, table: str, make: Any) -> Any:
        """Send the statements of the steps ``make()`` makes (each answered with its cursor's
        ``lastrowid``) and return what they return: the first one, built from what this
        object remembers of ``table``, as :meth:`_sent` sends one (built and sent again when
        the table has other names now), the rest after it in the transaction it is in, whose
        write lock keeps the table as it is."""
        adapter = self.adapter
        while True:
            steps = make()
            sql, params, names = next(steps)
            try:
                cursor = adapter._execute_judged(sql, params)
                break
            except Exception as exc:
                if isinstance(exc, sqlite3.OperationalError) and self._has_moved(table, names):
                    continue
                end_failed(adapter, exc, table, sql)
        execute = adapter.execute
        try:
            while True:
                try:
                    last = cursor.lastrowid
                finally:
                    cursor.close()
                sql, params, _ = steps.send(last)
                cursor = execute(sql, params)
        except StopIteration as done:
            return done.value

    def bulk_upsert(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
        """Upsert multiple documents efficiently.

        New docs (without ``_id``) are inserted and receive ids. Existing docs
        (with ``_id``) are updated.

        Uses chunked multi-row INSERT statements (same pattern as
        ``_insert_many_chunked``) to minimize per-row Python overhead, in one
        transaction. A column whose key a document does not hold is not written: a
        new row gets its DEFAULT, an existing row keeps its value.

        Args:
            table: Table name.
            docs: List of documents. If an element contains ``_id``, it is
                treated as an update; otherwise, an insert.

        Returns:
            list[int]: The ``_id`` for each input document, preserving order.
        """
        table = validate_table_name(table)
        self._ensure_table(table)
        if not docs:
            return []
        def make() -> Any:
            columns, layout, has = self._shape_of(table)
            return _bulk_upserts(table, docs, has, columns, layout)

        with self.transaction():
            ids = self._sent_all(table, make)
        return _hand_ids(docs, ids)

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

    def _read_rows(self, table: str, ids: list, *, versioned: bool = False) -> tuple[Optional[list], list[str]]:
        """The by-id read every reader of this database goes through; it writes nothing.

        Returns the stored rows of ``ids`` (stored order) and the columns they were read
        with: the promoted columns of ``table`` (:meth:`_columns_of`), and ``_version`` where
        the table has it (:meth:`_shape_of`: the row's version is its column, never a key
        of its document) or the read is ``versioned``, read with the guard of what is
        remembered (:meth:`_sent`). A missing table gives ``None`` rows and is not created. A
        ``versioned`` read of a table without its ``_version`` column gives its rows at
        version 0 and keeps its schema: the first versioned write adds the column.
        """
        table = validate_table_name(table)
        n, params = _id_params(ids)
        adapter = self.adapter
        columns = _row_columns((), versioned)  # of the rows given
        try:
            while True:  # the loop of :meth:`_sent`, written here: every read comes through
                promoted, layout, has = self._shape_of(table)
                columns = _row_columns(promoted, has or versioned)
                read = columns if has or not versioned else _row_columns(promoted)
                sql = _rows_sql(table, read, n, layout)
                try:
                    cursor = adapter._execute_judged(sql, params)
                    break
                except Exception as exc:
                    if isinstance(exc, sqlite3.OperationalError) and self._has_moved(table, layout):
                        continue
                    end_failed(adapter, exc, table, sql)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if is_missing_table(exc, table):
                return None, columns  # a read creates no table
            raise
        return (rows if read is columns else [(r[0], r[1], 0, *r[2:]) for r in rows]), columns

    def _has_version_column(self, table: str) -> Optional[bool]:
        """Whether the validated ``table`` has its ``_version`` column; writes nothing.

        ``True`` when it has it, ``False`` when the table is there without it, ``None`` when
        there is no such table. Read from the names remembered for it, or else looked up with
        one ``PRAGMA table_xinfo`` and remembered (:meth:`_seen`); a missing table is not
        remembered.
        """
        if self._is_versioned(table):
            return True
        layout = self._layout_of(table)
        if layout is None:
            layout, names = self._names(table)
            if layout:
                self._seen(table, layout, names)
        return _has_version(layout) if layout else None

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
        """Return a SQLerQuery that reads ``table`` through this DB, as a model's query set does.

        Args:
            table: Table name.

        Returns:
            SQLerQuery: Query object you can chain and execute.
        """
        return SQLerQuery(table=validate_table_name(table), adapter=self.adapter)._clone(db=self)

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
        self._open.levels.append(_Level(set(), {}, {}, self.adapter._txn_depth, set()))

    def _end_level(self, commit: bool, *, leaving: Optional[BaseException] = None) -> None:
        """End this thread's innermost level; what it learned about tables goes with it.

        A commit moves it to the level above, or at the outermost level to the shared memory
        (``_merge``); a rollback, or a commit that fails, drops it. That runs no statement and
        raises nothing. The tables of cached results written in it are cached again however
        the level ends, or go on being held by the level above (``cache.level_ended``).
        ``leaving``: the caller's exception on its way out (see ``_end_transaction``).
        """
        levels = self._open.levels
        level = levels.pop() if levels else None  # none: begun on another thread; nothing to merge
        committed = False
        try:
            _end_transaction(self.adapter, commit, leaving)
            committed = commit
        finally:
            if level is not None and level.written:
                cache.level_ended(level.written, self._written_level() if committed else None)
        if committed and level is not None:
            self._merge(level)

    # ---- versioned (optimistic locking) helpers ----

    def _ensure_versioned_table(self, table: str) -> None:
        """Ensure the target table exists and has a ``_version`` column.

        This upgrades an existing non-versioned table by adding the column.

        Args:
            table: Table name.
        """
        self._ensure_table(table)
        # fast path: check without lock
        layout, names = self._names(table)
        added: tuple[str, ...] = ()
        if not _has_version(layout):
            # serialize DDL; re-check inside lock
            with self._ddl_lock:
                layout, names = self._names(table)
                if not _has_version(layout):
                    self.adapter.execute(f'ALTER TABLE "{table}" ADD COLUMN "_version" INTEGER NOT NULL DEFAULT 0;')
                    self.adapter.auto_commit()
                    added = ("_version",)
        self._seen(table, layout, names, added)

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
        return self._upsert_versioned(validate_table_name(table), _id, doc, expected_version)

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
