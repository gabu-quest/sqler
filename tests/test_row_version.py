"""The version of a row is its table's ``_version`` column.

Every statement sqler sends that writes a row of a table with the column moves it by one,
whatever class or method sends it, so a save of an instance read before any write is refused;
no document carries a copy, and every read gives the column. A table without the column is
written as it always was. Sync and async, both safe bases.
"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

import pytest

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
    F,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerQuery,
    SQLerSafeModel,
)
from sqler.exceptions import StaleVersionError
from sqler.logging import query_logger
from sqler.models.mixins import AsyncSoftDeleteMixin, SoftDeleteMixin
from sqler.models.utils import RebaseConfig
from sqler.query.async_query import AsyncSQLerQuery

TABLE = "rv_items"


# ---- the classes: a safe class and a plain class of one table, and a referred class ----


class Target(SQLerModel):
    __tablename__ = "rv_targets"
    name: str = ""


class Safe(SQLerSafeModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[Target] = None


class Plain(SoftDeleteMixin, SQLerModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[Target] = None


@dataclass
class LiteTarget(SQLerLiteModel):
    __tablename__ = "rv_targets"
    name: str = ""


@dataclass
class LiteSafe(SQLerLiteSafeModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[LiteTarget] = None


@dataclass
class LitePlain(SQLerLiteModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[LiteTarget] = None


class ATarget(AsyncSQLerModel):
    __tablename__ = "rv_targets"
    name: str = ""


class ASafe(AsyncSQLerSafeModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[ATarget] = None


class APlain(AsyncSoftDeleteMixin, AsyncSQLerModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[ATarget] = None


@dataclass
class ALiteTarget(AsyncSQLerLiteModel):
    __tablename__ = "rv_targets"
    name: str = ""


@dataclass
class ALiteSafe(AsyncSQLerLiteSafeModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[ALiteTarget] = None


@dataclass
class ALitePlain(AsyncSQLerLiteModel):
    __tablename__ = TABLE
    name: str = ""
    qty: int = 0
    owner: Optional[ALiteTarget] = None


CLASSES = {
    ("sync", "pydantic_safe"): (Safe, Plain, Target),
    ("sync", "lite_safe"): (LiteSafe, LitePlain, LiteTarget),
    ("async", "pydantic_safe"): (ASafe, APlain, ATarget),
    ("async", "lite_safe"): (ALiteSafe, ALitePlain, ALiteTarget),
}


class World:
    """One way in: a database on a file (sync or async) and the classes of a base."""

    def __init__(self, mode, base, tmp_path):
        self.is_async = mode == "async"
        self.loop = asyncio.new_event_loop() if self.is_async else None
        self.path = str(tmp_path / "rv.db")
        self.db = (AsyncSQLerDB if self.is_async else SQLerDB).on_disk(self.path)
        if self.is_async:
            self.go(self.db.connect())
        self.Safe, self.Plain, self.Target = CLASSES[(mode, base)]

    def go(self, value):
        """``value``, awaited on the async side."""
        return self.loop.run_until_complete(value) if self.is_async and asyncio.iscoroutine(value) else value

    def call(self, fn, *args, **kwargs):
        return self.go(fn(*args, **kwargs))

    def raw(self, sql, params=()):
        """Rows read past sqler: through the adapter's own connection on either side."""
        if self.is_async:

            async def read():
                cur = await self.db.adapter.execute(sql, list(params))
                try:
                    return [tuple(r) for r in await cur.fetchall()]
                finally:
                    await cur.close()

            return self.go(read())
        return [tuple(r) for r in self.db.adapter.execute(sql, list(params)).fetchall()]

    def raw_write(self, sql, params=()):
        if self.is_async:

            async def write():
                cur = await self.db.adapter.execute(sql, list(params))
                await cur.close()
                await self.db.adapter.auto_commit()

            self.go(write())
        else:
            self.db.adapter.execute(sql, list(params))
            self.db.adapter.auto_commit()

    def row(self, _id, table=TABLE):
        """``(column version, document's $._version, document)`` of row ``_id``."""
        [(version, copy, data)] = self.raw(
            f"SELECT _version, json_extract(data, '$._version'), data FROM {table} WHERE _id = ?", [_id]
        )
        return version, copy, json.loads(data)

    def close(self):
        self.go(self.db.close())
        if self.loop:
            self.loop.close()


@pytest.fixture(params=["pydantic_safe", "lite_safe"])
def base(request):
    return request.param


@pytest.fixture(params=["sync", "async"])
def mode(request):
    return request.param


@pytest.fixture
def world(mode, base, tmp_path):
    w = World(mode, base, tmp_path)
    yield w
    w.close()


