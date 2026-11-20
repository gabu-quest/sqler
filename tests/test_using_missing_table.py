"""A query through a model on a table nobody created reads as empty, and creates nothing.

``using(db)``, ``using(db, table)`` and the async bound ``query()`` run no statement
that creates a table: on a missing table ``count()`` is 0, ``all()`` is empty,
``first()`` is ``None`` and the rest give what they give on an empty table, on all
five bases; afterwards the table is still missing. A read never waits for the write
lock (a connection that read the schema before the table existed needs it for
``CREATE TABLE IF NOT EXISTS``, not for a ``SELECT``).
"""

import asyncio
import sqlite3
import warnings
from dataclasses import dataclass

import pytest
import pytest_asyncio

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerModel,
    AsyncSQLerMsgspecModel,
    SQLerDB,
    SQLerLiteModel,
    SQLerModel,
    SQLerMsgspecModel,
)
from sqler.adapter import AsyncSQLiteAdapter
from sqler.query import F, SQLerExpression


class MtPy(SQLerModel):
    name: str = ""


@dataclass
class MtLite(SQLerLiteModel):
    name: str = ""


class MtMs(SQLerMsgspecModel):
    name: str = ""


class MtAs(AsyncSQLerModel):
    name: str = ""


@dataclass
class MtALite(AsyncSQLerLiteModel):
    name: str = ""


class MtAMs(AsyncSQLerMsgspecModel):
    name: str = ""


