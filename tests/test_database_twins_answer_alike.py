"""``SQLerDB`` and ``AsyncSQLerDB`` answer every call alike.

``CALLS`` is the one table: each row is a call, run on a ``SQLerDB`` and on an
``AsyncSQLerDB`` that hold the same three rows (``ROWS``, table ``us``). The two give the same
result and leave the same database (every table's schema and rows), or both raise the same
exception type with the same message. The table has a row for every public method the two
classes share, found by inspection, so a shared method missing from it fails
``test_every_shared_method_has_a_row``. A few results are also asserted literally
(``test_*_literally``), so the table cannot pass with both twins wrong in the same way.

A recipe is ``async`` so one table serves both: a sync call's result is used as it is, an
async call's is awaited (``_v``).
"""

import asyncio
import inspect
import warnings
from typing import Any

import pytest

from sqler import AsyncSQLerDB, SQLerDB

ROWS = [{"name": "ann", "age": 30}, {"name": "bob", "age": 30}, {"name": "cy", "age": 41}]
ALL = "SELECT _id, data FROM us ORDER BY _id"


async def _v(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _is_async(db: Any) -> bool:
    return isinstance(db, AsyncSQLerDB)


async def _in_transaction(db: Any, body: Any) -> Any:
    """``body(db)`` inside ``db.transaction()``, as each class enters it."""
    if _is_async(db):
        async with db.transaction():
            return await body(db)
    with db.transaction():
        return await body(db)


async def _state(db: Any) -> list:
    """Every table and index of ``db`` (name, kind, SQL) and the rows of each table, sorted,
    read with the adapter's own ``execute`` so the dump does not go through a call under test."""

    async def rows(sql: str) -> list:
        cur = await _v(db.adapter.execute(sql))
        try:
            got = [tuple(r) for r in await _v(cur.fetchall())]
        finally:
            await _v(cur.close())
        await _v(db.adapter.auto_commit())
        return got

    schema = await rows(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [name for kind, name, _, _ in schema if kind == "table"]
    return [schema, *[sorted(await rows(f'SELECT * FROM "{t}"'), key=repr) for t in tables]]


# ----- the calls ----------------------------------------------------------------------------


async def _promoted_table(db: Any) -> None:
    await _v(db.ensure_table("pt", promoted={"n": "INTEGER"}))


async def _fresh(db: Any, opened: Any) -> Any:
    await _v(opened.connect())
    try:
        await _v(opened.insert_document("t", {"a": 1}))
        return await _v(opened.find_document("t", 1))
    finally:
        await _v(opened.close())


async def _in_memory(db, tmp):
    return await _fresh(db, type(db).in_memory(shared=False))


async def _on_disk(db, tmp):
    return await _fresh(db, type(db).on_disk(str(tmp / "fresh.db")))


async def _close_then_connect(db, tmp):
    await _v(db.close())
    await _v(db.connect())
    return await _v(db.find_document("us", 1))


async def _move_promoted(db, tmp):
    await _promoted_table(db)
    await _v(db.insert_document("pt", {"n": 4, "x": 1}))
    return await _v(db.move_promoted("pt", "n", keep="column"))


async def _query(db, tmp):
    q = await _v(db.query("us"))
    return [await _v(q.count()), await _v(q.all())]


async def _query_missing(db, tmp):
    q = await _v(db.query("nope"))
    return [await _v(q.count()), await _v(q.all())]


async def _transaction_commit(db, tmp):
    async def body(d):
        return await _v(d.insert_document("us", {"name": "dan", "age": 9}))

    return await _in_transaction(db, body)


async def _transaction_rollback(db, tmp):
    async def body(d):
        await _v(d.insert_document("us", {"name": "dan", "age": 9}))
        raise KeyError("leave")

    return await _in_transaction(db, body)


def _sql(statement: str, params: Any = None):
    async def call(db, tmp):
        return await _v(db.execute_sql(statement, params))

    return call


def _call(name: str, *args: Any, before: Any = None, **kwargs: Any):
    async def call(db, tmp=None):
        if before is not None:
            await before(db)
        return await _v(getattr(db, name)(*args, **kwargs))

    return call


async def _promoted_and_versioned(db: Any) -> None:
    await _v(db.ensure_table("pt", promoted={"n": "INTEGER"}, versioned=True))


# (row id, the method it calls, the recipe)
CALLS = [
    ("in_memory", "in_memory", _in_memory),
    ("on_disk", "on_disk", _on_disk),
    ("connect", "connect", _call("connect")),
    ("close", "close", _close_then_connect),
    (
        "ensure_table",
        "ensure_table",
        _call("ensure_table", "t2", promoted={"n": "INTEGER NOT NULL DEFAULT 0"}, versioned=True),
    ),
    ("ensure_table-existing", "ensure_table", _call("ensure_table", "us", versioned=True)),
    ("ensure_table-bad-name", "ensure_table", _call("ensure_table", "1us")),
    (
        "ensure_table-not-null-no-default",
        "ensure_table",
        _call("ensure_table", "us", promoted={"n": "INTEGER NOT NULL"}),
    ),
    (
        "insert_many_promoted-missing-column",
        "insert_many_promoted",
        _call("insert_many_promoted", "us", [{"zz": 1}], ["zz"]),
    ),
    (
        "upsert_with_version-missing-table",
        "upsert_with_version",
        _call("upsert_with_version", "nope", 1, {"a": 1}, 0),
    ),
    (
        "find_document_with_version-versioned",
        "find_document_with_version",
        _call(
            "find_document_with_version",
            "us",
            1,
            before=_call("ensure_table", "us", versioned=True),
        ),
    ),
    ("query-missing-table", "query", _query_missing),
    (
        "create_index-bad-where",
        "create_index",
        _call("create_index", "us", "age", where="1; DROP TABLE us"),
    ),
    ("move_promoted", "move_promoted", _move_promoted),
    (
        "move_promoted-not-promoted",
        "move_promoted",
        _call("move_promoted", "us", "age", keep="document"),
    ),
    (
        "upsert_document_promoted",
        "upsert_document_promoted",
        _call(
            "upsert_document_promoted", "pt", None, {"n": 5, "x": 2}, ["n"], before=_promoted_table
        ),
    ),
    (
        "upsert_with_version_promoted",
        "upsert_with_version_promoted",
        _call(
            "upsert_with_version_promoted",
            "pt",
            None,
            {"n": 5, "x": 2},
            None,
            ["n"],
            before=_promoted_and_versioned,
        ),
    ),
    (
        "insert_document",
        "insert_document",
        _call("insert_document", "us", {"name": "dan", "age": 9}),
    ),
    (
        "insert_document-new-table",
        "insert_document",
        _call("insert_document", "other", {"k": [1, 2]}),
    ),
    (
        "upsert_document",
        "upsert_document",
        _call("upsert_document", "us", 2, {"name": "bo", "age": 31}),
    ),
    (
        "upsert_document-missing-row",
        "upsert_document",
        _call("upsert_document", "us", 9, {"name": "x"}),
    ),
    ("find_document", "find_document", _call("find_document", "us", 2)),
    ("find_document-missing-table", "find_document", _call("find_document", "nope", 1)),
    ("find_documents", "find_documents", _call("find_documents", "us", [3, 1, 9, 1])),
    ("delete_document", "delete_document", _call("delete_document", "us", 2)),
    ("insert_many", "insert_many", _call("insert_many", "us", [{"name": "e"}, {"name": "f"}])),
    (
        "insert_many_promoted",
        "insert_many_promoted",
        _call(
            "insert_many_promoted",
            "pt",
            [{"n": 1}, {"n": 2, "x": 3}],
            ["n"],
            before=_promoted_table,
        ),
    ),
    (
        "insert_many_with_version",
        "insert_many_with_version",
        _call("insert_many_with_version", "us", [{"name": "e"}]),
    ),
    (
        "insert_many_with_version_promoted",
        "insert_many_with_version_promoted",
        _call(
            "insert_many_with_version_promoted",
            "pt",
            [{"n": 1}, {"n": 2}],
            ["n"],
            before=_promoted_table,
        ),
    ),
    (
        "bulk_upsert",
        "bulk_upsert",
        _call("bulk_upsert", "us", [{"_id": 1, "name": "an"}, {"name": "g"}]),
    ),
    ("create_index", "create_index", _call("create_index", "us", "name", unique=True)),
    (
        "create_index-duplicate-values",
        "create_index",
        _call("create_index", "us", "age", unique=True),
    ),
    (
        "create_index-several-fields",
        "create_index",
        _call("create_index", "us", ["name", ("age", "desc")], where="_id > 0"),
    ),
    (
        "create_index-promoted-field",
        "create_index",
        _call("create_index", "pt", ["x", ("n", "desc")], before=_promoted_table),
    ),
    ("create_index-empty-list", "create_index", _call("create_index", "us", [])),
    ("create_index-bad-direction", "create_index", _call("create_index", "us", [("age", "down")])),
    (
        "create_index-follows-promotion",
        "create_index",
        _call(
            "ensure_table",
            "us",
            promoted={"age": "INTEGER"},
            before=_call("create_index", "us", ["name", ("age", "desc")]),
        ),
    ),
    (
        "drop_index",
        "drop_index",
        _call("drop_index", "idx_us_name", before=_call("create_index", "us", "name")),
    ),
    (
        "list_indexes",
        "list_indexes",
        _call("list_indexes", "us", before=_call("create_index", "us", "age")),
    ),
    (
        "list_indexes-all",
        "list_indexes",
        _call("list_indexes", before=_call("create_index", "us", "age")),
    ),
    (
        "index_exists",
        "index_exists",
        _call("index_exists", "idx_us_age", before=_call("create_index", "us", "age")),
    ),
    ("index_exists-not", "index_exists", _call("index_exists", "idx_none")),
    (
        "upsert_with_version",
        "upsert_with_version",
        _call("upsert_with_version", "us", None, {"name": "v"}, None),
    ),
    (
        "upsert_with_version-stale",
        "upsert_with_version",
        _call("upsert_with_version", "us", 1, {"name": "v"}, 7),
    ),
    (
        "find_document_with_version",
        "find_document_with_version",
        _call("find_document_with_version", "us", 1),
    ),
    ("query", "query", _query),
    ("transaction", "transaction", _transaction_commit),
    ("transaction-rollback", "transaction", _transaction_rollback),
    # execute_sql: the seven statements of a raw read, then the hostile ones
    ("execute_sql-id-data", "execute_sql", _sql(ALL)),
    ("execute_sql-id", "execute_sql", _sql("SELECT _id FROM us ORDER BY _id")),
    ("execute_sql-count", "execute_sql", _sql("SELECT COUNT(*) AS n FROM us")),
    (
        "execute_sql-two-fields",
        "execute_sql",
        _sql(
            "SELECT json_extract(data, '$.name') AS name, json_extract(data, '$.age') AS age FROM us ORDER BY _id"
        ),
    ),
    (
        "execute_sql-group",
        "execute_sql",
        _sql(
            "SELECT json_extract(data, '$.age') AS age, COUNT(*) AS n FROM us GROUP BY 1 ORDER BY 1"
        ),
    ),
    (
        "execute_sql-id-field",
        "execute_sql",
        _sql("SELECT _id, json_extract(data, '$.name') AS name FROM us ORDER BY _id"),
    ),
    ("execute_sql-data-id", "execute_sql", _sql("SELECT data, _id FROM us ORDER BY _id")),
    ("execute_sql-text-first", "execute_sql", _sql("SELECT 'x' AS k, _id FROM us ORDER BY _id")),
    (
        "execute_sql-unnamed",
        "execute_sql",
        _sql("SELECT json_extract(data, '$.name'), 1 + 1 FROM us ORDER BY _id"),
    ),
    (
        "execute_sql-params",
        "execute_sql",
        _sql("SELECT _id FROM us WHERE json_extract(data, '$.age') = ?", [30]),
    ),
    ("execute_sql-no-rows", "execute_sql", _sql("SELECT _id, data FROM us WHERE _id > 99")),
    ("execute_sql-same-name-twice", "execute_sql", _sql("SELECT 1 AS n, 2 AS n")),
    (
        "execute_sql-data-json-null",
        "execute_sql",
        _sql(
            "SELECT _id, CASE WHEN _id = 2 THEN 'null' ELSE data END AS data FROM us ORDER BY _id"
        ),
    ),
    (
        "execute_sql-data-json-list",
        "execute_sql",
        _sql(
            "SELECT _id, CASE WHEN _id = 2 THEN '[1, 2]' ELSE data END AS data FROM us ORDER BY _id"
        ),
    ),
    ("execute_sql-pragma", "execute_sql", _sql("PRAGMA table_info('us')")),
    ("execute_sql-write", "execute_sql", _sql("DELETE FROM us")),
    ("execute_sql-two-statements", "execute_sql", _sql("SELECT 1; SELECT 2")),
    ("execute_sql-missing-table", "execute_sql", _sql("SELECT _id FROM nope")),
]


async def _outcome(kind: type, recipe: Any, tmp: Any) -> tuple:
    """What ``recipe`` gives on a new database of class ``kind`` holding ``ROWS``: its result,
    or its exception's type and message; then the database it leaves."""
    db = kind.in_memory(shared=False)
    await _v(db.connect())
    try:
        for row in ROWS:
            await _v(db.insert_document("us", dict(row)))
        try:
            got: tuple = ("result", await recipe(db, tmp))
        except Exception as exc:
            got = ("raised", type(exc), str(exc))
        return got, await _state(db)
    finally:
        await _v(db.close())


def _both(recipe: Any, tmp_path: Any) -> tuple:
    async def run() -> tuple:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            (tmp_path / "sync").mkdir()
            (tmp_path / "async").mkdir()
            sync = await _outcome(SQLerDB, recipe, tmp_path / "sync")
            asynchronous = await _outcome(AsyncSQLerDB, recipe, tmp_path / "async")
        return sync, asynchronous

    return asyncio.run(run())


def _shared_methods() -> set[str]:
    def public(cls: type) -> set[str]:
        return {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))}

    return public(SQLerDB) & public(AsyncSQLerDB)