def _saved_twice(w):
    """A row of a safe class saved twice: column 1. Its target ``t`` for a reference."""
    target = w.call(w.Target(name="t").save, db=w.db)
    inst = w.call(w.Safe(name="a", qty=1, owner=target).save, db=w.db)
    inst.qty = 2
    w.call(inst.save, db=w.db)
    return inst._id, target


def _doc(w, _id):
    """The row as ``find_document`` gives it, ``_id`` and ``_version`` keys included."""
    return w.call(w.db.find_document, TABLE, _id)


def _partial(w, _id):
    inst = w.call(w.Plain.from_id, _id, db=w.db)
    inst.qty = 31
    return w.call(inst.save, db=w.db, _partial=True)


def _plain_save(w, _id):
    inst = w.call(w.Plain.from_id, _id, db=w.db)
    inst.qty = 30
    return w.call(inst.save, db=w.db)


def _bulk(w, _id):
    return w.call(w.db.bulk_upsert, TABLE, [{**_doc(w, _id), "qty": 18}])


def _with_version(w, _id):
    doc = _doc(w, _id)
    return w.call(w.db.upsert_with_version, TABLE, _id, {**doc, "qty": 19}, doc["_version"])


def _set_null(w, _id, target):
    return w.call(target.delete_with_policy, db=w.db, on_delete="set_null")


# writer: (what it does to row ``_id``, the stored document fields that prove it landed)
WRITERS = {
    "safe update()": (lambda w, i, t: w.call(w.Safe.using(w.db).filter(F("_id") == i).update, qty=10), {"qty": 10}),
    "safe update_one()": (lambda w, i, t: w.call(w.Safe.using(w.db).filter(F("_id") == i).update_one, qty=11), {"qty": 11}),
    "plain update()": (lambda w, i, t: w.call(w.Plain.using(w.db).filter(F("_id") == i).update, qty=12), {"qty": 12}),
    "plain update_one()": (lambda w, i, t: w.call(w.Plain.using(w.db).filter(F("_id") == i).update_one, qty=13), {"qty": 13}),
    "plain update_n()": (lambda w, i, t: w.call(w.Plain.using(w.db).filter(F("_id") == i).update_n, 1, qty=14), {"qty": 14}),
    "db.query(table).update()": (lambda w, i, t: w.call(w.go(w.db.query(TABLE)).filter(F("_id") == i).update, qty=15), {"qty": 15}),
    "upsert_document(doc as read)": (lambda w, i, t: w.call(w.db.upsert_document, TABLE, i, {**_doc(w, i), "qty": 16}), {"qty": 16}),
    "bulk_upsert": (lambda w, i, t: _bulk(w, i), {"qty": 18}),
    "upsert_with_version": (lambda w, i, t: _with_version(w, i), {"qty": 19}),
    "plain save()": (lambda w, i, t: _plain_save(w, i), {"qty": 30}),
    "plain partial save": (lambda w, i, t: _partial(w, i), {"qty": 31}),
    "set_null of a referrer": (lambda w, i, t: _set_null(w, i, t), {"owner": None}),
}


@pytest.mark.parametrize("writer", list(WRITERS))
def test_every_write_of_a_row_moves_its_version_once_and_a_save_read_before_it_is_refused(world, writer):
    w = world
    _id, target = _saved_twice(w)
    before = w.call(w.Safe.from_id, _id, db=w.db)
    write, landed = WRITERS[writer]

    write(w, _id, target)

    version, copy, doc = w.row(_id)
    assert (version, copy, {k: doc.get(k) for k in landed}) == (2, None, landed)
    before.name = "stale"
    with pytest.raises(StaleVersionError):
        w.call(before.save, db=w.db)
    after = w.call(w.Safe.from_id, _id, db=w.db)
    assert after._version == 2
    after.name = "fresh"
    w.call(after.save, db=w.db)
    version, copy, doc = w.row(_id)
    assert (version, copy, doc["name"], {k: doc.get(k) for k in landed}) == (3, None, "fresh", landed)


@pytest.mark.parametrize("insert", ["insert_document", "insert_many", "bulk_upsert", "upsert_with_version", "plain save_many"])
def test_a_new_row_of_a_versioned_table_is_at_zero_whoever_inserts_it(world, insert):
    w = world
    _saved_twice(w)  # the table has its column
    doc = {"name": "n", "qty": 5, "_version": 9}
    ways = {
        "insert_document": lambda: w.call(w.db.insert_document, TABLE, doc),
        "insert_many": lambda: w.call(w.db.insert_many, TABLE, [doc])[0],
        "bulk_upsert": lambda: w.call(w.db.bulk_upsert, TABLE, [dict(doc)])[0],
        "upsert_with_version": lambda: w.call(w.db.upsert_with_version, TABLE, None, doc, None)[0],
        "plain save_many": lambda: w.call(w.Plain.save_many, [w.Plain(name="n", qty=5)], db=w.db)[0]._id,
    }
    _id = ways[insert]()
    version, copy, stored = w.row(_id)
    assert (version, copy, stored["qty"]) == (0, None, 5)


