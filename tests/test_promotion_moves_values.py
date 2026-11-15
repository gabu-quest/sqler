"""Promoting a field on a table that has rows moves each row's value from its document into the column.

The rule: no row holds a promoted column's key in its document, and every row reads through its
model what it read before the promotion; where that cannot be kept, the promotion raises
``SchemaError`` and nothing has changed. Each test runs over every way a table gets a promoted
column (``ensure_table``, ``set_db`` of each base, the first save of each base, the async
``_ensure_schema``), sync and async.

Every table is built first with ``sqlite3`` alone, as the rows of a class that did not promote
the field left it: ``tickets(_id, data)`` with the documents given.
"""

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

import pytest
from sqler import AsyncSQLerDB, F, SQLerDB
from sqler.adapter import SQLiteAdapter
from sqler.exceptions import SchemaError, TableNotFoundError
from sqler.logging import query_logger
from sqler.models import (
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TABLE = "tickets"


# ---------------------------------------------------------------- the table as it was left


def _legacy(path, docs, columns=None, values=None):
    """``tickets`` with ``docs`` (ids 1, 2, ...); ``columns`` ``{name: definition}`` added the
    way an earlier version added them (the values left in the documents), and ``values``
    ``{_id: {column: value}}`` written into those columns since."""
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {TABLE} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)")
    for doc in docs:
        conn.execute(f"INSERT INTO {TABLE} (data) VALUES (json(?))", [json.dumps(doc)])
    for name, definition in (columns or {}).items():
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {definition}")
    for _id, cols in (values or {}).items():
        for name, value in cols.items():
            conn.execute(f"UPDATE {TABLE} SET {name} = ? WHERE _id = ?", [value, _id])
    conn.commit()
    conn.close()


def _raw(path, table=TABLE):
    """``(column names, rows ordered by _id)``, read with ``sqlite3`` alone."""
    conn = sqlite3.connect(path)
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        return cols, conn.execute(f"SELECT * FROM {table} ORDER BY _id").fetchall()
    finally:
        conn.close()


def _records(path):
    """What the database records as settled: ``[(table, column)]``; none when it records nothing."""
    conn = sqlite3.connect(path)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = '_sqler_promoted'").fetchone() is None:
            return []
        return conn.execute("SELECT tbl, col FROM _sqler_promoted ORDER BY tbl, col").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- the models


def _pydantic(base):
    def make(promoted, default):
        class Ticket(base):
            __tablename__ = TABLE
            __promoted__ = promoted
            name: str = ""
            status: Optional[str] = default

        return Ticket

    return make


def _lite(base):
    def make(promoted, default):
        @dataclass
        class Ticket(base):
            __tablename__ = TABLE
            __promoted__ = promoted
            name: str = ""
            status: Optional[str] = field(default_factory=lambda: default)

        return Ticket

    return make


def _msgspec(promoted, default):
    class Ticket(SQLerMsgspecModel):
        __tablename__ = TABLE
        __promoted__ = promoted
        name: str = ""
        status: Optional[str] = default

    return Ticket


class Run:
    """One database file, its sync object and, when asked, its async object on its own loop."""

    def __init__(self, path):
        self.path = path
        self.loop = asyncio.new_event_loop()
        self._db = None
        self._adb = None

    @property
    def db(self):
        if self._db is None:
            self._db = SQLerDB.on_disk(self.path)
        return self._db

    @property
    def adb(self):
        if self._adb is None:
            self._adb = AsyncSQLerDB.on_disk(self.path)
            self.go(self._adb.connect())
        return self._adb

    def go(self, coro):
        return self.loop.run_until_complete(coro)

    def close(self):
        if self._db is not None:
            self._db.close()
        if self._adb is not None:
            self.go(self._adb.close())
        self.loop.close()


@pytest.fixture
def run(tmp_path):
    r = Run(str(tmp_path / "p.db"))
    yield r
    r.close()


class Way:
    """One way a table gets its promoted column. ``model``: how its class is made (``None``: no
    model, the database's ``ensure_table``); ``is_async``: the class or database is async."""

    def __init__(self, name, model, is_async, promote):
        self.name, self.model, self.is_async, self._promote = name, model, is_async, promote

    def __repr__(self):
        return self.name

    def promote(self, run, promoted, default="unset"):
        """Promote ``promoted`` on ``tickets`` this way; returns the class (or ``None``)."""
        cls = self.model(promoted, default) if self.model else None
        self._promote(run, cls, promoted)
        return cls

    def db(self, run):
        return run.adb if self.is_async else run.db

    def remembered(self, run):
        return self.db(run)._promoted_of(TABLE)

    # what a row reads, through its model, or through the database where there is none
    def status_of(self, run, cls, _id):
        if cls is None:
            doc = run.go(run.adb.find_document(TABLE, _id)) if self.is_async else run.db.find_document(TABLE, _id)
            return doc.get("status", "<no key>")
        cls.set_db(self.db(run))
        row = run.go(cls.from_id(_id)) if self.is_async else cls.from_id(_id)
        return row.status

    def names(self, run, cls, where=None, order=None):
        cls.set_db(self.db(run))
        q = cls.query()
        if where is not None:
            q = q.filter(F("status") == where)
        if order is not None:
            q = q.order_by(order)
        rows = run.go(q.all()) if self.is_async else q.all()
        return [r.name for r in rows]


def _ensure(run, cls, promoted):
    run.db.ensure_table(TABLE, promoted=promoted)


def _aensure(run, cls, promoted):
    run.go(run.adb.ensure_table(TABLE, promoted=promoted))


def _set_db(run, cls, promoted):
    cls.set_db(run.db)


def _save(run, cls, promoted):
    cls(name="z", status="new").save(db=run.db)


def _aschema(run, cls, promoted):
    cls.set_db(run.adb)
    run.go(cls._ensure_schema())


def _asave(run, cls, promoted):
    run.go(cls(name="z", status="new").save(db=run.adb))