class MtPyPromoted(SQLerModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


class MtAsPromoted(AsyncSQLerModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


SYNC = [MtPy, MtLite, MtMs, MtPyPromoted]
SYNC_IDS = ["pydantic", "lite", "msgspec", "pydantic-promoted"]
ASYNC = [MtAs, MtALite, MtAMs, MtAsPromoted]
ASYNC_IDS = ["async-pydantic", "async-lite", "async-msgspec", "async-pydantic-promoted"]


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    try:
        yield d
    finally:
        d.close()


@pytest_asyncio.fixture
async def adb():
    d = AsyncSQLerDB.in_memory(shared=False)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


def _bound_subclass(base, db):
    sub = type(
        f"{base.__name__}Bound", (base,), {"__tablename__": f"{base.__name__.lower()}_bound"}
    )
    if hasattr(base, "__dataclass_fields__"):
        sub = dataclass(sub)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        sub.set_db(db)
    return sub


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_using_on_a_table_nobody_created_is_empty(db, cls):
    assert cls.using(db).count() == 0
    assert cls.using(db).all() == []
    assert cls.using(db).first() is None


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_using_after_the_only_save_was_rolled_back_is_empty(db, cls):
    with pytest.raises(RuntimeError, match="undo"):
        with db.transaction():
            cls(name="gone").save(db=db)
            raise RuntimeError("undo")
    assert cls.using(db).count() == 0
    assert cls.using(db).first() is None


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_bound_and_using_routes_agree_on_an_empty_table(db, cls):
    bound = _bound_subclass(cls, db)
    assert (bound.query().count(), bound.query().all(), bound.query().first()) == (0, [], None)
    assert (bound.using(db).count(), bound.using(db).all(), bound.using(db).first()) == (
        0,
        [],
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_using_on_a_table_nobody_created_is_empty(adb, cls):
    assert await cls.using(adb).count() == 0
    assert await cls.using(adb).all() == []
    assert await cls.using(adb).first() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_bound_and_using_routes_agree_on_an_empty_table(adb, cls):
    bound = _bound_subclass(cls, adb)
    assert await bound.query().count() == 0
    assert await bound.query().all() == []
    assert await bound.query().first() is None
    assert await bound.count() == 0
    assert await bound.using(adb).count() == 0
    assert await bound.using(adb).all() == []
    assert await bound.using(adb).first() is None


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_using_named_table_nobody_created_is_empty(db, cls):
    assert cls.using(db, "mt_never").count() == 0
    assert cls.using(db, "mt_never").all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_using_named_table_nobody_created_is_empty(adb, cls):
    assert await cls.using(adb, "mt_never").count() == 0
    assert await cls.using(adb, "mt_never").first() is None


@dataclass
class MtLitePromoted(SQLerLiteModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


class MtMsPromoted(SQLerMsgspecModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


@pytest.mark.parametrize(
    "cls", [MtPyPromoted, MtLitePromoted, MtMsPromoted], ids=["pydantic", "lite", "msgspec"]
)
def test_promoted_save_with_db_creates_its_table_on_a_fresh_database(db, cls):
    """The bound route creates a promoted table at set_db(); save(db=db) must too."""
    row = cls(name="first", status="open").save(db=db)
    table = cls._resolve_binding(db)[1]
    cols = [r[1] for r in db.adapter.execute(f'PRAGMA table_info("{table}");').fetchall()]
    assert cols == ["_id", "data", "status"]
    assert (
        db.adapter.execute(f"SELECT status FROM {table} WHERE _id = ?", [row._id]).fetchone()[0]
        == "open"
    )


# ----- a read creates nothing: every queryset method on a missing table ------------


def _table_exists(db, table):
    cur = db.adapter.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table])
    return cur.fetchone() is not None


async def _atable_exists(db, table):
    cur = await db.adapter.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table]
    )
    try:
        row = await cur.fetchone()
    finally:
        await cur.close()
    await db.adapter.auto_commit()
    return row is not None


def _sync_reads(qs_factory):
    """Every read and bulk write of a queryset, with what an empty table gives."""
    page = qs_factory().paginate(1, 5)
    return {
        "all": qs_factory().all(),
        "first": qs_factory().first(),
        "count": qs_factory().count(),
        "exists": qs_factory().exists(),
        "sum": qs_factory().sum("n"),
        "avg": qs_factory().avg("n"),
        "min": qs_factory().min("n"),
        "max": qs_factory().max("n"),
        "grouped_count": qs_factory().group_by("name").count(),
        "distinct_values": qs_factory().distinct_values("name"),
        "as_dicts": qs_factory().as_dicts(),
        "iter": list(qs_factory().iter()),
        "page": (page.items, page.total, page.total_pages, page.page, page.per_page),
        "filtered_count": qs_factory().filter(F("name") == "x").count(),
        "update": qs_factory().update(name="y"),
        "update_one": qs_factory().update_one(name="y"),
        "update_n": qs_factory().update_n(2, name="y"),
        "delete_all": qs_factory().delete_all(),
    }


async def _async_reads(qs_factory):
    page = await qs_factory().paginate(1, 5)
    return {
        "all": await qs_factory().all(),
        "first": await qs_factory().first(),
        "count": await qs_factory().count(),
        "exists": await qs_factory().exists(),
        "sum": await qs_factory().sum("n"),
        "avg": await qs_factory().avg("n"),
        "min": await qs_factory().min("n"),
        "max": await qs_factory().max("n"),
        "grouped_count": await qs_factory().group_by("name").count(),
        "distinct_values": await qs_factory().distinct_values("name"),
        "as_dicts": await qs_factory().as_dicts(),
        "page": (page.items, page.total, page.total_pages, page.page, page.per_page),
        "filtered_count": await qs_factory().filter(F("name") == "x").count(),
        "update": await qs_factory().update(name="y"),
        "update_one": await qs_factory().update_one(name="y"),
        "update_n": await qs_factory().update_n(2, name="y"),
        "delete_all": await qs_factory().delete_all(),
    }


EMPTY_SYNC = {
    "all": [],
    "first": None,
    "count": 0,
    "exists": False,
    "sum": None,
    "avg": None,
    "min": None,
    "max": None,
    "grouped_count": [],
    "distinct_values": [],
    "as_dicts": [],
    "iter": [],
    "page": ([], 0, 0, 1, 5),
    "filtered_count": 0,
    "update": 0,
    "update_one": None,
    "update_n": [],
    "delete_all": 0,
}
EMPTY_ASYNC = {k: v for k, v in EMPTY_SYNC.items() if k != "iter"}


@pytest.mark.parametrize("named", [False, True], ids=["class-table", "named-table"])
@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_reads_on_a_missing_table_are_empty_and_create_nothing(db, cls, named):
    table = "mt_absent" if named else cls._resolve_binding(db)[1]

    def qs():
        return cls.using(db, "mt_absent") if named else cls.using(db)

    assert _sync_reads(qs) == EMPTY_SYNC
    assert _table_exists(db, table) is False


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_reads_on_an_existing_empty_table_give_the_same(db, cls):
    """The control: what the missing-table answers are measured against."""
    db.ensure_table("mt_present", promoted=getattr(cls, "__promoted__", None))
    assert _sync_reads(lambda: cls.using(db, "mt_present")) == EMPTY_SYNC


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["using", "using-named", "bound-query"])
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_reads_on_a_missing_table_are_empty_and_create_nothing(adb, cls, route):
    if route == "bound-query":
        bound = _bound_subclass(cls, adb)
        table = bound._table

        def qs():
            return bound.query()
    elif route == "using-named":
        table = "mt_absent"

        def qs():
            return cls.using(adb, "mt_absent")
    else:
        table = cls._resolve_binding(adb)[1]

        def qs():
            return cls.using(adb)

    assert await _async_reads(qs) == EMPTY_ASYNC
    assert await _atable_exists(adb, table) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_bound_count_and_from_id_on_a_missing_table_create_nothing(adb, cls):
    bound = _bound_subclass(cls, adb)
    assert await bound.count() == 0
    assert await bound.from_id(1) is None
    assert await bound.from_ids([1, 2]) == []
    assert await _atable_exists(adb, bound._table) is False