def test_a_counter_change_the_class_allows_is_rebased_onto_a_plain_write(mode, tmp_path):
    w = World(mode, "pydantic_safe", tmp_path)
    try:
        Counter = _counter(w.is_async)
        c = w.call(Counter(hits=1).save, db=w.db)
        held = w.call(Counter.from_id, c._id, db=w.db)
        w.call(w.go(w.db.query("rv_counters")).filter(F("_id") == c._id).update, hits=10, note="plain")
        held.hits += 1
        w.call(held.save, db=w.db)
        version, copy, doc = w.row(c._id, "rv_counters")
        assert (version, copy, doc["hits"], doc["note"], held._version) == (2, None, 11, "plain", 2)
    finally:
        w.close()


def _counter(is_async):
    rebase = RebaseConfig(enabled=True, allowed_fields={"hits"}, max_delta=5)
    base = AsyncSQLerSafeModel if is_async else SQLerSafeModel

    class Counter(base):
        __tablename__ = "rv_counters"
        hits: int = 0
        note: str = ""
        _rebase_config = rebase

    return Counter


# ---- no document carries the version; the check is the column alone ----------------------------


def test_a_row_whose_document_says_99_is_saved_by_an_instance_at_its_column(world):
    w = world
    _id, _ = _saved_twice(w)
    w.raw_write(f"UPDATE {TABLE} SET data = json_set(data, '$._version', 99), _version = 3 WHERE _id = ?", [_id])
    inst = w.call(w.Safe.from_id, _id, db=w.db)
    assert inst._version == 3
    inst.name = "saved"
    w.call(inst.save, db=w.db)
    assert w.row(_id)[:2] == (4, None)  # the whole row written: the old key is gone


def test_a_write_of_some_fields_leaves_a_key_an_older_version_left(world):
    w = world
    _id, _ = _saved_twice(w)
    w.raw_write(f"UPDATE {TABLE} SET data = json_set(data, '$._version', 99), _version = 3 WHERE _id = ?", [_id])
    w.call(w.go(w.db.query(TABLE)).filter(F("_id") == _id).update, qty=7)
    inst = w.call(w.Safe.from_id, _id, db=w.db)
    inst.qty = 8
    w.call(inst.save, db=w.db, _partial=True)
    version, copy, doc = w.row(_id)
    assert (version, copy, doc["qty"], inst._version) == (5, 99, 8, 5)


# ---- a read gives the column ------------------------------------------------------------------


def _reads(w, _id):
    """Every road that gives row ``_id`` as a document or a model, by name: its ``_version``."""
    q = w.go(w.db.query(TABLE)).filter(F("_id") == _id)

    async def first_iter():
        return [d async for d in q.iter_dicts()][0]["_version"]

    def model(m):
        return m._version if hasattr(m, "_version") else None

    safe_q = w.Safe.using(w.db).filter(F("_id") == _id)
    inst = w.call(w.Safe.from_id, _id, db=w.db)
    return {
        "find_document": w.call(w.db.find_document, TABLE, _id)["_version"],
        "find_documents": w.call(w.db.find_documents, TABLE, [_id])[0]["_version"],
        "find_document_with_version": w.call(w.db.find_document_with_version, TABLE, _id)["_version"],
        "db.query all_dicts": w.call(q.all_dicts)[0]["_version"],
        "db.query first_dict": w.call(q.first_dict)["_version"],
        "db.query iter_dicts": w.go(first_iter()) if w.is_async else next(iter(q.iter_dicts()))["_version"],
        "db.query paginate": w.call(q.paginate, 1).items[0]["_version"],
        "plain as_dicts": w.call(w.Plain.using(w.db).filter(F("_id") == _id).as_dicts)[0]["_version"],
        "safe all": model(w.call(safe_q.all)[0]),
        "safe first": model(w.call(safe_q.first)),
        "safe from_id": model(inst),
        "safe from_ids": model(w.call(w.Safe.from_ids, [_id], db=w.db)[0]),
        "safe refresh": model(w.call(inst.refresh, db=w.db)),
        "rows an update returns": w.call(q.update_one, name="r")["_version"] - 1,
    }


def test_every_read_of_a_versioned_table_gives_the_column_not_the_document(world):
    w = world
    _id, _ = _saved_twice(w)
    w.raw_write(f"UPDATE {TABLE} SET data = json_set(data, '$._version', 99), _version = 3 WHERE _id = ?", [_id])
    got = _reads(w, _id)
    assert got == dict.fromkeys(got, 3)


