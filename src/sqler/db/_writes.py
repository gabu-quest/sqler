import functools
from typing import Any, NoReturn, Optional

from sqler._stored import dumps, stored
from sqler.db._guard import _and_guard, _check_sql, _guard
from sqler.db._rows import _MAX_VARIABLES
from sqler.exceptions import (
    InvalidFieldNameError,
    NotFoundError,
    StaleVersionError,
    UnstorableValueError,
)
from sqler.query.query import _MOVE_VERSION
from sqler.utils import (
    validate_column_name,
)


def _and_fields(columns: list[str], fields: Any) -> list[str]:
    """The promoted ``columns`` of a table, then each of the caller's ``fields`` they lack: a
    write names that one as a column, so SQLite refuses it while the table lacks it."""
    missing = [f for f in fields if f not in columns]
    return [*columns, *missing] if missing else columns


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


def _refuse_unwritten(gone: bool, table: str, _id: Any) -> NoReturn:
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