WAYS = [
    Way("ensure_table", None, False, _ensure),
    Way("pydantic.set_db", _pydantic(SQLerModel), False, _set_db),
    Way("pydantic.save", _pydantic(SQLerModel), False, _save),
    Way("safe.set_db", _pydantic(SQLerSafeModel), False, _set_db),
    Way("safe.save", _pydantic(SQLerSafeModel), False, _save),
    Way("msgspec.set_db", _msgspec, False, _set_db),
    Way("msgspec.save", _msgspec, False, _save),
    Way("lite.set_db", _lite(SQLerLiteModel), False, _set_db),
    Way("lite.save", _lite(SQLerLiteModel), False, _save),
    Way("lite_safe.set_db", _lite(SQLerLiteSafeModel), False, _set_db),
    Way("lite_safe.save", _lite(SQLerLiteSafeModel), False, _save),
    Way("async.ensure_table", None, True, _aensure),
    Way("async.ensure_schema", _pydantic(AsyncSQLerModel), True, _aschema),
    Way("async.save", _pydantic(AsyncSQLerModel), True, _asave),
    Way("async_safe.save", _pydantic(AsyncSQLerSafeModel), True, _asave),
    Way("async_lite.save", _lite(AsyncSQLerLiteModel), True, _asave),
    Way("async_lite_safe.save", _lite(AsyncSQLerLiteSafeModel), True, _asave),
]
MODEL_WAYS = [w for w in WAYS if w.model is not None]


def _first_two(path):
    """The columns but ``_version`` (a safe model adds it), and ``(_id, data, status)`` of rows 1
    and 2 (a way that saves adds row 3)."""
    cols, rows = _raw(path)
    return [c for c in cols if c != "_version"], [r[:3] for r in rows if r[0] <= 2]


# ---------------------------------------------------------------- 1. the values move with the column


@pytest.mark.parametrize("way", WAYS, ids=repr)
def test_values_move_into_the_column(run, way):
    _legacy(run.path, [{"name": "a", "status": "open"}, {"name": "b", "status": "done"}])
    cls = way.promote(run, {"status": "TEXT"})
    assert _first_two(run.path) == (
        ["_id", "data", "status"],
        [(1, '{"name":"a"}', "open"), (2, '{"name":"b"}', "done")],
    )
    assert [way.status_of(run, cls, i) for i in (1, 2)] == ["open", "done"]
    assert way.remembered(run) == ["status"]
    assert _records(run.path) == [(TABLE, "status")]
    if cls is not None:
        assert way.names(run, cls, where="open") == ["a"]
        assert [n for n in way.names(run, cls, order="status") if n != "z"] == ["b", "a"]


class Color(Enum):
    RED = "red"


STORED_FORMS = [
    ("text", "abc", "TEXT"),
    ("int", 42, "INTEGER"),
    ("float", 1.5, "REAL"),
    ("whole float in INTEGER", 2.0, "INTEGER"),
    ("whole float in REAL", 2.0, "REAL"),
    ("true", True, "INTEGER"),
    ("false", False, "INTEGER"),
    ("none", None, "TEXT"),
    ("datetime", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), "TEXT"),
    ("date", date(2026, 1, 2), "TEXT"),
    ("decimal fraction", Decimal("1.25"), "REAL"),
    ("decimal whole", Decimal("3"), "INTEGER"),
    ("timedelta whole", timedelta(seconds=90), "INTEGER"),
    ("timedelta fraction", timedelta(microseconds=1500), "REAL"),
    ("enum", Color.RED, "TEXT"),
    ("int in TEXT", 7, "TEXT"),
    ("min int64", -(2**63), "INTEGER"),
]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(("value", "definition"), [s[1:] for s in STORED_FORMS], ids=[s[0] for s in STORED_FORMS])
def test_moved_value_is_what_a_save_writes(run, is_async, value, definition):
    """The moved row and a row saved after the promotion hold the same, column by column."""
    promoted = {"v": definition}
    if is_async:
        db = run.adb
        run.go(db.insert_document("vals", {"k": 1, "v": value}))
        run.go(db.ensure_table("vals", promoted=promoted))
        run.go(db.upsert_document_promoted("vals", None, {"k": 1, "v": value}, ["v"]))
    else:
        db = run.db
        db.insert_document("vals", {"k": 1, "v": value})
        db.ensure_table("vals", promoted=promoted)
        db.upsert_document_promoted("vals", None, {"k": 1, "v": value}, ["v"])
    conn = sqlite3.connect(run.path)
    rows = conn.execute("SELECT data, v, typeof(v) FROM vals ORDER BY _id").fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0] == rows[1]


# ---------------------------------------------------------------- 2. what a row reads does not change


@pytest.mark.parametrize("way", WAYS, ids=repr)
def test_row_without_the_key_reads_the_same(run, way):
    """Row 1 has no key (saved before the field existed), row 2 holds JSON null."""
    _legacy(run.path, [{"name": "a"}, {"name": "b", "status": None}])
    cls = way.promote(run, {"status": "TEXT DEFAULT 'pending'"}, default="unset")
    # with a model: its default, as the row read it before; without one: the column's DEFAULT
    fill = "unset" if cls is not None else "pending"
    assert _first_two(run.path) == (
        ["_id", "data", "status"],
        [(1, '{"name":"a"}', fill), (2, '{"name":"b"}', None)],
    )
    assert [way.status_of(run, cls, i) for i in (1, 2)] == [fill, None]


@pytest.mark.parametrize("way", WAYS, ids=repr)
def test_rows_that_all_lack_the_key(run, way):
    _legacy(run.path, [{"name": "a"}, {"name": "b"}])
    cls = way.promote(run, {"status": "TEXT"}, default="unset")
    fill = "unset" if cls is not None else None
    assert _first_two(run.path)[1] == [(1, '{"name":"a"}', fill), (2, '{"name":"b"}', fill)]


# ---------------------------------------------------------------- 3. a promotion that cannot keep a value


REFUSED = [
    # (id, documents, definition, rows refused, first _id refused)
    ("null in NOT NULL", [{"status": "x"}, {"status": None}, {"status": None}], "TEXT NOT NULL DEFAULT 'x'", 2, 2),
    ("list", [{"status": "ok"}, {"status": "ok"}, {"status": ["l"]}], "TEXT", 1, 3),
    ("dict", [{"status": "ok"}, {"status": {"k": 1}}, {"status": "ok"}], "TEXT", 1, 2),
    ("reference", [{"status": {"_table": "users", "_id": 1}}, {"status": "ok"}], "TEXT", 1, 1),
    ("20-digit integer", [{"status": 1}, {"status": 12345678901234567890}], "INTEGER", 1, 2),
    ("integer past 64 bits", [{"status": 2**63}], "INTEGER", 1, 1),
]


@pytest.mark.parametrize("way", WAYS, ids=repr)
@pytest.mark.parametrize(("docs", "definition", "rows", "first"), [r[1:] for r in REFUSED], ids=[r[0] for r in REFUSED])
def test_a_value_the_column_cannot_keep_refuses(run, way, docs, definition, rows, first):
    _legacy(run.path, docs)
    before = _raw(run.path)
    with pytest.raises(SchemaError) as err:
        way.promote(run, {"status": definition})
    assert err.value.details == {"table": TABLE, "column": "status", "rows": rows, "first_id": first}
    assert _raw(run.path) == before
    assert way.remembered(run) is None
    assert _records(run.path) == []