def test_a_reference_loaded_through_a_typed_field_has_its_column_version(mode, tmp_path):
    w = World(mode, "pydantic_safe", tmp_path)
    try:
        Child, Holder = _holder(w.is_async)
        child = w.call(Child(name="c").save, db=w.db)
        w.raw_write("UPDATE rv_children SET data = json_set(data, '$._version', 99), _version = 3 WHERE _id = ?", [child._id])
        holder = w.call(Holder(child=child).save, db=w.db)
        loaded = w.call(Holder.from_id, holder._id, db=w.db)
        assert loaded.child._version == 3
    finally:
        w.close()


def _holder(is_async):
    safe, plain = (AsyncSQLerSafeModel, AsyncSQLerModel) if is_async else (SQLerSafeModel, SQLerModel)

    class Child(safe):
        __tablename__ = "rv_children"
        name: str = ""

    class Holder(plain):
        __tablename__ = "rv_holders"
        child: Optional[Child] = None

    return Child, Holder


def test_a_table_without_the_column_gives_its_documents_as_stored(mode, tmp_path):
    w = World(mode, "pydantic_safe", tmp_path)
    try:
        _id = w.call(w.db.insert_document, "rv_plain", {"name": "p", "_version": "mine"})
        assert w.call(w.db.find_document, "rv_plain", _id) == {"_id": _id, "name": "p", "_version": "mine"}
        assert w.call(w.go(w.db.query("rv_plain")).all_dicts) == [{"_id": _id, "name": "p", "_version": "mine"}]
        assert w.call(w.db.find_document_with_version, "rv_plain", _id)["_version"] == 0
        w.call(w.db.upsert_document, "rv_plain", _id, {"name": "q", "_version": "mine"})
        w.call(w.go(w.db.query("rv_plain")).update, name="r")
        assert w.raw("SELECT name FROM pragma_table_info('rv_plain')") == [("_id",), ("data",)]
        assert w.raw("SELECT data FROM rv_plain") == [('{"name":"r","_version":"mine"}',)]
    finally:
        w.close()


# ---- a statement of a table without the column is the statement it always was -------------------


def test_a_table_without_the_column_is_written_with_the_statements_it_always_was():
    db = SQLerDB.in_memory(shared=False)
    try:
        _id = db.insert_document("t", {"a": 1})
        with query_logger.capture() as sent:
            db.upsert_document("t", _id, {"a": 2})
            db._update_fields("t", _id, {"a": 3}, ["a"])
            db.bulk_upsert("t", [{"_id": _id, "a": 4}])
            db.insert_document("t", {"a": 5})
            db.query("t").filter(F("a") == 4).update(a=6)
    finally:
        db.close()
    guard = "CASE WHEN 1 THEN 1 ELSE (SELECT (`_id`, `data`) IN (SELECT * FROM t) FROM t) END"
    assert [q.sql for q in sent if "CREATE TABLE IF NOT EXISTS" not in q.sql] == [
        f"UPDATE t SET data = json(?) WHERE _id = ? AND {guard};",
        f"UPDATE t SET data = json_set(data, ?, json(?)) WHERE _id = ? AND {guard};",
        f"INSERT INTO t (_id, data) VALUES (?, json({guard.replace('THEN 1', 'THEN ?')})) ON CONFLICT(_id) DO UPDATE SET data = excluded.data;",
        f"INSERT INTO t (data) VALUES (json({guard.replace('THEN 1', 'THEN ?')}));",
        f"UPDATE t SET data = json_set(data, '$.a', ?) WHERE JSON_EXTRACT(data, '$.a') = ? AND {guard}",
    ]


# ---- a _version a caller puts in a document sets nothing ----------------------------------------


@pytest.mark.parametrize("given", ["7", 2**70], ids=["string", "2**70"])
@pytest.mark.parametrize("way", ["upsert_document", "bulk_upsert", "upsert_with_version", "insert_document"])
def test_a_version_in_a_document_written_to_a_versioned_table_is_not_stored(given, way):
    db = SQLerDB.in_memory(shared=False)
    try:
        db.ensure_table("t", versioned=True)
        _id = db.insert_document("t", {"name": "a"})
        doc = {"name": "x", "_version": given}
        ways = {
            "upsert_document": lambda: db.upsert_document("t", _id, doc),
            "bulk_upsert": lambda: db.bulk_upsert("t", [{**doc, "_id": _id}])[0],
            "upsert_with_version": lambda: db.upsert_with_version("t", _id, doc, 0)[0],
            "insert_document": lambda: db.insert_document("t", doc),
        }
        written = ways[way]()
        rows = db.adapter.execute("SELECT _id, data, _version FROM t ORDER BY _id").fetchall()
    finally:
        db.close()
    expect = [(1, '{"name":"x"}', 1)] if way != "insert_document" else [(1, '{"name":"a"}', 0), (2, '{"name":"x"}', 0)]
    assert (written, [tuple(r) for r in rows]) == (expect[-1][0], expect)