@pytest.mark.parametrize("cls", [MtPy, MtLite, MtMs], ids=["pydantic", "lite", "msgspec"])
def test_from_ids_with_a_database_on_a_missing_table_creates_nothing(db, cls):
    assert cls.from_ids([1], db=db) == []
    assert _table_exists(db, cls._resolve_binding(db)[1]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_refresh_after_the_table_was_dropped_is_a_missing_row(adb, cls):
    bound = _bound_subclass(cls, adb)
    row = await bound(name="gone").save()
    await adb.adapter.execute(f"DROP TABLE {bound._table}")
    await adb.adapter.auto_commit()
    with pytest.raises(LookupError, match=f"Row {row._id} not found"):
        await row.refresh()
    assert await _atable_exists(adb, bound._table) is False


@pytest.mark.parametrize("cls", [MtPy, MtLite, MtMs], ids=["pydantic", "lite", "msgspec"])
def test_refresh_after_the_table_was_dropped_is_a_missing_row(db, cls):
    bound = _bound_subclass(cls, db)
    row = bound(name="gone").save()
    db.adapter.execute(f"DROP TABLE {bound._table}")
    with pytest.raises(LookupError, match=f"Row {row._id} not found"):
        row.refresh()
    assert bound.from_id(row._id) is None
    assert _table_exists(db, bound._table) is False


# ----- what still raises -------------------------------------------------------------


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_a_filter_on_a_missing_other_table_still_raises(db, cls):
    cls(name="here").save(db=db)
    joined = SQLerExpression("EXISTS (SELECT 1 FROM mt_other_missing)")
    with pytest.raises(sqlite3.OperationalError, match="no such table: mt_other_missing"):
        cls.using(db).filter(joined).all()
    with pytest.raises(sqlite3.OperationalError, match="no such table: mt_other_missing"):
        cls.using(db).filter(joined).count()


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_filter_on_a_missing_other_table_still_raises(adb, cls):
    await cls(name="here").save(db=adb)
    joined = SQLerExpression("EXISTS (SELECT 1 FROM mt_other_missing)")
    with pytest.raises(sqlite3.OperationalError, match="no such table: mt_other_missing"):
        await cls.using(adb).filter(joined).all()
    with pytest.raises(sqlite3.OperationalError, match="no such table: mt_other_missing"):
        await cls.using(adb).filter(joined).count()


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_a_missing_column_still_raises_on_an_existing_table(db, cls):
    cls(name="here").save(db=db)
    bad = SQLerExpression("mt_no_such_column = ?", [1])
    with pytest.raises(sqlite3.OperationalError, match="no such column: mt_no_such_column"):
        cls.using(db).filter(bad).count()


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_missing_table_wins_over_a_missing_column(db, cls):
    bad = SQLerExpression("mt_no_such_column = ?", [1])
    assert cls.using(db, "mt_absent").filter(bad).all() == []
    assert cls.using(db, "mt_absent").filter(bad).count() == 0


# ----- writes still create the table ---------------------------------------------------


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_empty_named_read_then_save_of_a_new_row_creates_the_class_table(db, cls):
    """using(db, "archive") on a missing archive is empty; a new row saved after it goes
    to the class's own table (a new row was read from nowhere), which the write creates."""
    assert cls.using(db, "mt_archive").first() is None
    row = cls(name="fresh").save(db=db)
    assert [r.name for r in cls.using(db).all()] == ["fresh"]
    assert row._id == 1
    assert _table_exists(db, "mt_archive") is False


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_first_or_create_on_a_missing_table_inserts(db, cls):
    assert cls.using(db).count() == 0
    row, created = cls.first_or_create({"name": "a"}, db=db)
    assert created is True
    assert [r.name for r in cls.using(db).all()] == ["a"]
    assert row._id == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_first_or_create_on_a_missing_table_inserts(adb, cls):
    assert await cls.using(adb).count() == 0
    row, created = await cls.first_or_create({"name": "a"}, db=adb)
    assert created is True
    assert [r.name for r in await cls.using(adb).all()] == ["a"]


# ----- a read never waits for the write lock -------------------------------------------


class LkAsDoc(AsyncSQLerModel):
    n: int = 0


@dataclass
class LkALiteDoc(AsyncSQLerLiteModel):
    n: int = 0


async def _stale_pool_with_locked_writer(tmp_path, cls):
    """A pool of two whose connections both read the schema before the table existed,
    then a table with one committed row, and another connection holding the write lock."""
    path = str(tmp_path / "lock.db")
    adapter = AsyncSQLiteAdapter(path, pool_size=2)
    db = AsyncSQLerDB(adapter)
    await db.connect()
    assert len(adapter._all_conns) == 2
    for conn in adapter._all_conns:
        cur = await conn.execute("SELECT count(*) FROM sqlite_master")
        assert (await cur.fetchone())[0] == 0
        await cur.close()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)
    table = cls._table
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute(
        f"CREATE TABLE {table} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)"
    )
    writer.execute(f"INSERT INTO {table} (data) VALUES (json('{{\"n\": 1}}'))")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(f"INSERT INTO {table} (data) VALUES (json('{{\"n\": 2}}'))")
    return db, writer


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [LkAsDoc, LkALiteDoc], ids=["async-pydantic", "async-lite"])
async def test_reads_return_at_once_while_another_connection_holds_the_write_lock(tmp_path, cls):
    db, writer = await _stale_pool_with_locked_writer(tmp_path, cls)
    try:
        rows = await asyncio.wait_for(cls.using(db).all(), timeout=1.0)
        count = await asyncio.wait_for(cls.query().count(), timeout=1.0)
        row = await asyncio.wait_for(cls.from_id(1), timeout=1.0)
        bound_count = await asyncio.wait_for(cls.count(), timeout=1.0)
        assert [r.n for r in rows] == [1]
        assert count == 1
        assert row.n == 1
        assert bound_count == 1
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        await db.close()