@pytest.mark.parametrize("way", MODEL_WAYS, ids=repr)
def test_no_key_and_a_default_the_not_null_column_cannot_hold_refuses(run, way):
    """Row 2 has no key; the model's default is None: the NOT NULL column cannot hold what it reads."""
    _legacy(run.path, [{"name": "a", "status": "x"}, {"name": "b"}])
    before = _raw(run.path)
    with pytest.raises(SchemaError) as err:
        way.promote(run, {"status": "TEXT NOT NULL DEFAULT 'x'"}, default=None)
    assert err.value.details == {"table": TABLE, "column": "status", "rows": 1, "first_id": 2}
    assert _raw(run.path) == before


@pytest.mark.parametrize("way", MODEL_WAYS, ids=repr)
def test_no_key_and_a_list_default_refuses(run, way):
    """A field whose default is a list: a row without the key reads it, the column cannot hold it."""
    _legacy(run.path, [{"name": "a"}])
    before = _raw(run.path)
    with pytest.raises(SchemaError) as err:
        way.promote(run, {"status": "TEXT"}, default=[])
    assert err.value.details == {"table": TABLE, "column": "status", "rows": 1, "first_id": 1}
    assert _raw(run.path) == before


def test_a_key_inside_a_nested_object_is_not_touched(run):
    _legacy(run.path, [{"meta": {"status": "deep"}}, {"status": "top", "meta": {"status": "deep"}}])
    run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    assert _raw(run.path)[1] == [
        (1, '{"meta":{"status":"deep"}}', None),
        (2, '{"meta":{"status":"deep"}}', "top"),
    ]


def test_a_table_with_no_rows(run):
    _legacy(run.path, [])
    run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    assert _raw(run.path) == (["_id", "data", "status"], [])
    assert _records(run.path) == [(TABLE, "status")]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_two_thousand_rows_move_in_one_statement(run, is_async):
    _legacy(run.path, [{"n": i, "status": f"s{i}"} for i in range(2000)])
    with query_logger.capture() as sent:
        if is_async:
            run.go(run.adb.ensure_table(TABLE, promoted={"status": "TEXT"}))
        else:
            run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    assert [q.sql.split()[0] for q in sent if q.sql.lstrip().upper().startswith("UPDATE")] == ["UPDATE"]
    cols, rows = _raw(run.path)
    assert len(rows) == 2000
    assert rows[0] == (1, '{"n":0}', "s0")
    assert rows[1999] == (2000, '{"n":1999}', "s1999")


# ---------------------------------------------------------------- 4. a table an earlier version promoted


@pytest.mark.parametrize("way", WAYS, ids=repr)
def test_a_table_promoted_by_an_earlier_version_refuses_until_settled(run, way):
    """The column was added and the values left in the documents; row 2 was written by an
    ``update()`` since (the column is newer), row 3 was saved again (its key is gone)."""
    _legacy(
        run.path,
        [{"name": "a", "status": "open"}, {"name": "b", "status": "open"}, {"name": "c"}],
        columns={"status": "TEXT DEFAULT 'pending'"},
        values={2: {"status": "closed"}, 3: {"status": "done"}},
    )
    before = _raw(run.path)
    with pytest.raises(SchemaError) as err:
        way.promote(run, {"status": "TEXT DEFAULT 'pending'"})
    assert err.value.details == {"table": TABLE, "columns": {"status": {"rows": 2, "first_id": 1}}}
    assert "move_promoted('tickets', 'status', keep='document')" in str(err.value)
    assert _raw(run.path) == before
    assert _records(run.path) == []


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("keep", "column"),
    [("document", ["open", "open", "done"]), ("column", ["pending", "closed", "done"])],
)
def test_move_promoted_settles_the_table(run, is_async, keep, column):
    _legacy(
        run.path,
        [{"name": "a", "status": "open"}, {"name": "b", "status": "open"}, {"name": "c"}],
        columns={"status": "TEXT DEFAULT 'pending'"},
        values={2: {"status": "closed"}, 3: {"status": "done"}},
    )
    if is_async:
        run.go(run.adb.move_promoted(TABLE, "status", keep=keep))
        run.go(run.adb.ensure_table(TABLE, promoted={"status": "TEXT DEFAULT 'pending'"}))
    else:
        run.db.move_promoted(TABLE, "status", keep=keep)
        run.db.ensure_table(TABLE, promoted={"status": "TEXT DEFAULT 'pending'"})
    cols, rows = _raw(run.path)
    assert [r[1] for r in rows] == ['{"name":"a"}', '{"name":"b"}', '{"name":"c"}']
    assert [r[2] for r in rows] == column
    assert _records(run.path) == [(TABLE, "status")]


def test_a_settled_table_is_not_read_again(run):
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    assert _records(run.path) == [(TABLE, "status")]
    other = SQLerDB.on_disk(run.path)
    try:
        with query_logger.capture() as sent:
            other.ensure_table(TABLE, promoted={"status": "TEXT"})
    finally:
        other.close()
    assert [q.sql for q in sent if "json_type" in q.sql] == []


def test_a_read_only_database_reads_and_records_nothing(run, tmp_path):
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    ro = SQLerDB(SQLiteAdapter(f"file:{run.path}?mode=ro"))
    try:
        ro.ensure_table(TABLE, promoted={"status": "TEXT"})
        assert ro._promoted_of(TABLE) == ["status"]
    finally:
        ro.close()
    assert _records(run.path) == []


def test_a_read_only_database_with_values_in_documents_refuses(run):
    _legacy(run.path, [{"name": "a", "status": "open"}], columns={"status": "TEXT"})
    ro = SQLerDB(SQLiteAdapter(f"file:{run.path}?mode=ro"))
    try:
        with pytest.raises(SchemaError) as err:
            ro.ensure_table(TABLE, promoted={"status": "TEXT"})
    finally:
        ro.close()
    assert err.value.details == {"table": TABLE, "columns": {"status": {"rows": 1, "first_id": 1}}}


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_move_promoted_refuses_what_it_cannot_do(run, is_async):
    _legacy(run.path, [{"name": "a", "status": ["l"]}], columns={"status": "TEXT"})
    db = run.adb if is_async else run.db

    def call(*args, **kwargs):
        out = db.move_promoted(*args, **kwargs)
        return run.go(out) if is_async else out

    with pytest.raises(ValueError, match="keep must be 'document' or 'column', not 'both'"):
        call(TABLE, "status", keep="both")
    with pytest.raises(TableNotFoundError) as missing:
        call("nothere", "status", keep="document")
    assert missing.value.table == "nothere"
    with pytest.raises(SchemaError) as not_promoted:
        call(TABLE, "name", keep="document")
    assert not_promoted.value.details == {"table": TABLE, "column": "name"}
    before = _raw(run.path)
    with pytest.raises(SchemaError) as refused:
        call(TABLE, "status", keep="document")
    assert refused.value.details == {"table": TABLE, "column": "status", "rows": 1, "first_id": 1}
    assert _raw(run.path) == before
    assert _records(run.path) == []