@pytest.mark.parametrize("query", ["db.query", "a query built from the adapter"])
def test_update_of_version_is_refused_on_a_versioned_table_for_a_plain_query(tmp_path, query):
    db = SQLerDB.on_disk(str(tmp_path / "t.db"))
    try:
        db.ensure_table("t", versioned=True)
        db.insert_document("t", {"name": "a"})
        q = db.query("t") if query == "db.query" else SQLerQuery("t", db.adapter)
        with pytest.raises(ValueError, match="_version cannot be set"):
            q.update(_version=5)
        assert [tuple(r) for r in db.adapter.execute("SELECT data, _version FROM t").fetchall()] == [('{"name":"a"}', 0)]
    finally:
        db.close()


def test_a_safe_class_refused_an_update_of_version_has_changed_nothing(mode, tmp_path):
    w = World(mode, "pydantic_safe", tmp_path)
    try:
        w.call(w.db.insert_document, TABLE, {"name": "a"})  # the table, made without the column
        for update, args in (("update", ()), ("update_one", ()), ("update_n", (1,))):
            with pytest.raises(ValueError, match="_version cannot be set"):
                w.call(getattr(w.Safe.using(w.db), update), *args, _version=5)
        assert w.raw(f"SELECT name FROM pragma_table_info('{TABLE}')") == [("_id",), ("data",)]
        assert w.raw(f"SELECT data FROM {TABLE}") == [('{"name":"a"}',)]
    finally:
        w.close()


def test_with_version_on_a_table_without_the_column_reads_and_writes_with_no_version(mode, tmp_path):
    w = World(mode, "pydantic_safe", tmp_path)
    try:
        _id = w.call(w.db.insert_document, "rv_plain", {"name": "p"})
        assert w.call(w.go(w.db.query("rv_plain")).with_version().all_dicts) == [{"_id": _id, "name": "p"}]
        assert w.call(w.go(w.db.query("rv_plain")).with_version().update, name="q") == 1
        assert w.raw("SELECT name FROM pragma_table_info('rv_plain')") == [("_id",), ("data",)]
        assert w.raw("SELECT data FROM rv_plain") == [('{"name":"q"}',)]
    finally:
        w.close()


# ---- a database written before this change ----------------------------------------------------


def _legacy(path):
    """``rv_items`` as the released version stored it: the column, and each document holding
    ``_version`` equal to it."""
    conn = sqlite3.connect(path)
    conn.executescript(
        f"CREATE TABLE {TABLE} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL, "
        "_version INTEGER NOT NULL DEFAULT 0);"
    )
    doc = {"name": "old", "qty": 1, "owner": None, "_version": 2}
    conn.execute(f"INSERT INTO {TABLE} (data, _version) VALUES (?, 2)", [json.dumps(doc)])
    conn.commit()
    schema = conn.execute("SELECT sql FROM sqlite_master").fetchall()
    conn.close()
    return schema


def test_a_database_written_by_the_released_version_needs_no_step(mode, base, tmp_path):
    path = str(tmp_path / "old.db")
    schema = _legacy(path)
    w = _world_at(mode, base, path)
    try:
        with query_logger.capture() as sent:
            inst = w.call(w.Safe.from_id, 1, db=w.db)
            stale = w.call(w.Safe.from_id, 1, db=w.db)
            assert inst._version == 2
            w.call(w.Plain.using(w.db).filter(F("_id") == 1).update, qty=5)
            stale.name = "stale"
            with pytest.raises(StaleVersionError):
                w.call(stale.save, db=w.db)
            fresh = w.call(w.Safe.from_id, 1, db=w.db)
            fresh.name = "new"
            w.call(fresh.save, db=w.db)
            assert w.row(1)[:2] == (4, None)
            w.call(fresh.delete, db=w.db)
    finally:
        w.close()
    assert [q.sql for q in sent if q.sql.lstrip().upper().startswith(("ALTER", "DROP"))] == []
    conn = sqlite3.connect(path)
    try:
        assert (conn.execute("SELECT sql FROM sqlite_master").fetchall(), conn.execute(f"SELECT count(*) FROM {TABLE}").fetchall()) == (schema, [(0,)])
    finally:
        conn.close()


def _world_at(mode, base, path):
    w = object.__new__(World)
    w.is_async = mode == "async"
    w.loop = asyncio.new_event_loop() if w.is_async else None
    w.path = path
    w.db = (AsyncSQLerDB if w.is_async else SQLerDB).on_disk(path)
    if w.is_async:
        w.go(w.db.connect())
    w.Safe, w.Plain, w.Target = CLASSES[(mode, base)]
    return w