# ----- async bound query() runs the statements it ran before -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [LkAsDoc, LkALiteDoc], ids=["async-pydantic", "async-lite"])
async def test_async_bound_query_runs_only_its_select_when_the_table_exists(tmp_path, cls):
    adapter = AsyncSQLiteAdapter(str(tmp_path / "trace.db"), pool_size=2)
    db = AsyncSQLerDB(adapter)
    await db.connect()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(db)
        await cls(n=1).save()
        traced: list[str] = []
        for conn in adapter._all_conns:
            await conn.set_trace_callback(traced.append)

        first = await cls.query().first()
        count = await cls.query().count()
        everything = await cls.query().all()

        table = cls._table
        # each carries the guard of the columns its database remembers, a branch that never runs
        guard = f"WHERE CASE WHEN 1 THEN 1 ELSE (SELECT (`_id`, `data`) IN (SELECT * FROM {table}) FROM {table}) END"
        assert (first.n, count, [r.n for r in everything]) == (1, 1, [1])
        assert traced == [
            f"SELECT _id, data FROM {table} {guard} LIMIT 1",
            f"SELECT COUNT(*) FROM {table} {guard}",
            f"SELECT _id, data FROM {table} {guard}",
        ]
    finally:
        for conn in adapter._all_conns:
            await conn.set_trace_callback(None)
        await db.close()


# --- a read of a missing table inside the caller's transaction -------------------


class MtTxnKept(SQLerModel):
    __tablename__ = "mt_txn_kept"
    n: int = 0


class MtTxnAbsent(SQLerModel):
    __tablename__ = "mt_txn_absent"
    n: int = 0


class MtAsTxnKept(AsyncSQLerModel):
    __tablename__ = "mt_as_txn_kept"
    n: int = 0


class MtAsTxnAbsent(AsyncSQLerModel):
    __tablename__ = "mt_as_txn_absent"
    n: int = 0


def test_missing_table_read_inside_a_transaction_keeps_the_callers_writes(db):
    with db.transaction():
        MtTxnKept(n=1).save(db=db)
        assert MtTxnAbsent.using(db).count() == 0
        assert MtTxnAbsent.using(db).all() == []
        assert MtTxnAbsent.using(db).update(n=5) == 0
        MtTxnKept(n=2).save(db=db)

    assert sorted(r.n for r in MtTxnKept.using(db).all()) == [1, 2]
    assert _table_exists(db, "mt_txn_absent") is False


@pytest.mark.asyncio
async def test_async_missing_table_read_inside_a_transaction_keeps_the_callers_writes(adb):
    async with adb.transaction():
        await MtAsTxnKept(n=1).save(db=adb)
        assert await MtAsTxnAbsent.using(adb).count() == 0
        assert await MtAsTxnAbsent.using(adb).all() == []
        assert await MtAsTxnAbsent.using(adb).first() is None
        await MtAsTxnKept(n=2).save(db=adb)

    assert sorted(r.n for r in await MtAsTxnKept.using(adb).all()) == [1, 2]
    assert await _atable_exists(adb, "mt_as_txn_absent") is False
