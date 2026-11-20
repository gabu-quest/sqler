"""Referential integrity utilities for SQLer models.

Which rows refer to a row is decided here, once, for the sync and the async ways alike: a
referrer is a row that holds a reference (``{"_table": ..., "_id": ...}``) to the row at a
position its class declares as a model, the positions the loader follows
(:func:`sqler.models._refs.typed_fields`). A dict of that shape anywhere else is data.

The look reads only the tables of the registered classes that can hold such a reference,
one statement per table for any number of target rows, and a delete of a row of a class no
class can refer to reads nothing.
"""

import json
import operator
import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Collection, Generator, Optional, Union

from sqler import registry
from sqler.models import _refs, _write_rules
from sqler.query.query import is_missing_table
from sqler.utils import is_ref_dict, validate_table_name

if TYPE_CHECKING:
    from sqler.db.sqler_db import SQLerDB

Referrer = tuple[str, int, dict]
# a read the look asks for: ``(table, sql, params)``; it is sent the rows, or None when the
# table does not exist in the database
Read = tuple[str, str, list]
# what a read gives: its rows; None for a table that is not there; the error of SQLite's
# JSON functions (see failed_read)
Rows = Union[list, sqlite3.OperationalError, None]


# ----- which tables can refer to a table --------------------------------------------------

# the position of a class whose annotations are not all known yet: a reference anywhere
_ANYWHERE = _refs.Position((object,), None, None)

# target table -> (registry state, its answer)
_REFERRING: dict[str, tuple[int, tuple]] = {}


def _can_hold(at: _refs.Position, target: type) -> bool:
    """True when ``at``, or a list entry or dict value under it, admits a row of ``target``."""
    if at.admits and issubclass(target, at.admits):
        return True
    return any(inner is not None and _can_hold(inner, target) for inner in (at.item, at.value))


def _joined(a: Optional[_refs.Position], b: Optional[_refs.Position]) -> Optional[_refs.Position]:
    """The position that admits what ``a`` admits and what ``b`` admits (one field of two
    classes whose rows share a table)."""
    if a is None or b is None:
        return a or b
    if a == b:
        return a
    return _refs.Position(
        tuple(dict.fromkeys(a.admits + b.admits)),
        _joined(a.item, b.item),
        _joined(a.value, b.value),
    )


def _referring(target_table: str) -> tuple:
    """``(table, fields)`` for each table whose rows can hold a reference to a row of
    ``target_table``: ``fields`` is ``{field: Position}`` of the fields that can hold one,
    the positions of every class whose rows live in the table (``registry.holders()``)
    joined, or None when one of them has annotations that do not all resolve yet (read
    whole).

    Worked out once per state of the registry. An answer that holds an unresolved class is
    not kept: the class may resolve with no registration."""
    state = registry.state()
    kept = _REFERRING.get(target_table)
    if kept is not None and kept[0] == state:
        return kept[1]
    target = registry.resolve(target_table)
    found: list = []
    complete = True
    if isinstance(target, type):
        for table, classes in registry.holders().items():
            held: dict = {}
            fields: Optional[dict] = held
            for cls in classes:
                typed = _refs.known_typed_fields(cls)
                if typed is None:
                    fields = None
                    break
                for name, at in typed.items():
                    if _can_hold(at, target):
                        held[name] = _joined(held.get(name), at)
            if fields is None:
                complete = False
                found.append((table, None))
            elif fields:
                found.append((table, fields))
    answer = tuple(found)
    if complete:
        _REFERRING[target_table] = (state, answer)
    return answer


def referable(table: str) -> bool:
    """Whether a registered class can hold a reference to a row of ``table``."""
    return bool(_referring(table))


# ----- the look ---------------------------------------------------------------------------


def _ids(target_id: Union[int, Collection[int]]) -> frozenset:
    """The ids as plain integers (``True`` is 1, as it is where ids are compared)."""
    return frozenset(map(operator.index, [target_id] if isinstance(target_id, int) else target_id))