def test_every_shared_method_has_a_row():
    assert {method for _, method, _ in CALLS} == _shared_methods()


def test_the_row_ids_are_unique():
    ids = [row_id for row_id, _, _ in CALLS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("recipe", [r for _, _, r in CALLS], ids=[i for i, _, _ in CALLS])
def test_the_twins_answer_alike(recipe, tmp_path):
    sync, asynchronous = _both(recipe, tmp_path)
    assert asynchronous == sync


def test_a_count_reads_as_its_column_literally(tmp_path):
    sync, asynchronous = _both(_sql("SELECT COUNT(*) AS n FROM us"), tmp_path)
    assert (sync[0], asynchronous[0]) == (("result", [{"n": 3}]), ("result", [{"n": 3}]))


def test_a_group_by_reads_every_column_literally(tmp_path):
    recipe = _sql(
        "SELECT json_extract(data, '$.age') AS age, COUNT(*) AS n FROM us GROUP BY 1 ORDER BY 1"
    )
    sync, asynchronous = _both(recipe, tmp_path)
    expected = ("result", [{"age": 30, "n": 2}, {"age": 41, "n": 1}])
    assert (sync[0], asynchronous[0]) == (expected, expected)


def test_an_index_over_several_fields_is_made_where_each_is_stored_literally(tmp_path):
    recipe = _call(
        "ensure_table",
        "us",
        promoted={"age": "INTEGER"},
        before=_call("create_index", "us", ["name", ("age", "desc")]),
    )
    sync, asynchronous = _both(recipe, tmp_path)
    expected = [
        (
            "index",
            "idx_us_name__age_desc",
            "us",
            "CREATE INDEX idx_us_name__age_desc ON us (json_extract(data, '$.name'), age DESC) ",
        ),
    ]
    assert (
        [r for r in sync[1][0] if r[0] == "index"],
        [r for r in asynchronous[1][0] if r[0] == "index"],
    ) == (expected, expected)


def test_documents_read_by_data_and_id_in_any_order_literally(tmp_path):
    sync, asynchronous = _both(_sql("SELECT data, _id FROM us ORDER BY _id"), tmp_path)
    expected = ("result", [{**row, "_id": i} for i, row in enumerate(ROWS, start=1)])
    assert (sync[0], asynchronous[0]) == (expected, expected)


def test_a_document_that_is_no_object_raises_the_same_type_error_literally(tmp_path):
    recipe = _sql(
        "SELECT _id, CASE WHEN _id = 2 THEN '[1, 2]' ELSE data END AS data FROM us ORDER BY _id"
    )
    sync, asynchronous = _both(recipe, tmp_path)
    expected = ("raised", TypeError, "list indices must be integers or slices, not str")
    assert (sync[0], asynchronous[0]) == (expected, expected)