# ---- hostile inputs ---------------------------------------------------------------------------


@pytest.mark.parametrize("write", ["upsert_document", "_update_fields", "bulk_upsert", "db.query update"])
def test_a_column_another_object_added_after_this_one_remembered_the_table_is_moved_by_its_next_plain_write(tmp_path, write):
    path = str(tmp_path / "two.db")
    a = SQLerDB.on_disk(path)
    b = SQLerDB.on_disk(path)
    try:
        _id = a.insert_document(TABLE, {"name": "a", "qty": 1})
        a.find_document(TABLE, _id)  # ``a`` remembers the table without ``_version``
        Safe(name="b").save(db=b)  # a safe class's first save on ``b`` gives the table the column
        ways = {
            "upsert_document": lambda: a.upsert_document(TABLE, _id, {"name": "a", "qty": 2}),
            "_update_fields": lambda: a._update_fields(TABLE, _id, {"qty": 2}, ["qty"]),
            "bulk_upsert": lambda: a.bulk_upsert(TABLE, [{"_id": _id, "name": "a", "qty": 2}]),
            "db.query update": lambda: a.query(TABLE).filter(F("_id") == _id).update(qty=2),
        }
        ways[write]()
    finally:
        a.close()
        b.close()
    conn = sqlite3.connect(path)
    try:
        got = conn.execute(f"SELECT _id, _version FROM {TABLE} ORDER BY _id").fetchall()
    finally:
        conn.close()
    assert got == [(1, 1), (2, 0)]


@pytest.mark.parametrize("bound", ["using", "set_db"])
def test_a_plain_class_moves_the_version_however_it_is_bound(bound):
    db = SQLerDB.in_memory(shared=False)
    try:
        _id = Safe(name="a").save(db=db)._id
        held = Safe.from_id(_id, db=db)
        if bound == "set_db":
            with pytest.warns(DeprecationWarning):
                Plain.set_db(db)
            Plain.query().filter(F("_id") == _id).update(qty=3)
        else:
            Plain.using(db).filter(F("_id") == _id).update(qty=3)
        with pytest.raises(StaleVersionError):
            held.save(db=db)
        assert [tuple(r) for r in db.adapter.execute(f"SELECT _version FROM {TABLE}").fetchall()] == [(1,)]
    finally:
        Plain._db = None
        db.close()


def test_bulk_upsert_of_one_row_twice_in_one_call_moves_it_twice():
    """Each document is a write of the row (two ``ON CONFLICT DO UPDATE`` rows of one
    statement): the version counts writes, the last document is the row."""
    db = SQLerDB.in_memory(shared=False)
    try:
        db.ensure_table("t", versioned=True)
        _id = db.insert_document("t", {"n": 0})
        db.bulk_upsert("t", [{"_id": _id, "n": 1}, {"_id": _id, "n": 2}])
        assert [tuple(r) for r in db.adapter.execute("SELECT data, _version FROM t").fetchall()] == [('{"n":2}', 2)]
    finally:
        db.close()


@pytest.mark.parametrize("scope", ["transaction", "savepoint"])
def test_a_plain_write_rolled_back_leaves_the_version_where_it_was(scope):
    db = SQLerDB.in_memory(shared=False)
    try:
        db.ensure_table("t", versioned=True)
        _id = db.insert_document("t", {"n": 0})
        try:
            with db.transaction():
                if scope == "savepoint":
                    db.upsert_document("t", _id, {"n": 1})  # kept: the outer transaction commits
                    try:
                        with db.transaction():
                            db.upsert_document("t", _id, {"n": 2})
                            raise _Undo
                    except _Undo:
                        pass
                else:
                    db.upsert_document("t", _id, {"n": 1})
                    raise _Undo
        except _Undo:
            pass
        rows = [tuple(r) for r in db.adapter.execute("SELECT data, _version FROM t").fetchall()]
    finally:
        db.close()
    assert rows == ([('{"n":1}', 1)] if scope == "savepoint" else [('{"n":0}', 0)])


class _Undo(Exception):
    pass


def test_a_save_after_a_plain_update_one_that_matched_no_row_goes_through():
    db = SQLerDB.in_memory(shared=False)
    try:
        inst = Safe(name="a").save(db=db)
        assert Plain.using(db).filter(F("name") == "nobody").update_one(qty=1) is None
        inst.name = "b"
        inst.save(db=db)
        assert [tuple(r) for r in db.adapter.execute(f"SELECT _version FROM {TABLE}").fetchall()] == [(1,)]
    finally:
        db.close()