# ---------------------------------------------------------------- 5. two that promote at once


def test_two_database_objects_promote_at_once(run):
    _legacy(run.path, [{"n": i, "status": f"s{i}"} for i in range(200)])
    dbs = [SQLerDB.on_disk(run.path), SQLerDB.on_disk(run.path)]
    barrier = threading.Barrier(2)
    errors = []

    def work(db):
        barrier.wait()
        try:
            db.ensure_table(TABLE, promoted={"status": "TEXT"})
        except Exception as exc:  # collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(db,)) for db in dbs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for db in dbs:
        db.close()
    assert errors == []
    cols, rows = _raw(run.path)
    assert cols == ["_id", "data", "status"]
    assert rows == [(i + 1, f'{{"n":{i}}}', f"s{i}") for i in range(200)]


def test_two_threads_on_one_database_promote_at_once(run):
    _legacy(run.path, [{"n": i, "status": f"s{i}"} for i in range(200)])
    db = run.db
    barrier = threading.Barrier(2)
    errors = []

    def work():
        barrier.wait()
        try:
            db.ensure_table(TABLE, promoted={"status": "TEXT"})
        except Exception as exc:  # collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert _raw(run.path) == (["_id", "data", "status"], [(i + 1, f'{{"n":{i}}}', f"s{i}") for i in range(200)])


def test_two_tasks_promote_at_once(run):
    _legacy(run.path, [{"n": i, "status": f"s{i}"} for i in range(200)])
    db = run.adb

    async def both():
        return await asyncio.gather(
            db.ensure_table(TABLE, promoted={"status": "TEXT"}),
            db.ensure_table(TABLE, promoted={"status": "TEXT"}),
            return_exceptions=True,
        )

    assert run.go(both()) == [None, None]
    assert _raw(run.path) == (["_id", "data", "status"], [(i + 1, f'{{"n":{i}}}', f"s{i}") for i in range(200)])


