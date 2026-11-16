"""An instance belongs to the database it was read from or written to, on every base, sync
and async: ``save()``, ``refresh()`` and ``delete()`` with no ``db=`` act there, whatever
database its class is bound to, and a class bound to none works from the instance alone."""

import asyncio
import gc
import inspect
import itertools
import sqlite3
import types
import warnings
import weakref
from dataclasses import dataclass
from typing import Optional

import pytest
from pydantic import BaseModel

import sqler.models as models
from sqler import SQLerDB
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import NotConnectedError, StaleVersionError
from sqler.logging import query_logger
from sqler.models import SQLerLiteModelBase, SQLerMsgspecModelBase
from sqler.models.mixins import AsyncAuditLogMixin, AuditLogMixin
from sqler.query import F

# Every public base class, as the package exports it.
BASES = [
    base
    for base in (getattr(models, name) for name in models.__all__)
    if isinstance(base, type) and hasattr(base, "from_id") and hasattr(base, "delete_with_policy")
]
ASYNC_BASES = [b for b in BASES if inspect.iscoroutinefunction(b.delete)]
SAFE_BASES = [b for b in BASES if getattr(b, "_versioned_rows", False)]

_tables = itertools.count()


def declare(base, name: str, fields: dict, mixins: tuple = ()):
    """A class on ``base`` with ``fields`` (each ``name: (type, default)``) and its own table."""
    table = f"ih_{name.lower()}_{next(_tables)}"
    ns = {
        "__tablename__": table,
        "__annotations__": {k: t for k, (t, _) in fields.items()},
        **{k: d for k, (_, d) in fields.items()},
    }
    bases = (*mixins, base)
    if issubclass(base, SQLerLiteModelBase):
        return dataclass(types.new_class(name, bases, {}, lambda body: body.update(ns)))
    if issubclass(base, SQLerMsgspecModelBase):
        return types.new_class(name, bases, {"kw_only": True}, lambda body: body.update(ns))
    if issubclass(base, BaseModel):
        return types.new_class(name, bases, {}, lambda body: body.update(ns))
    raise AssertionError(f"no way to declare a class on {base.__name__}: add its family")


def row_class(base, mixins: tuple = ()):
    return declare(base, "Row", {"name": (str, "")}, mixins)


def bind(cls, db) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)


async def settle(value):
    return await value if inspect.isawaitable(value) else value


async def open_db(base, path=None):
    if base in ASYNC_BASES:
        db = AsyncSQLerDB.on_disk(str(path)) if path else AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        return db
    return SQLerDB.on_disk(str(path)) if path else SQLerDB.in_memory(shared=False)


async def close(*dbs) -> None:
    for db in dbs:
        await settle(db.close())


async def name_of(db, cls, rid: int = 1):
    doc = await settle(db.find_document(cls.__tablename__, rid))
    return None if doc is None else doc["name"]


async def two_rows(base, cls, home, elsewhere) -> None:
    """Row 1 of ``cls`` in ``home`` ("home row") and in ``elsewhere`` ("elsewhere row"),
    each written by the class, so a versioned class's table has its version column."""
    await settle(cls(name="home row").save(db=home))
    await settle(cls(name="elsewhere row").save(db=elsewhere))