def test_a_promoted_column_and_a_search_index_kept_by_triggers_follow_a_write_that_moves_the_version_once():
    from sqler.fts import FTSIndex

    class Doc(SQLerSafeModel):
        __tablename__ = "rv_docs"
        __promoted__ = {"status": "TEXT"}
        title: str = ""
        status: str = ""

    db = SQLerDB.in_memory(shared=False)
    try:
        d = Doc(title="alpha", status="new").save(db=db)
        index = FTSIndex(Doc, ["title"], index_name="rv_docs_search", db=db)
        index.create(db)
        db.upsert_document("rv_docs", d._id, {"title": "beta", "status": "open"})
        row = tuple(db.adapter.execute("SELECT data, status, _version FROM rv_docs").fetchone())
        assert (row, [h.title for h in index.search("beta")], index.search("alpha")) == (('{"title":"beta"}', "open", 1), ["beta"], [])
    finally:
        db.close()


def test_a_set_based_update_of_2000_plain_rows_moves_each_once_in_one_statement():
    db = SQLerDB.in_memory(shared=False)
    try:
        db.ensure_table(TABLE, versioned=True)
        db.insert_many(TABLE, [{"name": "r", "qty": i} for i in range(2000)])
        with query_logger.capture() as sent:
            assert Plain.using(db).update(name="s") == 2000
        rows = db.adapter.execute(f"SELECT _version, count(*) FROM {TABLE} GROUP BY _version").fetchall()
    finally:
        db.close()
    assert ([tuple(r) for r in rows], len([q for q in sent if q.sql.startswith("UPDATE")])) == ([(1, 2000)], 1)


@pytest.mark.parametrize("delete", ["delete()", "delete_all()"])
def test_a_save_of_an_instance_read_before_a_delete_writes_nothing_back(delete):
    """Nothing to move: the row is gone, and the safe save is refused as it always was."""
    db = SQLerDB.in_memory(shared=False)
    try:
        inst = Safe(name="a").save(db=db)
        held = Safe.from_id(inst._id, db=db)
        inst.delete(db=db) if delete == "delete()" else Plain.using(db).delete_all()
        held.name = "b"
        with pytest.raises(StaleVersionError):
            held.save(db=db)
        assert db.adapter.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("way", ["soft_delete", "restore"])
def test_a_mixin_that_writes_the_row_moves_its_version(mode, tmp_path, way):
    w = World(mode, "pydantic_safe", tmp_path)
    try:
        _id, _ = _saved_twice(w)
        held = w.call(w.Safe.from_id, _id, db=w.db)
        plain = w.call(w.Plain.from_id, _id, db=w.db)
        w.call(plain.soft_delete, db=w.db)
        if way == "restore":
            w.call(plain.restore, db=w.db)
        held.name = "stale"
        with pytest.raises(StaleVersionError):
            w.call(held.save, db=w.db)
        assert w.row(_id)[:2] == (2 if way == "soft_delete" else 3, None)
    finally:
        w.close()


# ---- a query built from an adapter alone: no database object to ask ----------------------------


def _bare(w, table=TABLE):
    """A query of ``table`` built from the adapter alone, as ``SQLerQuery(table, adapter)``."""
    return (AsyncSQLerQuery if w.is_async else SQLerQuery)(table, w.db.adapter)


BARE_UPDATES = {
    "update()": lambda q: q.update(qty=9),
    "update_one()": lambda q: q.update_one(qty=9),
    "update_n()": lambda q: q.update_n(1, qty=9),
}


@pytest.mark.parametrize("update", list(BARE_UPDATES))
def test_a_query_built_from_an_adapter_alone_moves_the_version_and_a_save_read_before_it_is_refused(mode, tmp_path, update):
    w = World(mode, "pydantic_safe", tmp_path)
    try:
        _id, _ = _saved_twice(w)
        held = w.call(w.Safe.from_id, _id, db=w.db)
        w.go(BARE_UPDATES[update](_bare(w).filter(F("_id") == _id)))
        version, copy, doc = w.row(_id)
        assert (version, copy, doc["qty"]) == (2, None, 9)
        held.name = "stale"
        with pytest.raises(StaleVersionError):
            w.call(held.save, db=w.db)
    finally:
        w.close()


def test_a_bare_async_query_inside_a_transaction_moves_the_version(tmp_path):
    w = World("async", "pydantic_safe", tmp_path)
    try:
        _id, _ = _saved_twice(w)

        async def update():
            async with w.db.transaction():
                return await _bare(w).filter(F("_id") == _id).update_one(qty=9)

        assert w.go(update())["_version"] == 2
        assert w.row(_id)[:2] == (2, None)
    finally:
        w.close()