def test_another_connection_holds_the_write_lock(run):
    _legacy(run.path, [{"name": "a", "status": "open"}])
    before = _raw(run.path)
    db = SQLerDB.on_disk(run.path, timeout_ms=50)
    holder = sqlite3.connect(run.path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            db.ensure_table(TABLE, promoted={"status": "TEXT"})
        holder.rollback()
        assert _raw(run.path) == before
        assert db._promoted_of(TABLE) is None
        db.ensure_table(TABLE, promoted={"status": "TEXT"})
    finally:
        holder.close()
        db.close()
    assert _raw(run.path)[1] == [(1, '{"name":"a"}', "open")]


# ---------------------------------------------------------------- 6. inside the caller's open transaction


@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
def test_a_promotion_inside_a_transaction_belongs_to_it(run, commit):
    _legacy(run.path, [{"name": "a", "status": "open"}])
    before = _raw(run.path)
    db = run.db
    with pytest.raises(RuntimeError) if not commit else _nothing():
        with db.transaction():
            db.ensure_table(TABLE, promoted={"status": "TEXT"})
            if not commit:
                raise RuntimeError("roll back")
    if commit:
        assert _raw(run.path)[1] == [(1, '{"name":"a"}', "open")]
        assert db._promoted_of(TABLE) == ["status"]
        assert _records(run.path) == [(TABLE, "status")]
    else:
        assert _raw(run.path) == before
        assert db._promoted_of(TABLE) is None
        assert _records(run.path) == []


@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
def test_an_async_promotion_inside_a_transaction_belongs_to_it(run, commit):
    _legacy(run.path, [{"name": "a", "status": "open"}])
    before = _raw(run.path)
    db = run.adb

    async def body():
        async with db.transaction():
            await db.ensure_table(TABLE, promoted={"status": "TEXT"})
            if not commit:
                raise RuntimeError("roll back")

    if commit:
        run.go(body())
        assert _raw(run.path)[1] == [(1, '{"name":"a"}', "open")]
        assert db._promoted_of(TABLE) == ["status"]
    else:
        with pytest.raises(RuntimeError):
            run.go(body())
        assert _raw(run.path) == before
        assert db._promoted_of(TABLE) is None
        assert _records(run.path) == []


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------- 7. a remembered table costs what it did


def _statements(fn):
    with query_logger.capture() as sent:
        fn()
    return [q.sql.split()[0].upper() for q in sent]


def test_a_remembered_table_costs_no_added_statement(run):
    Ticket = _pydantic(SQLerModel)({"status": "TEXT"}, "unset")
    Ticket.set_db(run.db)
    first = Ticket(name="a", status="open").save()
    assert _statements(lambda: Ticket(name="b", status="new").save()) == ["INSERT"]
    first.name = "a2"
    assert _statements(first.save) == ["UPDATE"]
    assert _statements(lambda: Ticket.from_id(1)) == ["SELECT"]


def test_an_async_remembered_table_costs_no_added_statement(run):
    Ticket = _pydantic(AsyncSQLerModel)({"status": "TEXT"}, "unset")
    Ticket.set_db(run.adb)
    run.go(Ticket(name="a", status="open").save())
    assert _statements(lambda: run.go(Ticket(name="b", status="new").save())) == ["INSERT"]
    assert _statements(lambda: run.go(Ticket.from_id(1))) == ["SELECT"]


# ---------------------------------------------------------------- 8. the refusal SQLite makes


@pytest.mark.parametrize("way", WAYS, ids=repr)
@pytest.mark.parametrize("docs", [[{"name": "a", "status": "open"}], []], ids=["rows", "empty"])
def test_not_null_without_default_on_an_existing_table_refuses(run, way, docs):
    _legacy(run.path, docs)
    before = _raw(run.path)
    with pytest.raises(SchemaError, match=r"tickets\.status: SQLite cannot add a NOT NULL column with no DEFAULT"):
        way.promote(run, {"status": "TEXT NOT NULL"})
    assert _raw(run.path) == before
    assert way.remembered(run) is None


def test_created_with_its_columns_records_the_table(run):
    run.db.ensure_table("fresh", promoted={"status": "TEXT", "n": "INTEGER"})
    assert _records(run.path) == [("fresh", "n"), ("fresh", "status")]



# ---------------------------------------------------------------- a read checks the table as an ensure does


def _unsettled(path):
    """``tickets`` as an earlier version left it: column ``status`` added, row 1's value still in
    its document (``"open"``), row 2 without the key; nothing recorded."""
    _legacy(path, [{"name": "a", "status": "open"}, {"name": "b"}], columns={"status": "TEXT"})


UNSETTLED = {"table": TABLE, "columns": {"status": {"rows": 1, "first_id": 1}}}


def _ticket(base=SQLerModel):
    return _pydantic(base)({"status": "TEXT"}, None)


SYNC_READS = {
    "find_document": lambda run, cls: run.db.find_document(TABLE, 1),
    "find_documents": lambda run, cls: run.db.find_documents(TABLE, [1, 2]),
    "from_ids": lambda run, cls: cls.from_ids([1], db=run.db),
    "all": lambda run, cls: cls.using(run.db).all(),
    "first": lambda run, cls: cls.using(run.db).first(),
    "iter": lambda run, cls: list(cls.using(run.db).iter()),
    "count": lambda run, cls: cls.using(run.db).filter(F("status") == "open").count(),
    "from_id": lambda run, cls: _bound(run, cls).from_id(1),
    "as_dicts": lambda run, cls: cls.using(run.db).as_dicts(),
    "exists": lambda run, cls: cls.using(run.db).filter(F("status") == "open").exists(),
    "sum": lambda run, cls: cls.using(run.db).sum("status"),
    "avg": lambda run, cls: cls.using(run.db).avg("status"),
    "min": lambda run, cls: cls.using(run.db).min("status"),
    "max": lambda run, cls: cls.using(run.db).max("status"),
    "distinct_values": lambda run, cls: cls.using(run.db).distinct_values("status"),
    "paginate": lambda run, cls: cls.using(run.db).paginate(1, 10),
    "update_one": lambda run, cls: cls.using(run.db).filter(F("name") == "a").update_one(status="y"),
    "update_n": lambda run, cls: cls.using(run.db).filter(F("name") == "a").update_n(1, status="y"),
}


def _bound(run, cls):
    with pytest.raises(SchemaError):
        cls.set_db(run.db)  # the sync bases ensure the table when they are bound: refused too
    cls._db = run.db
    return cls


SYNC_WRITES = {
    "save": lambda run, cls: cls(name="new", status="x").save(db=run.db),
    "update": lambda run, cls: cls.using(run.db).filter(F("name") == "a").update(status="y"),
    "save_many": lambda run, cls: cls.save_many([cls(name="m", status="m")], db=run.db),
    "delete": lambda run, cls: cls.using(run.db).filter(F("status") == "open").delete_all(),
}


def _async_from_id(run, cls):
    cls.set_db(run.adb)  # the async bases bind with no ensure
    return run.go(cls.from_id(1))


ASYNC_READS = {
    "find_document": lambda run, cls: run.go(run.adb.find_document(TABLE, 1)),
    "find_documents": lambda run, cls: run.go(run.adb.find_documents(TABLE, [1, 2])),
    "from_id": _async_from_id,
    "from_ids": lambda run, cls: run.go(cls.from_ids([1], db=run.adb)),
    "all": lambda run, cls: run.go(cls.using(run.adb).all()),
    "first": lambda run, cls: run.go(cls.using(run.adb).first()),
    "count": lambda run, cls: run.go(cls.using(run.adb).filter(F("status") == "open").count()),
    "exists": lambda run, cls: run.go(cls.using(run.adb).filter(F("status") == "open").exists()),
    "sum": lambda run, cls: run.go(cls.using(run.adb).sum("status")),
    "avg": lambda run, cls: run.go(cls.using(run.adb).avg("status")),
    "min": lambda run, cls: run.go(cls.using(run.adb).min("status")),
    "max": lambda run, cls: run.go(cls.using(run.adb).max("status")),
    "distinct_values": lambda run, cls: run.go(cls.using(run.adb).distinct_values("status")),
    "paginate": lambda run, cls: run.go(cls.using(run.adb).paginate(1, 10)),
    "update_one": lambda run, cls: run.go(cls.using(run.adb).filter(F("name") == "a").update_one(status="y")),
    "update_n": lambda run, cls: run.go(cls.using(run.adb).filter(F("name") == "a").update_n(1, status="y")),
}

ASYNC_WRITES = {
    "save": lambda run, cls: run.go(cls(name="new", status="x").save(db=run.adb)),
    "update": lambda run, cls: run.go(cls.using(run.adb).filter(F("name") == "a").update(status="y")),
    "save_many": lambda run, cls: run.go(cls.asave_many([cls(name="m", status="m")], db=run.adb)),
    "delete": lambda run, cls: run.go(cls.using(run.adb).filter(F("status") == "open").delete_all()),
}


@pytest.mark.parametrize("read", list(SYNC_READS), ids=str)
def test_a_read_of_an_unsettled_table_refuses_and_so_does_every_write_after_it(run, read):
    _unsettled(run.path)
    before = _raw(run.path)
    cls = _ticket()
    with pytest.raises(SchemaError) as err:
        SYNC_READS[read](run, cls)
    assert err.value.details == UNSETTLED
    for name, write in SYNC_WRITES.items():
        with pytest.raises(SchemaError) as refused:
            write(run, cls)
        assert (name, refused.value.details) == (name, UNSETTLED)
    assert _raw(run.path) == before
    assert _records(run.path) == []
    assert run.db._promoted_of(TABLE) is None


@pytest.mark.parametrize("read", list(ASYNC_READS), ids=str)
def test_an_async_read_of_an_unsettled_table_refuses_and_so_does_every_write_after_it(run, read):
    _unsettled(run.path)
    before = _raw(run.path)
    cls = _ticket(AsyncSQLerModel)
    with pytest.raises(SchemaError) as err:
        ASYNC_READS[read](run, cls)
    assert err.value.details == UNSETTLED
    for name, write in ASYNC_WRITES.items():
        with pytest.raises(SchemaError) as refused:
            write(run, cls)
        assert (name, refused.value.details) == (name, UNSETTLED)
    assert _raw(run.path) == before
    assert _records(run.path) == []
    assert run.adb._promoted_of(TABLE) is None


def test_a_read_then_a_save_of_the_row_read_loses_nothing(run):
    """The sequence that lost ``"open"``: read row 1 through ``find_document``, then save it."""
    _unsettled(run.path)
    before = _raw(run.path)
    cls = _ticket()
    with pytest.raises(SchemaError):
        run.db.find_document(TABLE, 1)
    row = cls(name="a", status=None)
    row._id = 1
    with pytest.raises(SchemaError):
        row.save(db=run.db)
    assert _raw(run.path) == before


def test_a_read_only_database_on_an_unsettled_table_refuses_the_read(run):
    _unsettled(run.path)
    before = _raw(run.path)
    ro = SQLerDB(SQLiteAdapter(f"file:{run.path}?mode=ro"))
    try:
        with pytest.raises(SchemaError) as err:
            ro.find_document(TABLE, 1)
    finally:
        ro.close()
    assert err.value.details == UNSETTLED
    assert _raw(run.path) == before
    assert _records(run.path) == []


def test_an_object_that_read_before_another_settled_the_table(run):
    """Object one reads (refused); object two settles with ``move_promoted``; object one then
    reads the moved value and writes, with the check run again, not remembered from before."""
    _unsettled(run.path)
    cls = _ticket()
    with pytest.raises(SchemaError):
        run.db.find_document(TABLE, 1)
    other = SQLerDB.on_disk(run.path)
    try:
        other.move_promoted(TABLE, "status", keep="document")
    finally:
        other.close()
    assert run.db.find_document(TABLE, 1) == {"name": "a", "_id": 1, "status": "open"}
    cls(name="c", status="new").save(db=run.db)
    assert _raw(run.path)[1] == [(1, '{"name":"a"}', "open"), (2, '{"name":"b"}', None), (3, '{"name":"c"}', "new")]


# what each statement of a read is: the columns, the record, the tally, the rows
def _kinds(sent):
    out = []
    for q in sent:
        sql = q.sql.lstrip()
        if sql.upper().startswith("PRAGMA TABLE_INFO"):
            out.append("columns")
        elif "_sqler_promoted" in sql:
            out.append("record")
        elif "json_type" in sql:
            out.append("tally")
        else:
            out.append(sql.split()[0].upper())
    return out


def _read_kinds(fn):
    with query_logger.capture() as sent:
        fn()
    return _kinds(sent)


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_read_of_a_table_without_promoted_columns_costs_what_it_did(run, is_async):
    _legacy(run.path, [{"name": "a"}])
    read = (lambda: run.go(run.adb.find_document(TABLE, 1))) if is_async else (lambda: run.db.find_document(TABLE, 1))
    assert _read_kinds(read) == ["columns", "SELECT"]
    assert _read_kinds(read) == ["SELECT"]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_read_of_a_recorded_table_reads_the_record_once(run, is_async):
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    SQLerDB.on_disk(run.path).ensure_table(TABLE, promoted={"status": "TEXT"})
    assert _records(run.path) == [(TABLE, "status")]
    read = (lambda: run.go(run.adb.find_document(TABLE, 1))) if is_async else (lambda: run.db.find_document(TABLE, 1))
    assert _read_kinds(read) == ["columns", "record", "SELECT"]
    assert _read_kinds(read) == ["SELECT"]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_read_of_an_unrecorded_complete_table_tallies_once_and_records_nothing(run, is_async):
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    db = run.adb if is_async else run.db
    read = (lambda: run.go(db.find_document(TABLE, 1))) if is_async else (lambda: db.find_document(TABLE, 1))
    assert _read_kinds(read) == ["columns", "record", "tally", "SELECT"]
    assert _read_kinds(read) == ["SELECT"]
    assert _records(run.path) == []
    assert db._promoted_of(TABLE) == ["status"]


def test_a_table_settled_by_a_read_costs_no_added_statement_on_writes(run):
    """The twin of the cost pin above that starts from a read, not from ``set_db``."""
    Ticket = _ticket()
    Ticket.set_db(run.db)
    Ticket(name="a", status="open").save()
    other = SQLerDB.on_disk(run.path)
    try:
        assert other.find_document(TABLE, 1) == {"name": "a", "_id": 1, "status": "open"}
        assert _statements(lambda: Ticket(name="b", status="new").save(db=other)) == ["INSERT"]
        (row,) = Ticket.from_ids([1], db=other)
        row.name = "a2"
        assert _statements(lambda: row.save(db=other)) == ["UPDATE"]
        assert _statements(lambda: other.find_document(TABLE, 1)) == ["SELECT"]
    finally:
        other.close()


def test_an_async_table_settled_by_a_read_costs_no_added_statement_on_writes(run):
    Ticket = _ticket(AsyncSQLerModel)
    run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    run.db.insert_document(TABLE, {"name": "a"})
    assert run.go(run.adb.find_document(TABLE, 1)) == {"name": "a", "_id": 1, "status": None}
    assert _statements(lambda: run.go(Ticket(name="b", status="new").save(db=run.adb))) == ["INSERT"]
    assert _statements(lambda: run.go(run.adb.find_document(TABLE, 1))) == ["SELECT"]


# ---------------------------------------------------------------- a complete table needs no write lock


def test_a_complete_unrecorded_table_is_used_while_another_connection_writes(run):
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    db = SQLerDB.on_disk(run.path, timeout_ms=50)
    holder = sqlite3.connect(run.path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        db.ensure_table(TABLE, promoted={"status": "TEXT"})
        assert db._promoted_of(TABLE) == ["status"]
        assert db.find_document(TABLE, 1) == {"name": "a", "_id": 1, "status": None}
        holder.rollback()
        assert _records(run.path) == []
        db.ensure_table(TABLE, promoted={"status": "TEXT"})  # the next ensure that can writes it
        assert _records(run.path) == [(TABLE, "status")]
    finally:
        holder.close()
        db.close()


def test_an_async_complete_unrecorded_table_is_used_while_another_connection_writes(run):
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    holder = sqlite3.connect(run.path)

    async def scene():
        db = AsyncSQLerDB.on_disk(run.path, timeout_ms=50)
        await db.connect()
        try:
            holder.execute("BEGIN IMMEDIATE")
            await db.ensure_table(TABLE, promoted={"status": "TEXT"})
            held = (db._promoted_of(TABLE), set(db._unrecorded), await db.find_document(TABLE, 1), _records(run.path))
            holder.rollback()
            await db.ensure_table(TABLE, promoted={"status": "TEXT"})  # the next ensure that can writes it
            return held, set(db._unrecorded), _records(run.path)
        finally:
            await db.close()

    try:
        held, unrecorded, after = run.go(scene())
    finally:
        holder.close()
    assert held == (["status"], {TABLE}, {"name": "a", "_id": 1, "status": None}, [])
    assert (unrecorded, after) == (set(), [(TABLE, "status")])


def test_a_read_only_database_on_a_complete_table_records_nothing_and_says_so(run):
    """The ensure tries to record, the database refuses the write (the first statement of the
    record, its ``CREATE``, is sent and refused), and the table is remembered as settled with
    its record still to write."""
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    ro = SQLerDB(SQLiteAdapter(f"file:{run.path}?mode=ro"))
    try:
        with query_logger.capture() as sent:
            ro.ensure_table(TABLE, promoted={"status": "TEXT"})
        assert _kinds(sent) == ["columns", "record", "tally", "record"]
        assert sent[-1].sql.startswith("CREATE TABLE IF NOT EXISTS _sqler_promoted")
        assert ro._promoted_of(TABLE) == ["status"]
        assert ro._unrecorded == {TABLE}
    finally:
        ro.close()
    assert _records(run.path) == []


# ---------------------------------------------------------------- the record belongs to its table


@pytest.mark.parametrize("name", ["_sqler_promoted", "_SQLER_Promoted", "_sqler_migrations"])
def test_a_table_of_sqlers_own_is_not_a_user_table_name(name):
    from sqler.exceptions import InvalidTableNameError
    from sqler.utils import validate_table_name

    with pytest.raises(InvalidTableNameError, match="sqler keeps for itself"):
        validate_table_name(name)


def test_sqlers_own_tables_are_one_set():
    from sqler.db.sqler_db import _SETTLED
    from sqler.migrations import MIGRATIONS_TABLE
    from sqler.utils import SQLER_TABLES

    assert SQLER_TABLES == {MIGRATIONS_TABLE, _SETTLED} == {"_sqler_migrations", "_sqler_promoted"}


def _foreign_record_table(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE _sqler_promoted (a TEXT, b TEXT)")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "touch",
    [
        "new table",
        "added column",
        "read",
        "ensure of a promoted table",
    ],
)
def test_a_table_named_like_the_record_that_is_not_it_refuses(run, touch):
    _legacy(run.path, [{"name": "a", "status": "open"}], columns={"note": "TEXT"} if touch != "added column" else None)
    _foreign_record_table(run.path)
    before = _raw(run.path)
    with pytest.raises(SchemaError, match="_sqler_promoted is not") as err:
        if touch == "new table":
            run.db.ensure_table("fresh", promoted={"status": "TEXT"})
        elif touch == "added column":
            run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
        elif touch == "read":
            run.db.find_document(TABLE, 1)
        else:
            run.db.ensure_table(TABLE, promoted={"note": "TEXT"})
    assert err.value.details == {"table": "_sqler_promoted"}
    assert _raw(run.path) == before
    conn = sqlite3.connect(run.path)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'fresh'").fetchall() == []
    finally:
        conn.close()


def test_a_table_made_again_by_other_means_keeps_its_record(run):
    """sqler drops and renames no table, so nothing of sqler's moves a record. A table dropped
    and made again by plain SQL under a recorded name keeps that record: it counts as settled,
    and a value made again in a document is not read (the column wins). Deleting its rows from
    ``_sqler_promoted`` has it checked again."""
    _legacy(run.path, [{"name": "a"}], columns={"status": "TEXT"})
    run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    run.db.close()
    run._db = None
    conn = sqlite3.connect(run.path)
    conn.execute(f"DROP TABLE {TABLE}")
    conn.commit()
    conn.close()
    _legacy(run.path, [{"name": "b", "status": "open"}], columns={"status": "TEXT"})
    assert _records(run.path) == [(TABLE, "status")]
    assert run.db.find_document(TABLE, 1) == {"name": "b", "_id": 1, "status": None}
    conn = sqlite3.connect(run.path)
    conn.execute("DELETE FROM _sqler_promoted WHERE tbl = ?", [TABLE])
    conn.commit()
    conn.close()
    fresh = SQLerDB.on_disk(run.path)
    try:
        with pytest.raises(SchemaError) as err:
            fresh.find_document(TABLE, 1)
    finally:
        fresh.close()
    assert err.value.details == UNSETTLED


# ---------------------------------------------------------------- a search index made before the promotion


def test_a_search_index_finds_rows_whose_field_was_promoted(run):
    from sqler.fts import FTSIndex

    _legacy(run.path, [{"name": "a", "status": "open"}, {"name": "b", "status": "done"}])
    Before = _pydantic(SQLerModel)(None, None)
    Before.set_db(run.db)
    FTSIndex(Before, ["status"], db=run.db, index_name="tickets_fts").create()

    def found(word):
        conn = sqlite3.connect(run.path)
        try:
            return [r[0] for r in conn.execute("SELECT rowid FROM tickets_fts WHERE tickets_fts MATCH ? ORDER BY rowid", [word])]
        finally:
            conn.close()

    assert (found("open"), found("done")) == ([1], [2])
    After = _ticket()
    After.set_db(run.db)  # the promotion moves the values out of the documents
    assert (found("open"), found("done")) == ([1], [2])
    after = FTSIndex(After, ["status"], db=run.db, index_name="tickets_fts")
    assert after._make(run.db, write=False) is False  # the triggers are the ones create() writes now
    row = After.from_ids([2], db=run.db)[0]
    row.status = "open"
    row.save()
    assert (found("open"), found("done")) == ([1, 2], [])


SEARCHES = {
    "search": lambda index: index.search("alpha"),
    "search_ranked": lambda index: [hit.model for hit in index.search_ranked("alpha")],
    "search_with_highlights": lambda index: [hit.model for hit in index.search_with_highlights("alpha")],
}


@pytest.mark.parametrize("search", list(SEARCHES), ids=str)
def test_a_search_hit_holds_what_its_row_holds_in_its_columns(run, search):
    """A hit is the model ``from_ids`` gives for its row: the promoted field from its column."""
    from sqler.fts import FTSIndex

    Ticket = _ticket()
    Ticket.set_db(run.db)
    for name, status in [("alpha", "open"), ("beta", "done"), ("alpha two", "held")]:
        Ticket(name=name, status=status).save()
    assert _raw(run.path)[1] == [(1, '{"name":"alpha"}', "open"), (2, '{"name":"beta"}', "done"), (3, '{"name":"alpha two"}', "held")]
    index = FTSIndex(Ticket, ["name"], db=run.db, index_name="tickets_fts")
    index.create()
    hits = SEARCHES[search](index)
    assert sorted((m._id, m.name, m.status) for m in hits) == [(1, "alpha", "open"), (3, "alpha two", "held")]
    assert [m.model_dump() for m in sorted(hits, key=lambda m: m._id)] == [
        m.model_dump() for m in Ticket.from_ids([1, 3], db=run.db)
    ]


@pytest.mark.parametrize("search", list(SEARCHES), ids=str)
def test_a_search_of_an_unsettled_table_refuses(run, search):
    from sqler.fts import FTSIndex

    _legacy(run.path, [{"name": "alpha", "status": "open"}, {"name": "beta"}])
    Before = _pydantic(SQLerModel)(None, None)
    Before.set_db(run.db)
    FTSIndex(Before, ["name"], db=run.db, index_name="tickets_fts").create()  # the index is there already
    run.db.close()
    run._db = None
    conn = sqlite3.connect(run.path)
    conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN status TEXT")  # as an earlier version added it
    conn.commit()
    conn.close()
    before = _raw(run.path)
    cls = _ticket()
    cls._db = run.db
    index = FTSIndex(cls, ["name"], db=run.db, index_name="tickets_fts")
    with pytest.raises(SchemaError) as err:
        SEARCHES[search](index)
    assert err.value.details == UNSETTLED
    with pytest.raises(SchemaError) as refused:
        index.create()
    assert refused.value.details == UNSETTLED
    assert _raw(run.path) == before


def test_a_ranked_hit_is_ordered_by_its_score(run):
    from sqler.fts import FTSIndex

    Ticket = _ticket()
    Ticket.set_db(run.db)
    for name in ["alpha beta gamma delta", "alpha", "beta"]:
        Ticket(name=name, status="s").save()
    index = FTSIndex(Ticket, ["name"], db=run.db, index_name="tickets_fts")
    index.create()
    hits = index.search_ranked("alpha")
    assert [(h.model._id, h.model.status) for h in hits] == [(2, "s"), (1, "s")]  # the shorter row ranks first
    assert hits[0].score < hits[1].score < 0
    conn = sqlite3.connect(run.path)
    conn.execute("DROP TRIGGER IF EXISTS tickets_fts_ad")
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")]
    for name in names:
        conn.execute(f'DROP TRIGGER "{name}"')
    conn.execute(f"DELETE FROM {TABLE} WHERE _id = 2")  # the index still names a row that is gone
    conn.commit()
    conn.close()
    assert [h.model._id for h in index.search_ranked("alpha")] == [1]


def test_a_default_worked_out_by_a_pydantic_without_the_flag_is_a_plain_default(run):
    """pydantic before 2.10 has no ``default_factory_takes_validated_data`` on a field: its
    factories take no argument, and the fill is what the factory gives."""
    from sqler.db import sqler_db

    class Field:
        default_factory = staticmethod(lambda: "made")

        def is_required(self):
            return False

        def get_default(self, *, call_default_factory):
            return self.default_factory() if call_default_factory else None

    class Model:
        model_fields = {"status": Field()}

    assert sqler_db._fill_of(Model, "status") == "made"


def test_an_error_without_an_sqlite_code_is_not_a_database_that_takes_no_write():
    from sqler.db import sqler_db

    assert sqler_db._unwritable(sqlite3.OperationalError("another thread's transaction holds the connection")) is False
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    assert sqler_db._unwritable(busy) is True


# ---------------------------------------------------------------- sqler's own tables are not the user's


def test_the_table_counts_leave_out_sqlers_own_tables(run):
    from sqler.migrations import Migration, MigrationRunner
    from sqler.ops import async_get_stats, get_stats

    run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    MigrationRunner(run.db, [Migration(1, "first", lambda d: None)]).migrate()
    assert [r[0] for r in sqlite3.connect(run.path).execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )] == ["_sqler_migrations", "_sqler_promoted", TABLE]
    assert get_stats(run.db).table_count == 1
    assert run.go(async_get_stats(run.adb)).table_count == 1


# ---------------------------------------------------------------- a column nobody promoted


def test_a_hand_added_column_is_read_over_the_document_and_refused_by_name(run):
    """A read sets every column beside ``data`` on the document, a column added by hand too, so
    a key of its name left in a document is hidden: the check refuses it, and says so without
    calling it a column the class promoted."""
    _legacy(run.path, [{"name": "a", "note": "kept"}], columns={"note": "TEXT"})
    conn = sqlite3.connect(run.path)
    conn.execute(f"UPDATE {TABLE} SET note = 'col'")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaError) as err:
        run.db.ensure_table(TABLE, promoted={"status": "TEXT"})
    assert err.value.details == {"table": TABLE, "columns": {"note": {"rows": 1, "first_id": 1}}}
    assert "promoted" not in str(err.value).split("db.move_promoted")[0]
    assert "columns beside data" in str(err.value)
    run.db.move_promoted(TABLE, "note", keep="column")
    assert run.db.find_document(TABLE, 1) == {"name": "a", "_id": 1, "note": "col"}