# ----- a class bound to one database, a row read from another ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_read_from_another_database_is_refreshed_from_there(base):
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await two_rows(base, Row, home, elsewhere)
        got = await settle(Row.using(elsewhere).first())
        got.name = "edited, not saved"
        await settle(got.refresh())
        assert got.name == "elsewhere row"
    finally:
        await close(home, elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_read_from_another_database_is_saved_there(base):
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await two_rows(base, Row, home, elsewhere)
        got = await settle(Row.using(elsewhere).first())
        got.name = "elsewhere row, edited"
        await settle(got.save())
        assert (await name_of(home, Row), await name_of(elsewhere, Row)) == (
            "home row",
            "elsewhere row, edited",
        )
    finally:
        await close(home, elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_read_from_another_database_is_deleted_there(base):
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await two_rows(base, Row, home, elsewhere)
        got = await settle(Row.using(elsewhere).first())
        await settle(got.delete())
        assert (await name_of(home, Row), await name_of(elsewhere, Row)) == ("home row", None)
    finally:
        await close(home, elsewhere)


# ----- a class bound to no database ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_an_unbound_class_saves_refreshes_and_deletes_an_instance_it_saved_with_db(base):
    db = await open_db(base)
    try:
        Row = row_class(base)
        m = await settle(Row(name="a").save(db=db))
        m.name = "b"
        await settle(m.save())
        assert await name_of(db, Row) == "b"
        m.name = "edited, not saved"
        await settle(m.refresh())
        assert m.name == "b"
        await settle(m.delete())
        assert await name_of(db, Row) is None
    finally:
        await close(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_an_unbound_class_saves_refreshes_and_deletes_an_instance_it_read_with_using(base):
    db = await open_db(base)
    try:
        Row = row_class(base)
        await settle(Row(name="a").save(db=db))
        got = await settle(Row.using(db).first())
        got.name = "c"
        await settle(got.save())
        assert await name_of(db, Row) == "c"
        got.name = "edited, not saved"
        await settle(got.refresh())
        assert got.name == "c"
        await settle(got.delete())
        assert await name_of(db, Row) is None
    finally:
        await close(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_from_id_and_refresh_take_db_on_every_base(base):
    db = await open_db(base)
    try:
        Row = row_class(base)
        await settle(Row(name="a").save(db=db))
        got = await settle(Row.from_id(1, db=db))
        assert (got.name, got._id) == ("a", 1)
        assert await settle(Row.from_id(2, db=db)) is None
        got.name = "edited, not saved"
        await settle(got.refresh(db=db))
        assert got.name == "a"
    finally:
        await close(db)


# ----- every loader gives its instances their home ---------------------------------------


LOADERS = {
    "from_id(id, db=)": lambda cls, db: cls.from_id(1, db=db),
    "from_ids([id], db=)": lambda cls, db: _first(cls.from_ids([1], db=db)),
    "using().first()": lambda cls, db: cls.using(db).first(),
    "using().all()": lambda cls, db: _first(cls.using(db).all()),
    "using().resolve(False).all()": lambda cls, db: _first(cls.using(db).resolve(False).all()),
    "using().update_one()": lambda cls, db: cls.using(db).filter(F("_id") == 1).update_one(name="u"),
    "using().update_n()": lambda cls, db: _first(
        cls.using(db).filter(F("_id") == 1).update_n(1, name="u")
    ),
}


async def _first(found):
    found = await settle(found)
    assert len(found) == 1
    return found[0]


LOADED = [(base, loader) for base in BASES for loader in LOADERS]


@pytest.mark.asyncio
@pytest.mark.parametrize(("base", "loader"), LOADED, ids=lambda x: getattr(x, "__name__", x))
async def test_a_row_each_loader_reads_from_another_database_is_saved_there(base, loader):
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await two_rows(base, Row, home, elsewhere)
        got = await settle(LOADERS[loader](Row, elsewhere))
        got.name = "saved"
        await settle(got.save())
        assert (await name_of(home, Row), await name_of(elsewhere, Row)) == ("home row", "saved")
    finally:
        await close(home, elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [b for b in BASES if b not in ASYNC_BASES], ids=lambda b: b.__name__)
async def test_a_row_streamed_from_another_database_is_saved_there(base):
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await two_rows(base, Row, home, elsewhere)
        got = next(iter(Row.using(elsewhere).iter()))
        got.name = "saved"
        got.save()
        assert (await name_of(home, Row), await name_of(elsewhere, Row)) == ("home row", "saved")
    finally:
        await close(home, elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_refresh_from_another_database_moves_the_home_there(base):
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await two_rows(base, Row, home, elsewhere)
        got = await settle(Row.from_id(1))
        await settle(got.refresh(db=elsewhere))
        assert got.name == "elsewhere row"
        got.name = "saved"
        await settle(got.save())
        assert (await name_of(home, Row), await name_of(elsewhere, Row)) == ("home row", "saved")
    finally:
        await close(home, elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_child_loaded_through_its_parent_from_another_database_is_saved_there(base):
    """The parent's class is bound to ``home``, the child's to none: the child is read from
    the database its parent was read from, and goes back there."""
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Child = declare(base, "Child", {"name": (str, "")})
        Parent = declare(base, "Parent", {"title": (str, ""), "child": (Optional[Child], None)})
        bind(Parent, home)
        child = await settle(Child(name="elsewhere child").save(db=elsewhere))
        await settle(Parent(title="p", child=child).save(db=elsewhere))
        got = await settle(Parent.using(elsewhere).first())
        got.child.name = "edited child"
        await settle(got.child.save())
        assert await name_of(elsewhere, Child) == "edited child"
        assert await settle(home.find_document(Child.__tablename__, 1)) is None
    finally:
        await close(home, elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_child_of_a_bound_class_comes_from_its_class_database_and_goes_back_there(base):
    """A referenced row of a class bound with ``set_db()`` is read from that database (the
    loader's rule), so that is its home."""
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Child = declare(base, "Child", {"name": (str, "")})
        Parent = declare(base, "Parent", {"title": (str, ""), "child": (Optional[Child], None)})
        bind(Child, home)
        await settle(Child(name="home child").save(db=home))
        await settle(Child(name="elsewhere child").save(db=elsewhere))
        await settle(Parent(title="p", child=await settle(Child.from_id(1))).save(db=elsewhere))
        got = await settle(Parent.using(elsewhere).first())
        assert got.child.name == "home child"
        got.child.name = "edited child"
        await settle(got.child.save())
        assert (await name_of(home, Child), await name_of(elsewhere, Child)) == (
            "edited child",
            "elsewhere child",
        )
    finally:
        await close(home, elsewhere)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_read_from_another_table_of_the_same_database_is_saved_to_that_table(base):
    db = await open_db(base)
    try:
        Row = row_class(base)
        Other = declare(base, "Other", {"name": (str, "")})
        await settle(Other(name="in other").save(db=db))
        got = await settle(Row.using(db, Other.__tablename__).first())
        got.name = "saved"
        await settle(got.save())
        assert await name_of(db, Other) == "saved"
        assert await settle(db.find_document(Row.__tablename__, 1)) is None
    finally:
        await close(db)


# ----- a write moves the home only when it succeeded --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("audited", [False, True], ids=["plain", "audit log"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_save_many_to_a_database_makes_it_the_home_of_each_instance(base, audited):
    other = await open_db(base)
    try:
        audit = (AsyncAuditLogMixin if base in ASYNC_BASES else AuditLogMixin,) if audited else ()
        Row = row_class(base, audit)
        batch = [Row(name="a"), Row(name="b")]
        await settle(Row.save_many(batch, db=other))
        batch[0].name = "saved"
        await settle(batch[0].save())
        assert [await name_of(other, Row, i) for i in (1, 2)] == ["saved", "b"]
    finally:
        await close(other)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_deleted_instance_keeps_its_home_and_a_save_writes_it_back_there(base):
    home, elsewhere = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await two_rows(base, Row, home, elsewhere)
        got = await settle(Row.using(elsewhere).first())
        await settle(got.delete())
        assert got._id is None
        await settle(got.save())
        assert got._id == 2
        assert (await name_of(home, Row, 2), await name_of(elsewhere, Row, 2)) == (
            None,
            "elsewhere row",
        )
    finally:
        await close(home, elsewhere)


# ----- what the home is not: a fresh instance, a rebound class, a closed or dropped database


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_new_instance_of_a_bound_class_saves_to_the_class_database(base):
    home = await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, home)
        await settle(Row(name="new").save())
        assert await name_of(home, Row) == "new"
    finally:
        await close(home)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_an_instance_read_before_its_class_is_rebound_saves_where_it_was_read(base):
    a, b = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, a)
        await settle(Row(name="in a").save())
        await settle(Row(name="in b").save(db=b))
        got = await settle(Row.from_id(1))
        bind(Row, b)
        got.name = "saved"
        await settle(got.save())
        assert (await name_of(a, Row), await name_of(b, Row)) == ("saved", "in b")
    finally:
        await close(a, b)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [b for b in BASES if b not in ASYNC_BASES], ids=lambda b: b.__name__)
async def test_a_save_whose_home_is_closed_raises_its_error_and_writes_nowhere_else(base):
    gone, bound = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, bound)
        m = await settle(Row(name="in gone").save(db=gone))
        await close(gone)
        m.name = "after close"
        with pytest.raises(NotConnectedError):
            m.save()
        assert bound.find_document(Row.__tablename__, 1) is None
    finally:
        await close(bound)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ASYNC_BASES, ids=lambda b: b.__name__)
async def test_a_save_whose_async_home_was_closed_goes_to_that_database_only(base, tmp_path):
    """A closed async database takes a later write (it connects again); the write goes to it,
    never to the class's database."""
    path = tmp_path / "closed.db"
    gone, bound = await open_db(base, path), await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, bound)
        m = await Row(name="in gone").save(db=gone)
        await gone.close()
        m.name = "after close"
        await m.save()
        assert await bound.find_document(Row.__tablename__, 1) is None
    finally:
        await close(gone, bound)
    reader = await open_db(base, path)
    try:
        assert await name_of(reader, Row) == "after close"
    finally:
        await close(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_dropped_database_object_stays_the_home_and_takes_the_save(base, tmp_path):
    path = tmp_path / "dropped.db"
    Row = row_class(base)
    db = await open_db(base, path)
    m = await settle(Row(name="before").save(db=db))
    kept = weakref.ref(db)
    del db
    gc.collect()
    assert kept() is not None  # the instance holds its database
    m.name = "after"
    await settle(m.save())
    await close(kept())
    reader = await open_db(base, path)
    try:
        assert await name_of(reader, Row) == "after"
    finally:
        await close(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ASYNC_BASES, ids=lambda b: b.__name__)
async def test_two_tasks_with_two_databases_keep_their_rows_in_their_database(base):
    a, b = await open_db(base), await open_db(base)
    try:
        Row = row_class(base)
        await settle(Row(name="a0").save(db=a))
        await settle(Row(name="b0").save(db=b))
        turn = asyncio.Event()

        async def work(db, mark):
            got = await Row.using(db).first()
            await turn.wait()
            got.name = mark
            await got.save()
            await got.refresh()
            return got.name

        tasks = [asyncio.create_task(work(a, "from a")), asyncio.create_task(work(b, "from b"))]
        await asyncio.sleep(0)
        turn.set()
        assert await asyncio.gather(*tasks) == ["from a", "from b"]
        assert (await name_of(a, Row), await name_of(b, Row)) == ("from a", "from b")
    finally:
        await close(a, b)


# ----- a versioned class's query knows the version through every door --------------------

WAYS = {
    "update": lambda q: q.update(balance=500),
    "update_one": lambda q: q.update_one(balance=500),
    "update_n": lambda q: q.update_n(1, balance=500),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["query()", "using(db)", "using(db, table)"])
@pytest.mark.parametrize("way", list(WAYS))
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_a_set_update_through_every_door_refuses_a_stale_save(base, way, door):
    db = await open_db(base)
    try:
        Acct = declare(base, "Acct", {"owner": (str, ""), "balance": (int, 0)})
        bind(Acct, db)
        if door == "using(db, table)":
            Other = declare(base, "OtherAcct", {"owner": (str, ""), "balance": (int, 0)})
            await settle(Other(owner="o", balance=100).save(db=db))
            table = Other.__tablename__
            held = await settle(Acct.using(db, table).first())
            query = Acct.using(db, table)
        else:
            table = Acct.__tablename__
            await settle(Acct(owner="o", balance=100).save())
            held = await settle(Acct.from_id(1))
            query = Acct.query() if door == "query()" else Acct.using(db)
        await settle(WAYS[way](query.filter(F("_id") == 1)))
        held.owner = "h"
        with pytest.raises(StaleVersionError):
            await settle(held.save())
        doc = await settle(db.find_document_with_version(table, 1))
        assert (doc["owner"], doc["balance"], doc["_version"]) == ("o", 500, 1)
    finally:
        await close(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_an_instance_read_through_using_holds_its_row_version(base):
    db = await open_db(base)
    try:
        Acct = declare(base, "Acct", {"owner": (str, ""), "balance": (int, 0)})
        a = await settle(Acct(owner="o").save(db=db))
        for owner in ("p", "q"):
            a.owner = owner
            await settle(a.save())
        got = await settle(Acct.using(db).first())
        assert got._version == 2
        got.owner = "r"
        await settle(got.save())
        doc = await settle(db.find_document_with_version(Acct.__tablename__, 1))
        assert (doc["owner"], doc["_version"]) == ("r", 3)
    finally:
        await close(db)


# ----- no read changes the schema; a versioned query asks for the version column once -----


def bind_unversioned(cls, db) -> None:
    """Bind ``cls`` to ``db`` as its plain base binds, without the ``_version`` column the
    sync safe bases' ``set_db()`` adds to the table."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _plain_of_bound(cls).set_db.__func__(cls, db)


def _plain_of_bound(cls):
    """The plain base class ``cls`` (a safe class, or a safe base) is built on."""
    for klass in cls.__mro__:
        if klass in BASES and not getattr(klass, "_versioned_rows", False):
            return klass
    raise AssertionError(f"{cls.__name__} has no plain base")


def _sqlite(path: str, *statements: str) -> None:
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _columns(path: str, table: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _table_names_at(path: str) -> set:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


ORIGINS = ["plain class", "plain SQL", "copy of a versioned table"]


async def _old_table(base, path: str, table: str, origin: str) -> None:
    """``table`` at ``path`` with one row named "old" and no ``_version`` column."""
    _sqlite(path, "PRAGMA journal_mode = WAL")
    if origin == "plain SQL":
        _sqlite(
            path,
            f"CREATE TABLE {table} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)",
            f"INSERT INTO {table} (data) VALUES ('{{\"name\": \"old\"}}')",
        )
        return
    maker = row_class(_plain_of_bound(base) if origin == "plain class" else base)
    db = await open_db(base, path)
    try:
        await settle(maker(name="old").save(db=db))
    finally:
        await close(db)
    if origin == "plain class":
        _sqlite(path, f"ALTER TABLE {maker.__tablename__} RENAME TO {table}")
    else:
        _sqlite(path, f"CREATE TABLE {table} AS SELECT _id, data FROM {maker.__tablename__}")
    assert _columns(path, table) == ["_id", "data"]


async def _door(door: str, Row, Parent, Other, db):
    """The row of id 1 that ``door`` reads from ``db``."""
    if door == "from_id":
        return await settle(Row.from_id(1, db=db))
    if door == "from_ids":
        return await _first(Row.from_ids([1], db=db))
    if door == "refresh":
        got = await settle(Row.from_id(1, db=db))
        await settle(got.refresh())
        return got
    if door == "reference":
        return (await settle(Parent.from_id(1, db=db))).child
    if door == "query()":
        return await _first(Row.query().all())
    if door == "using(db)":
        return await settle(Row.using(db).first())
    if door == "using(db, table)":
        return await _first(Other.using(db, Row.__tablename__).all())
    if door == "iter()":
        return next(iter(Row.using(db).iter()))
    raise AssertionError(door)


READ_DOORS = ["from_id", "from_ids", "refresh", "reference", "query()", "using(db)", "using(db, table)"]
DOORED = [
    (base, door)
    for base in SAFE_BASES
    for door in [*READ_DOORS, *([] if base in ASYNC_BASES else ["iter()"])]
]


async def _world(base, path: str, origin: str):
    """A safe class ``Row`` whose table at ``path`` has no ``_version`` column, with a plain
    ``Parent`` row that refers to its row 1, and a safe ``Other`` on another table."""
    Row = row_class(base)
    await _old_table(base, path, Row.__tablename__, origin)
    Parent = declare(_plain_of_bound(base), "Parent", {"child": (Optional[Row], None)})
    Other = row_class(base)
    db = await open_db(base, path)
    try:
        child = await settle(Row.from_id(1, db=db))
        await settle(Parent(child=child).save(db=db))
    finally:
        await close(db)
    assert _columns(path, Row.__tablename__) == ["_id", "data"]
    return Row, Parent, Other


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize(("base", "door"), DOORED, ids=lambda x: getattr(x, "__name__", x))
async def test_a_versioned_read_of_a_table_without_the_column_works_on_a_database_that_cannot_be_written(
    base, door, origin, tmp_path
):
    path = str(tmp_path / "old.db")
    Row, Parent, Other = await _world(base, path, origin)
    db = await open_db(base, f"file:{path}?mode=ro")
    try:
        bind_unversioned(Row, db)
        got = await _door(door, Row, Parent, Other, db)
        assert (got._id, got.name, got._version) == (1, "old", 0)
    finally:
        await close(db)
    assert _columns(path, Row.__tablename__) == ["_id", "data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("base", "door"), DOORED, ids=lambda x: getattr(x, "__name__", x))
async def test_a_holder_who_read_a_table_without_the_column_at_version_0_saves_and_adds_it(
    base, door, tmp_path
):
    path = str(tmp_path / "old.db")
    Row, Parent, Other = await _world(base, path, "plain SQL")
    db = await open_db(base, path)
    try:
        bind_unversioned(Row, db)
        got = await _door(door, Row, Parent, Other, db)
        assert _columns(path, Row.__tablename__) == ["_id", "data"]
        got.name = "new"
        await settle(got.save())
        assert got._version == 1
        doc = await settle(db.find_document_with_version(Row.__tablename__, 1))
        assert (doc["name"], doc["_version"]) == ("new", 1)
    finally:
        await close(db)
    assert _columns(path, Row.__tablename__) == ["_id", "data", "_version"]


SET_DOORS = ["query()", "using(db)", "using(db, table)"]


def _set_query(door: str, Row, Other, db):
    if door == "query()":
        return Row.query()
    if door == "using(db)":
        return Row.using(db)
    return Other.using(db, Row.__tablename__)


def _g(table, *names):
    """The guard a statement built from what its database remembers of ``table`` carries
    (``names``: the columns ``SELECT *`` gives): a branch that never runs."""
    row = ", ".join(f"`{name}`" for name in names)
    return f"CASE WHEN 1 THEN 1 ELSE (SELECT ({row}) IN (SELECT * FROM {table}) FROM {table}) END"


async def _sent(run):
    with query_logger.capture() as log:
        result = await settle(run())
    return result, [entry.sql for entry in log]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", SET_DOORS)
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_count_and_exists_on_a_table_without_the_column_send_their_one_statement(base, door, tmp_path):
    path = str(tmp_path / "old.db")
    Row, _, Other = await _world(base, path, "plain SQL")
    db = await open_db(base, path)
    try:
        bind_unversioned(Row, db)
        table = Row.__tablename__
        count = f"SELECT COUNT(*) FROM {table} WHERE {_g(table, '_id', 'data')}"
        assert await _sent(lambda: _set_query(door, Row, Other, db).count()) == (
            1,
            [f'PRAGMA table_xinfo("{table}");', count],  # the database learns the table once
        )
        assert await _sent(lambda: _set_query(door, Row, Other, db).exists()) == (
            True,
            [count],  # the one statement exists() sends
        )
    finally:
        await close(db)
    assert _columns(path, Row.__tablename__) == ["_id", "data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", SET_DOORS)
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_delete_all_on_a_table_without_the_column_deletes_and_keeps_the_schema(base, door, tmp_path):
    path = str(tmp_path / "old.db")
    Row, _, Other = await _world(base, path, "plain SQL")
    db = await open_db(base, path)
    try:
        bind_unversioned(Row, db)
        # the plain Parent row refers to row 1: its reference is nulled, its schema kept
        assert await settle(_set_query(door, Row, Other, db).delete_all(on_delete="set_null")) == 1
        assert await settle(Row.using(db).all()) == []
    finally:
        await close(db)
    assert _columns(path, Row.__tablename__) == ["_id", "data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", SET_DOORS)
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_a_set_update_of_a_table_without_the_column_adds_it_and_moves_the_version(base, door, tmp_path):
    path = str(tmp_path / "old.db")
    Row, _, Other = await _world(base, path, "plain SQL")
    db = await open_db(base, path)
    try:
        bind_unversioned(Row, db)
        assert await settle(_set_query(door, Row, Other, db).filter(F("_id") == 1).update(name="u")) == 1
        got = await settle(Row.from_id(1, db=db))
        assert (got.name, got._version) == ("u", 1)
        got.name = "saved"
        await settle(got.save())
        doc = await settle(db.find_document_with_version(Row.__tablename__, 1))
        assert (doc["name"], doc["_version"]) == ("saved", 2)
    finally:
        await close(db)
    assert _columns(path, Row.__tablename__) == ["_id", "data", "_version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_a_versioned_read_of_a_missing_table_is_empty_and_creates_nothing(base, tmp_path):
    path = str(tmp_path / "empty.db")
    _sqlite(path, "PRAGMA journal_mode = WAL")
    db = await open_db(base, path)
    try:
        Row, Other = row_class(base), row_class(base)
        table = Row.__tablename__
        assert await settle(Row.from_id(1, db=db)) is None
        assert await settle(Row.from_ids([1], db=db)) == []
        assert await settle(Row.using(db).all()) == []
        assert await settle(Other.using(db, table).first()) is None
        assert await settle(Row.using(db).count()) == 0
    finally:
        await close(db)
    assert table not in _table_names_at(path)


SET_WRITES = {
    "update": (lambda q: q.update(name="u"), 0),
    "update_one": (lambda q: q.update_one(name="u"), None),
    "update_n": (lambda q: q.update_n(1, name="u"), []),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", list(SET_WRITES))
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_a_set_update_of_a_missing_table_finds_no_row_and_creates_nothing(base, way, tmp_path):
    path = str(tmp_path / "empty.db")
    _sqlite(path, "PRAGMA journal_mode = WAL")
    write, nothing = SET_WRITES[way]
    db = await open_db(base, path)
    try:
        Row = row_class(base)
        assert await settle(write(Row.using(db))) == nothing
    finally:
        await close(db)
    assert _table_names_at(path) - {"sqlite_sequence"} == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["update_one", "update_n"])
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_a_set_update_that_returns_rows_adds_the_column_to_a_table_without_it(base, way, tmp_path):
    path = str(tmp_path / "old.db")
    Row, _, _ = await _world(base, path, "plain SQL")
    write, _ = SET_WRITES[way]
    db = await open_db(base, path)
    try:
        got = await settle(write(Row.using(db).filter(F("_id") == 1)))
        rows = got if isinstance(got, list) else [got]
        assert [(r.name, r._version) for r in rows] == [("u", 1)]
    finally:
        await close(db)
    assert _columns(path, Row.__tablename__) == ["_id", "data", "_version"]


def _with_version_column(base, path: str):
    """A safe class with one row saved at ``path``, so its table has the column."""
    Row = row_class(base)

    async def make():
        db = await open_db(base, path)
        try:
            await settle(Row(name="a").save(db=db))
        finally:
            await close(db)
        return Row

    return make()


@pytest.mark.asyncio
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_each_door_asks_for_the_version_column_once_per_database_object(base, tmp_path):
    path = str(tmp_path / "v.db")
    Row = await _with_version_column(base, path)
    Other = row_class(base)
    t = Row.__tablename__
    look = f'PRAGMA table_xinfo("{t}");'
    guard = _g(t, "_id", "data", "_version")
    by_id = f"SELECT _id, data, _version FROM {t} WHERE _id IN (?) AND {guard};"
    for door, select in (
        ("using(db).all()", lambda db: Row.using(db).all()),
        ("using(db, table).all()", lambda db: Other.using(db, t).all()),
        ("from_id", lambda db: Row.from_id(1, db=db)),
        ("from_ids", lambda db: Row.from_ids([1], db=db)),
    ):
        db = await open_db(base, path)
        try:
            first = (await _sent(lambda: select(db)))[1]
            second = (await _sent(lambda: select(db)))[1]
            count = (await _sent(lambda: Row.using(db).count()))[1]
        finally:
            await close(db)
        read = f"SELECT _id, data, _version FROM {t} WHERE {guard}" if door.startswith("using") else by_id
        assert (door, first, second, count) == (door, [look, read], [read], [f"SELECT COUNT(*) FROM {t} WHERE {guard}"])


@pytest.mark.asyncio
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_a_query_made_before_another_database_object_adds_the_column_reads_it_after(base, tmp_path):
    path = str(tmp_path / "old.db")
    Row, _, _ = await _world(base, path, "plain SQL")
    t = Row.__tablename__
    reader, writer = await open_db(base, path), await open_db(base, path)
    try:
        look = f'PRAGMA table_xinfo("{t}");'
        old = f"SELECT _id, data FROM {t} WHERE {_g(t, '_id', 'data')}"
        new = f"SELECT _id, data, _version FROM {t} WHERE {_g(t, '_id', 'data', '_version')}"
        # no column: the table is learned once, without it
        for sent_here in ([look, old], [old]):
            got, sent = await _sent(lambda: Row.using(reader).all())
            assert ([(g.name, g._version) for g in got], sent) == ([("old", 0)], sent_here)
        held = await settle(Row.from_id(1, db=writer))
        held.name = "written"
        await settle(held.save())  # the writer adds the column
        got, sent = await _sent(lambda: Row.using(reader).all())
        assert ([(g.name, g._version) for g in got], sent) == ([("written", 1)], [old, look, new])  # refused, learned
        _, sent = await _sent(lambda: Row.using(reader).all())
        assert sent == [new]
    finally:
        await close(reader, writer)


class _Undone(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_what_a_rolled_back_transaction_learned_of_the_column_is_asked_again(base, tmp_path):
    path = str(tmp_path / "v.db")
    Row = await _with_version_column(base, path)
    t = Row.__tablename__
    look, read = f'PRAGMA table_xinfo("{t}");', f"SELECT _id, data, _version FROM {t} WHERE {_g(t, '_id', 'data', '_version')}"
    db = await open_db(base, path)
    try:
        with pytest.raises(_Undone):
            if base in ASYNC_BASES:
                async with db.transaction():
                    assert (await _sent(lambda: Row.using(db).all()))[1] == [look, read]
                    raise _Undone
            else:
                with db.transaction():
                    assert (await _sent(lambda: Row.using(db).all()))[1] == [look, read]
                    raise _Undone
        assert (await _sent(lambda: Row.using(db).all()))[1] == [look, read]
        assert (await _sent(lambda: Row.using(db).all()))[1] == [read]
    finally:
        await close(db)


# ----- a set-based update returns instances with their home and the row's version ---------


UPDATES = {
    "update_one": lambda q: q.filter(F("_id") == 1).update_one(name="u"),
    "update_n": lambda q: q.filter(F("_id") == 1).update_n(1, name="u"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["query()", "using(db)"])
@pytest.mark.parametrize("way", list(UPDATES))
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_an_instance_a_set_update_returns_holds_its_home_and_version_and_saves(base, way, door):
    db = await open_db(base)
    try:
        Row = row_class(base)
        bind(Row, db)
        await settle(Row(name="o").save())
        got = await settle(UPDATES[way](Row.query() if door == "query()" else Row.using(db)))
        got = got[0] if isinstance(got, list) else got
        assert (got._id, got._version, vars(got)["_home"].db is db) == (1, 1, True)
        got.name = "saved"
        await settle(got.save())
        doc = await settle(db.find_document_with_version(Row.__tablename__, 1))
        assert (doc["name"], doc["_version"], got._version) == ("saved", 2, 2)
        fresh = await settle(Row.from_id(1))
        fresh.name = "again"
        await settle(fresh.save())
        assert (await settle(db.find_document_with_version(Row.__tablename__, 1)))["_version"] == 3
    finally:
        await close(db)


# ----- a save that fails after the home was set leaves the home as it was -----------------


def _audited(base):
    return row_class(base, (AsyncAuditLogMixin if base in ASYNC_BASES else AuditLogMixin,))


FAILURES = ["an audit row that raises", "a commit that fails"]


def _break(path: str, table: str, failure: str) -> None:
    """Make the next audit row of ``table`` at ``path`` fail as ``failure`` says, with a real
    trigger: refused at once, or (a deferred foreign key) at the commit."""
    if failure == "an audit row that raises":
        _sqlite(
            path,
            f"CREATE TRIGGER no_audit BEFORE INSERT ON {table}_audit BEGIN SELECT RAISE(ABORT, 'refused'); END",
        )
    else:
        _sqlite(
            path,
            "CREATE TABLE gone (id INTEGER PRIMARY KEY)",
            "CREATE TABLE dangling (ref INTEGER REFERENCES gone(id) DEFERRABLE INITIALLY DEFERRED)",
            f"CREATE TRIGGER dangle AFTER INSERT ON {table}_audit BEGIN INSERT INTO dangling VALUES (7); END",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_save_elsewhere_that_fails_after_the_write_leaves_the_home(base, failure, tmp_path):
    home_path, other_path = str(tmp_path / "home.db"), str(tmp_path / "other.db")
    Row = _audited(base)
    home, other = await open_db(base, home_path), await open_db(base, other_path)
    try:
        m = await settle(Row(name="mine").save(db=home))
        await settle(Row(name="theirs").save(db=other))
        _break(other_path, Row.__tablename__, failure)
        m.name = "refused"
        with pytest.raises(sqlite3.IntegrityError):
            await settle(m.save(db=other))
        assert await name_of(other, Row) == "theirs"
        m.name = "saved"
        await settle(m.save())
        assert (await name_of(home, Row), await name_of(other, Row)) == ("saved", "theirs")
    finally:
        await close(home, other)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_save_many_elsewhere_that_fails_after_the_write_leaves_no_home(base, failure, tmp_path):
    home_path, other_path = str(tmp_path / "home.db"), str(tmp_path / "other.db")
    Row = _audited(base)
    home, other = await open_db(base, home_path), await open_db(base, other_path)
    try:
        bind(Row, home)
        await settle(Row(name="theirs").save(db=other))
        _break(other_path, Row.__tablename__, failure)
        batch = [Row(name="a"), Row(name="b")]
        with pytest.raises(sqlite3.IntegrityError):
            await settle(Row.save_many(batch, db=other))
        assert [vars(m).get("_home") for m in batch] == [None, None]
        await settle(batch[0].save())  # no home: the class's database
        assert [await name_of(home, Row, i) for i in (1, 2)] == ["a", None]
        assert [await name_of(other, Row, i) for i in (1, 2)] == ["theirs", None]
    finally:
        await close(home, other)


def _cancel_at_the_audit_row(task_box: list, table: str):
    """A query-log callback that cancels the task in ``task_box`` when its audit row of
    ``table`` is written: after the row itself, so after the home was set."""

    def callback(entry) -> None:
        if entry.sql.lstrip().startswith(f"INSERT INTO {table}_audit") and task_box:
            task_box.pop().cancel()

    return callback


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ASYNC_BASES, ids=lambda b: b.__name__)
async def test_a_save_elsewhere_cancelled_after_the_write_leaves_the_home(base, tmp_path):
    Row = _audited(base)
    home, other = await open_db(base, tmp_path / "home.db"), await open_db(base, tmp_path / "other.db")
    callback = _cancel_at_the_audit_row([], Row.__tablename__)
    try:
        m = await Row(name="mine").save(db=home)
        await Row(name="theirs").save(db=other)
        m.name = "cancelled"
        box: list = []
        callback = _cancel_at_the_audit_row(box, Row.__tablename__)
        query_logger.add_callback(callback)
        with query_logger.capture():
            task = asyncio.ensure_future(m.save(db=other))
            box.append(task)
            with pytest.raises(asyncio.CancelledError):
                await task
        query_logger.remove_callback(callback)
        assert await name_of(other, Row) == "theirs"
        m.name = "saved"
        await m.save()
        assert (await name_of(home, Row), await name_of(other, Row)) == ("saved", "theirs")
    finally:
        query_logger.remove_callback(callback)
        await close(home, other)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ASYNC_BASES, ids=lambda b: b.__name__)
async def test_a_save_many_elsewhere_cancelled_after_the_write_leaves_no_home(base, tmp_path):
    Row = _audited(base)
    home, other = await open_db(base, tmp_path / "home.db"), await open_db(base, tmp_path / "other.db")
    box: list = []
    callback = _cancel_at_the_audit_row(box, Row.__tablename__)
    try:
        bind(Row, home)
        await Row(name="theirs").save(db=other)
        batch = [Row(name="a"), Row(name="b")]
        query_logger.add_callback(callback)
        with query_logger.capture():
            task = asyncio.ensure_future(Row.save_many(batch, db=other))
            box.append(task)
            with pytest.raises(asyncio.CancelledError):
                await task
        query_logger.remove_callback(callback)
        assert [vars(m).get("_home") for m in batch] == [None, None]
        await batch[0].save()
        assert [await name_of(home, Row, i) for i in (1, 2)] == ["a", None]
        assert [await name_of(other, Row, i) for i in (1, 2)] == ["theirs", None]
    finally:
        query_logger.remove_callback(callback)
        await close(home, other)