def test_a_bare_query_made_before_another_connection_added_the_column_moves_it(tmp_path):
    path = str(tmp_path / "two.db")
    a, b = SQLerDB.on_disk(path), SQLerDB.on_disk(path)
    try:
        _id = a.insert_document(TABLE, {"name": "a", "qty": 1})
        q = SQLerQuery(TABLE, a.adapter).filter(F("_id") == _id)
        q.update(qty=2)  # the table has no column yet
        Safe(name="b").save(db=b)  # a safe class's first save on ``b`` gives the table the column
        held = Safe.from_id(_id, db=b)
        q.update(qty=3)
        with pytest.raises(StaleVersionError):
            held.save(db=b)
        rows = [tuple(r) for r in a.adapter.execute(f"SELECT _id, _version FROM {TABLE} ORDER BY _id").fetchall()]
    finally:
        a.close()
        b.close()
    assert rows == [(1, 1), (2, 0)]


def test_a_bare_query_that_changes_no_row_moves_no_version():
    db = SQLerDB.in_memory(shared=False)
    try:
        inst = Safe(name="a").save(db=db)
        q = SQLerQuery(TABLE, db.adapter)
        with pytest.raises(ValueError, match="n must be >= 1"):
            q.update_n(0, qty=1)
        nobody = q.filter(F("name") == "nobody")
        assert (nobody.update(qty=1), nobody.update_one(qty=1), nobody.update_n(2, qty=1)) == (0, None, [])
        inst.name = "b"
        inst.save(db=db)
        assert [tuple(r) for r in db.adapter.execute(f"SELECT _version FROM {TABLE}").fetchall()] == [(1,)]
    finally:
        db.close()


def test_a_bare_query_on_a_table_without_the_column_sends_the_update_it_always_did():
    """One read of the table's columns first, each time (a memory could be stale), then the
    update as it always was."""
    db = SQLerDB.in_memory(shared=False)
    try:
        db.insert_document("t", {"a": 1})
        with query_logger.capture() as sent:
            assert SQLerQuery("t", db.adapter).filter(F("a") == 1).update(a=2) == 1
    finally:
        db.close()
    assert [q.sql for q in sent] == [
        "SELECT name FROM pragma_table_xinfo(?);",
        "UPDATE t SET data = json_set(data, '$.a', ?) WHERE JSON_EXTRACT(data, '$.a') = ?",
    ]


# ---- a move that changes what a read gives moves the version --------------------------------------


@pytest.mark.parametrize("keep", ["document", "column"])
def test_a_move_moves_the_version_of_each_row_whose_read_it_changes_and_no_other(keep):
    """Three rows of a settled promoted column whose documents an older writer gave the key
    again: one disagrees with the column, one agrees, one has no key."""

    class Moved(SQLerSafeModel):
        __tablename__ = "rv_moved"
        __promoted__ = {"status": "TEXT"}
        title: str = ""
        status: str = ""

    db = SQLerDB.in_memory(shared=False)
    try:
        ids = [Moved(title=t, status=s).save(db=db)._id for t, s in (("a", "c"), ("b", "same"), ("n", "x"))]
        for _id, key in zip(ids, ("d", "same")):
            db.adapter.execute("UPDATE rv_moved SET data = json_set(data, '$.status', ?) WHERE _id = ?", [key, _id])
        db.adapter.auto_commit()
        held = Moved.from_id(ids[0], db=db)
        db.move_promoted("rv_moved", "status", keep=keep)
        rows = [tuple(r) for r in db.adapter.execute("SELECT status, _version FROM rv_moved ORDER BY _id").fetchall()]
        held.title = "from the instance read before"
        if keep == "document":
            with pytest.raises(StaleVersionError):
                held.save(db=db)
        else:
            held.save(db=db)
    finally:
        db.close()
    assert rows == ([("d", 1), ("same", 0), ("x", 0)] if keep == "document" else [("c", 0), ("same", 0), ("x", 0)])


# ---- inside a transaction opened on the adapter ---------------------------------------------------


def test_a_table_without_the_column_is_written_as_always_inside_a_transaction_opened_on_the_adapter(tmp_path):
    """Nothing can be remembered there, so a database object that has not seen the table reads
    it for each write; the statements carry no guard, as they never did."""
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)")
    conn.execute("""INSERT INTO t (data) VALUES ('{"a":1}')""")
    conn.commit()
    conn.close()
    db = SQLerDB.on_disk(path)
    try:
        db.adapter.begin_transaction()
        with query_logger.capture() as sent:
            db.insert_document("t", {"a": 2})
            db.upsert_document("t", 1, {"a": 3})
            db.query("t").update(a=4)
        db.adapter.end_transaction()
    finally:
        db.close()
    assert [q.sql for q in sent if "CREATE TABLE" not in q.sql] == [
        'PRAGMA table_xinfo("t");',
        "INSERT INTO t (data) VALUES (json(?));",
        'PRAGMA table_xinfo("t");',
        "UPDATE t SET data = json(?) WHERE _id = ?;",
        'PRAGMA table_xinfo("t");',
        "UPDATE t SET data = json_set(data, '$.a', ?)",
    ]