# ---------------------------------------------------------------- a default worked out from the row


def _from_the_row():
    from pydantic import Field

    class Ticket(SQLerModel):
        __tablename__ = TABLE
        __promoted__ = {"status": "TEXT"}
        name: str = ""
        status: Optional[str] = Field(default_factory=lambda data: data["name"] + "!")

    return Ticket


def test_a_default_from_the_row_refuses_when_a_row_lacks_the_key(run):
    _legacy(run.path, [{"name": "a", "status": "open"}, {"name": "b"}])
    before = _raw(run.path)
    with pytest.raises(SchemaError) as err:
        _from_the_row().set_db(run.db)
    assert err.value.details == {"table": TABLE, "column": "status", "rows": 1, "first_id": 2}
    assert _raw(run.path) == before


def test_a_default_from_the_row_is_not_needed_when_every_row_has_the_key(run):
    _legacy(run.path, [{"name": "a", "status": "open"}, {"name": "b", "status": None}])
    _from_the_row().set_db(run.db)
    assert _raw(run.path)[1] == [(1, '{"name":"a"}', "open"), (2, '{"name":"b"}', None)]


def test_a_table_sqler_makes_replaces_what_is_recorded_under_its_name(run):
    run.db.ensure_table("fresh", promoted={"old": "TEXT"})
    conn = sqlite3.connect(run.path)
    conn.execute("DROP TABLE fresh")
    conn.commit()
    conn.close()
    other = SQLerDB.on_disk(run.path)
    try:
        other.ensure_table("fresh", promoted={"status": "TEXT"})
    finally:
        other.close()
    assert _records(run.path) == [("fresh", "status")]