def _hit(target_table: str, ids: frozenset) -> Callable[[dict, tuple], bool]:
    """Whether a reference at a position admitting ``admits`` is to one of ``ids`` of
    ``target_table``."""
    allowed = registry.get_allowed_tables(target_table)
    target = registry.resolve(target_table)

    def hit(ref: dict, admits: tuple) -> bool:
        key = _refs.ref_key(ref)
        return (
            key is not None
            and key[0] in allowed
            and key[1] in ids
            and (not isinstance(target, type) or issubclass(target, admits))
        )

    return hit


def _swap(value: Any, at: _refs.Position, hit: Callable, path: str, paths: list) -> Any:
    """``value`` with None in place of each reference ``hit`` takes at a position ``at``
    puts a model; the path of each is added to ``paths``. A reference is not walked into,
    as the loader does not (but at :data:`_ANYWHERE`, where nothing is known)."""
    anywhere = at is _ANYWHERE
    if isinstance(value, dict):
        if is_ref_dict(value):
            if at.admits and hit(value, at.admits):
                paths.append(path or "$")
                return None
            if not anywhere:
                return value
        inner = at if anywhere else at.value
        if inner is None:
            return value
        return {
            k: _swap(v, inner, hit, f"{path}.{k}" if path else k, paths) for k, v in value.items()
        }
    if isinstance(value, list):
        inner = at if anywhere else at.item
        if inner is None:
            return value
        return [_swap(v, inner, hit, f"{path}[{i}]", paths) for i, v in enumerate(value)]
    return value


def _swap_doc(doc: dict, fields: Optional[dict], hit: Callable, paths: list) -> dict:
    """:func:`_swap` of each of ``fields`` in ``doc`` (``fields`` None: the whole of it)."""
    if fields is None:
        return _swap(doc, _ANYWHERE, hit, "", paths)
    return {k: _swap(v, fields[k], hit, k, paths) if k in fields else v for k, v in doc.items()}


# the text holds a backslash-u escape: a name may be spelled with one
_ESCAPED = "data GLOB '*\\u*'"


# the row is stored as a BLOB (a BLOB sorts after every other value, the empty one first)
_BLOB = "data >= x''"


def _text_test(names: list, one: bool) -> str:
    """The test on the text of ``data`` that every row holding a usable reference to one of
    the ids in ``?1`` passes (``one``: ``?1`` holds one id), by a table named one of
    ``names``. A usable ``_id`` is a JSON integer (:func:`_refs.ref_key`): one spelling, no
    escapes, so the text holds its digits. Its ``_table`` equals one of ``names``, and a JSON
    string spells an identifier character only as itself or as a backslash-u escape, so the
    text holds one of them or a backslash-u. A name that is not an identifier tests nothing.
    A row stored as a BLOB passes untested: its bytes need not spell the text as GLOB reads
    it (UTF-16), and a build of SQLite can match no BLOB with GLOB."""
    terms = []
    if one:  # '[5]' -> '5'
        terms.append("data GLOB ('*' || substr(?1, 2, length(?1) - 2) || '*')")
    if all(name.isidentifier() for name in names):
        spelled = [f"data GLOB '*{name}*'" for name in names]
        terms.append(f"({' OR '.join([*spelled, _ESCAPED])})")
    return f"(({' AND '.join(terms)}) OR {_BLOB})" if terms else ""


def _look_sql(table: str, fields: Optional[dict], text: str, json_terms: bool = True) -> str:
    """The SELECT of the rows of ``table`` that may hold a reference to one of the ids in
    ``?1`` (a JSON array) at ``fields``: those that pass ``text`` (:func:`_text_test`, before
    any JSON function), then the JSON terms. A field that is the reference itself is read
    with ``json_extract(data, '$.<field>._id')``, which an index on that expression serves
    (``db.create_index(table, "<field>._id")``); a list or a dict is walked with
    ``json_tree``. The positions are decided exactly afterwards (:func:`_swap`).

    SQLite's JSON functions raise on a row they cannot read, and no test in SQL tells such a
    row from one Python reads (``NaN``, ``Infinity``). So no row is passed over for being
    unreadable: the statement fails, and the look is sent again with ``json_terms`` False,
    the text test alone, and decided in Python (:func:`_look`)."""
    if not json_terms:
        return f"SELECT _id, data FROM {table}{' WHERE ' + text if text else ''};"
    ids = "(SELECT value FROM json_each(?1))"

    def tree(name: str = "") -> str:
        root = f", '$.{name}'" if name else ""
        return f"EXISTS (SELECT 1 FROM json_tree({table}.data{root}) WHERE key = '_id' AND value IN {ids})"

    if fields is None or not all(name.isidentifier() for name in fields):
        terms = [tree()]
    else:
        terms = [
            f"json_extract(data, '$.{name}._id') IN {ids}"
            if at.item is None and at.value is None
            else tree(name)
            for name, at in fields.items()
        ]
    where = f"{text} AND " if text else ""
    return f"SELECT _id, data FROM {table} WHERE {where}({' OR '.join(terms)});"


def _look(target_table: str, target_id) -> Generator[Read, Rows, list]:
    """The referrers of the rows ``target_id`` (one id or a collection) of ``target_table``:
    one read per table that can refer to it, whatever the number of ids."""
    ids = _ids(target_id)
    found: list[Referrer] = []
    if not ids:
        return found
    hit = _hit(target_table, ids)
    text = _text_test(sorted(registry.get_allowed_tables(target_table)), len(ids) == 1)
    params = [json.dumps(sorted(ids))]
    text_params = params if "?1" in text else []  # the second statement is the text test alone
    for table, fields in _referring(target_table):
        rows = yield table, _look_sql(table, fields, text), params
        if isinstance(rows, sqlite3.OperationalError):
            rows = yield table, _look_sql(table, fields, text, json_terms=False), text_params
            if isinstance(rows, sqlite3.OperationalError):
                raise rows  # this statement calls no JSON function: the error is not a row's
        for row_id, data in rows or ():
            try:
                doc = json.loads(data)
            except ValueError:
                continue  # no load reads this row either
            paths: list[str] = []
            if fields is None or isinstance(doc, dict):
                _swap_doc(doc, fields, hit, paths)
            if paths:
                found.append((table, int(row_id), {"paths": paths}))
    return found


def _nulled(target_table: str, target_id) -> Callable[[str, dict], dict]:
    """``null(table, doc)``: ``doc``, a row of ``table``, with None in place of each of its
    references to ``target_id`` of ``target_table``."""
    hit = _hit(target_table, _ids(target_id))
    fields = dict(_referring(target_table))
    return lambda table, doc: _swap_doc(doc, fields.get(table, {}), hit, [])


def answers(exc: Exception, table: str) -> bool:
    """True when ``exc``, the failure of a read of the look, is an answer to it: ``table`` is
    not there, or it may be SQLite's JSON functions failing on a row they cannot read (every
    message of theirs names JSON).

    The look sends its reads with the adapter's ``_execute_judged``, so an answer ends
    nothing that was pending on the connection; any other failure ends what a failed
    statement ends (``_end_failed_statement``) and is raised."""
    return is_missing_table(exc, table) or (
        isinstance(exc, sqlite3.OperationalError) and "JSON" in str(exc)
    )


def failed_read(exc: Exception, table: str) -> Rows:
    """What a read of the look whose failure ``exc`` :func:`answers` it gives: None for a
    table that is not there; ``exc`` itself for :func:`_look` to read the table without
    SQLite's JSON functions."""
    assert isinstance(exc, sqlite3.OperationalError)  # :func:`answers` holds only for one
    return None if is_missing_table(exc, table) else exc


def _read(db: "SQLerDB", read: Read) -> Rows:
    table, sql, params = read
    try:
        return db.adapter._execute_judged(sql, params).fetchall()
    except Exception as exc:
        if not answers(exc, table):
            db.adapter._end_failed_statement(exc)
        return failed_read(exc, table)


def find_referrers(db: "SQLerDB", target_table: str, target_id) -> list[Referrer]:
    """Find the rows that refer to the target rows.

    A row refers to a target row when it holds a reference to it at a position its class
    declares as a model. Only the tables of classes that can hold such a reference are read,
    each once.

    Args:
        db: The database instance to search.
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
            read = look.send(_read(db, read))
    except StopIteration as done:
        return done.value


def find_ref_paths(data: dict, allowed_tables: set[str], target_id: int) -> list[str]:
    """Find the JSON paths in ``data`` holding a reference to ``target_id`` of one of
    ``allowed_tables``, at any position, as :func:`validate_references` reads them (this
    takes no class: :func:`find_referrers` looks only at the positions a class declares as
    a model). A dict that is not a reference (:func:`sqler.utils.is_ref_dict`) is data, and
    is only walked into.

    Args:
        data: The document to search.
        allowed_tables: Set of table names that are valid reference targets.
        target_id: The row ``_id`` we're looking for.

    Returns:
        List of JSON paths (e.g., ``['author', 'comments[0].user']``), ``'$'`` for
        ``data`` itself.
    """
    paths: list[str] = []

    def walk(value, path: str):
        if isinstance(value, dict):
            if (
                is_ref_dict(value)
                and value["_table"] in allowed_tables
                and value["_id"] == target_id
            ):
                paths.append(path or "$")
            for k, v in value.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")

    walk(data, "")
    return paths


def set_null_referrers(
    db: "SQLerDB", target_table: str, target_id, referrers: list[Referrer]
) -> None:
    """Null the references to the target rows in the referring rows.

    Each reference to the target (one ``_id`` or a collection) at a position the referring
    row's class declares as a model becomes ``None``. A row whose table has a registered
    class meets that class's rules: its update stamps and its audit row.

    Args:
        db: The database instance.
        target_table: Table name of the targets being deleted.
        target_id: Row ``_id`` of the target being deleted, or a collection of them.
        referrers: List of ``(table, row_id, metadata)`` from :func:`find_referrers`.
    """
    for table, _row_id, _meta in referrers:
        validate_table_name(table)
    null = _nulled(target_table, target_id)
    for table, row_id, _meta in referrers:
        row = db.adapter.execute(
            f"SELECT _id, data FROM {table} WHERE _id = ?;", [row_id]
        ).fetchone()
        if row:
            _write_rules.null_referrer(db, table, row_id, null(table, json.loads(row[1])))


def _by_table(level: list[Referrer], visited: set) -> dict[str, list[int]]:
    """The rows of ``level`` not yet in ``visited``, by table; they are added to it."""
    rows: dict[str, list[int]] = {}
    for table, row_id, _ in level:
        if (table, row_id) not in visited:
            visited.add((table, row_id))
            rows.setdefault(table, []).append(row_id)
    return rows


def cascade_delete(db: "SQLerDB", referrers: list[Referrer], visited: set[tuple[str, int]]) -> None:
    """Delete the referring rows and, level by level, the rows that refer to them.

    Each level is deleted, then its referrers are looked for: one look per table of the
    level. A row whose table has a registered class with an audit log gets its ``delete``
    row.

    Args:
        db: The database instance.
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
                _write_rules.delete_referrer(db, table, row_id)
        level = [ref for table, ids in rows.items() for ref in find_referrers(db, table, ids)]


def validate_references(db: "SQLerDB"):
    """Validate all references across all tables in the database.

    Scans all registered tables and checks that every reference
    (``{"_table": ..., "_id": ...}``) points to an existing row.

    Args:
        db: The database instance to validate.

    Returns:
        List of :class:`BrokenRef` instances describing dangling references.
    """
    from . import BrokenRef

    broken: list[BrokenRef] = []

    for table in registry.tables().keys():
        exists = db.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table]
        ).fetchone()
        if not exists:
            continue
        cur = db.adapter.execute(f"SELECT _id, data FROM {table};")
        for _id, data_json in cur.fetchall():
            try:
                data = json.loads(data_json)
            except (json.JSONDecodeError, TypeError):
                # Skip rows with invalid JSON
                continue

            def walk(value, path: str):
                if is_ref_dict(value):
                    t, i = value["_table"], value["_id"]
                    # Validate table name to prevent SQL injection
                    if not _refs._is_table_name(t):
                        # Invalid table name in reference - treat as broken
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
                    # check existence
                    cur2 = db.adapter.execute(f"SELECT 1 FROM {t} WHERE _id = ?;", [i])
                    if not cur2.fetchone():
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
                        walk(v, f"{path}.{k}" if path else k)
                elif isinstance(value, list):
                    for idx, v in enumerate(value):
                        walk(v, f"{path}[{idx}]")

            walk(data, "")

    return broken
