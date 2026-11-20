"""The async twin of ``tests/sync/test_streaming.py``: ``AsyncSQLerQuery.iter_dicts()`` and
``AsyncSQLerQuerySet.iter()`` give their rows one at a time.

They give what ``all_dicts()`` and the query set's loader give for the same statement, each
row once with the values its statement saw, also when the loop writes. Outside a
transaction the statement holds one pooled connection until the iteration ends, and that
hold ends through the statement's own lease whichever way the iteration ends, never through
the pin of the context that runs the end.

Every wait is on an event, a task or a bounded ``wait_for``.
"""

import asyncio
import contextlib
import gc
import inspect
import itertools
import json
import sqlite3
import threading
import types
import warnings
from dataclasses import dataclass

import aiosqlite
import pytest
from pairwise import all_pairs

from sqler import AsyncSQLerDB, AsyncSQLerModel, AsyncSQLerSafeModel, F, SQLerDB
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.exceptions import NoAdapterError, NotConnectedError
from sqler.logging import query_logger
from sqler.models import AsyncSQLerLiteModel, AsyncSQLerLiteSafeModel, SQLerLiteModelBase
from sqler.query import SQLerExpression
from sqler.query.async_query import AsyncSQLerQuery

pytestmark = pytest.mark.asyncio

ROWS = 150  # more than two of the chunks (64 rows) the cursor's iteration reads at a time
GATE_AT = 100  # a row of the second chunk

BASES = {
    "pydantic": AsyncSQLerModel,
    "pydantic-safe": AsyncSQLerSafeModel,
    "lite": AsyncSQLerLiteModel,
    "lite-safe": AsyncSQLerLiteSafeModel,
}
DATABASES = ["file, pool of 4", "file, pool of 1", "memory"]
_tables = itertools.count()


class Gate:
    """The SQL function ``gate(_id)``, true for every row; armed, the step of the statement
    that reaches row ``GATE_AT`` waits in the connection's thread until ``open`` is set."""

    def __init__(self):
        self.armed = False
        self.entered, self.open = threading.Event(), threading.Event()

    def __call__(self, rid):
        if self.armed and rid == GATE_AT:
            self.entered.set()
            self.open.wait(10)
        return 1


async def _database(kind: str, tmp_path, gate: Gate = None) -> AsyncSQLerDB:
    if kind == "memory":
        db = AsyncSQLerDB.in_memory(shared=False, acquire_timeout=10)
    else:
        size = 4 if kind.endswith("4") else 1
        adapter = AsyncSQLiteAdapter(
            str(tmp_path / f"s{next(_tables)}.db"),
            pragmas=["PRAGMA journal_mode = WAL"],
            pool_size=size,
            acquire_timeout=10,
        )
        db = AsyncSQLerDB(adapter)
    await db.connect()
    for conn in db.adapter._all_conns:
        await conn.create_function("gate", 1, gate or Gate())
    return db


def declare(base):
    """A class of ``base`` with one field ``n``, on a table of its own."""
    table = f"st_{next(_tables)}"
    ns = {"__module__": __name__, "__tablename__": table, "__annotations__": {"n": int}, "n": 0}
    cls = types.new_class(f"Row_{table}", (base,), {}, lambda body: body.update(ns))
    return dataclass(cls) if issubclass(base, SQLerLiteModelBase) else cls


async def _seeded(db, base=AsyncSQLerModel, rows=ROWS):
    cls = declare(base)
    await cls.save_many([cls(n=i) for i in range(rows)], db=db)
    return cls


def _free(db) -> bool:
    adapter = db.adapter
    return adapter._pool.qsize() == adapter._pool_size


async def _settle(db) -> None:
    for _ in range(10):  # turns of the loop: one closes an iterator that was dropped
        await asyncio.sleep(0)
    await asyncio.gather(*db.adapter._reclaims)  # a connection rolled back in the background


def _committed(db, table) -> int:
    conn = sqlite3.connect(db.adapter.path)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _seen(models) -> list:
    return [
        (
            type(m),
            m._id,
            m.n,
            getattr(m, "_version", None),
            m.__dict__["_home"].db,
            m.__dict__["_home"].table,
        )
        for m in models
    ]


# --- the ways an iteration ends ------------------------------------------------------------


async def _ran_out(qs, gate):
    got = [m async for m in qs.iter()]
    return got, _free(qs._db)


async def _closed(qs, gate):
    it = qs.iter()
    got = [await it.__anext__()]
    await it.aclose()
    return got, _free(qs._db)


async def _raised(qs, gate):
    got = []
    with pytest.raises(LookupError):
        async for m in qs.iter():
            got.append(m)
            if len(got) == 3:
                raise LookupError
    return got, None


async def _dropped(qs, gate):
    it = qs.iter()
    got = [await it.__anext__()]
    del it
    gc.collect()
    return got, None


async def _cancelled_in_body(qs, gate):
    got, given = [], asyncio.Event()

    async def consume():
        async for m in qs.iter():
            got.append(m)
            given.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(given.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return got, None


async def _cancelled_in_fetch(qs, gate):
    got = []

    async def consume():
        async for m in qs.iter():
            got.append(m)

    gate.armed = True
    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(gate.entered.wait, 5)
        task.cancel()
    finally:
        gate.open.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    return got, None


WAYS = {
    "rows run out": (_ran_out, ROWS),
    "aclose()": (_closed, 1),
    "body raises": (_raised, 3),
    "dropped": (_dropped, 1),
    "cancelled in its body": (_cancelled_in_body, 1),
    "cancelled in a fetch": (_cancelled_in_fetch, 64),
}


@pytest.mark.parametrize(
    *all_pairs(("base", list(BASES)), ("way", list(WAYS)), ("kind", DATABASES))
)
async def test_an_iteration_gives_the_loaders_models_and_its_end_leaves_the_pool_whole(
    base, way, kind, tmp_path, caplog
):
    gate = Gate()
    db = await _database(kind, tmp_path, gate)
    try:
        cls = await _seeded(db, BASES[base])
        qs = cls.using(db).filter(
            SQLerExpression("gate(_id)")
        )  # rows in rowid order, stepped as fetched
        expected = qs._models(await qs.as_dicts())
        end, given = WAYS[way]
        gc.collect()  # what earlier tests left to collect warns here, not in the iteration's own block
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            got, at_once = await end(qs, gate)
            await _settle(db)
            gc.collect()
            whole = _free(db)
            # the next statement, from a task that must get a connection of the pool
            count = await asyncio.wait_for(asyncio.create_task(qs.count()), 5)
    finally:
        await db.close()
    assert (len(got), got == expected[: len(got)], _seen(got)) == (
        given,
        True,
        _seen(expected[:given]),
    )
    assert (at_once, whole, count) == (
        True if way in ("rows run out", "aclose()") else None,
        True,
        ROWS,
    )
    leaks = [w for w in warned if issubclass(w.category, (RuntimeWarning, ResourceWarning))]
    assert [(w.category.__name__, str(w.message)) for w in leaks] == []
    assert [r.getMessage() for r in caplog.records if r.name == "asyncio"] == []


# --- iter_dicts() gives what all_dicts() gives ------------------------------------------

SHAPES = {
    "no rows": (0, lambda q: q),
    "filter, order and limit": (ROWS, lambda q: q.filter(F("n") >= 10).order_by("-n").limit(70)),
    "select of two fields": (ROWS, lambda q: q.select("n", "m")),
    "5,000 rows": (5000, lambda q: q),
}


@pytest.mark.parametrize("shape", list(SHAPES))
async def test_iter_dicts_gives_what_all_dicts_gives_one_at_a_time_with_the_same_statement(
    shape, tmp_path
):
    rows, made = SHAPES[shape]
    db = await _database("file, pool of 4", tmp_path)
    try:
        await db.ensure_table("docs")
        if rows:
            await db.insert_many("docs", [{"n": i, "m": -i, "x": "y"} for i in range(rows)])
        q = made(await db.query("docs"))
        eager = await q.all_dicts()
        it = q.iter_dicts()
        with query_logger.capture() as sent:
            streamed = [d async for d in it]
        with query_logger.capture() as sent_eager:
            await q.all_dicts()
        whole = _free(db)
    finally:
        await db.close()
    assert (inspect.isasyncgen(it), streamed, [s.sql for s in sent], whole) == (
        True,
        eager,
        [s.sql for s in sent_eager],
        True,
    )
    if shape == "5,000 rows":
        assert (len(streamed), streamed[0]["_id"], streamed[-1]["_id"]) == (5000, 1, 5000)


async def test_iter_dicts_without_an_adapter_raises_at_its_first_step():
    it = AsyncSQLerQuery(table="docs").iter_dicts()
    with pytest.raises(NoAdapterError, match="^No adapter set for query$"):
        await it.__anext__()


# --- rows as the statement saw them --------------------------------------------------------


@pytest.mark.parametrize("kind", ["file, pool of 4", "memory"])
@pytest.mark.parametrize("unit", ["no transaction", "transaction"])
async def test_a_loop_that_saves_each_model_gets_each_row_once_as_its_statement_saw_it(
    kind, unit, tmp_path
):
    db = await _database(kind, tmp_path)
    try:
        cls = await _seeded(db, rows=2000)
        await db.create_index(cls.__tablename__, "n")  # a save moves its row on in the order walked
        seen = []
        async with db.transaction() if unit == "transaction" else contextlib.nullcontext():
            async for m in cls.using(db).order_by("n").iter():
                seen.append((m._id, m.n))
                if len(seen) > 2000:  # a walk that meets the rows it moved would not end
                    break
                m.n += 1000
                await m.save()
        stored = sorted((m._id, m.n) for m in await cls.using(db).all())
        whole = _free(db)
    finally:
        await db.close()
    assert seen == [(i, i - 1) for i in range(1, 2001)]
    assert (stored, whole) == ([(i, i + 999) for i in range(1, 2001)], True)


async def test_an_iteration_inside_a_transaction_sees_the_transactions_writes(tmp_path):
    db = await _database("file, pool of 4", tmp_path)
    try:
        cls = await _seeded(db, rows=3)
        async with db.transaction():
            await cls(n=99).save(db=db)
            got = [m.n async for m in cls.using(db).iter()]
        whole = _free(db)
    finally:
        await db.close()
    assert (got, whole) == ([0, 1, 2, 99], True)


@pytest.mark.parametrize("end", ["commit", "rollback"])
async def test_an_iterator_left_unfinished_when_its_transaction_ends_goes_on_giving_what_its_statement_saw(
    end, tmp_path
):
    db = await _database("file, pool of 4", tmp_path)
    try:
        cls = await _seeded(db)
        with contextlib.suppress(LookupError):
            async with db.transaction():
                await cls(n=-1).save(db=db)
                it = cls.using(db).iter()
                first = await it.__anext__()
                if end == "rollback":
                    raise LookupError
        await asyncio.wait_for(asyncio.create_task(cls.using(db).delete_all()), 5)
        rest = [m.n async for m in it]
        whole = _free(db)
    finally:
        await db.close()
    assert ([first.n] + rest, whole) == (list(range(ROWS)) + [-1], True)


# --- the hold of one statement and nobody else's ----------------------------------------


@pytest.mark.parametrize("tables", ["one table", "two tables"])
async def test_two_iterators_of_one_task_read_in_turn_each_give_their_rows(tables, tmp_path):
    db = await _database("file, pool of 1", tmp_path)
    try:
        one = await _seeded(db)
        two = one if tables == "one table" else await _seeded(db)
        a, b = one.using(db).iter(), two.using(db).order_by("-n").iter()
        got_a, got_b = [], []
        for _ in range(ROWS):
            got_a.append((await a.__anext__()).n)
            got_b.append((await b.__anext__()).n)
        after = (await anext(a, None), await anext(b, None))
        whole = _free(db)
    finally:
        await db.close()
    assert (got_a, got_b, after, whole) == (
        list(range(ROWS)),
        list(range(ROWS - 1, -1, -1)),
        (None, None),
        True,
    )


@pytest.mark.parametrize("end", ["rows run out", "dropped"])
async def test_an_iterator_another_task_ends_ends_its_own_statements_hold_and_not_that_tasks(
    end, tmp_path
):
    db = await _database("file, pool of 4", tmp_path)
    try:
        cls = await _seeded(db)
        await db.ensure_table("other")
        held, handed = [], asyncio.Event()

        async def other():  # started before this task pins a connection: it has a pin of its own
            await db.adapter.execute(
                "INSERT INTO other (data) VALUES ('{}')"
            )  # its own unit, not committed
            await handed.wait()
            rest = [m.n async for m in held[0]] if end == "rows run out" else held.clear()
            await _settle(db)
            during = (_committed(db, "other"), db.adapter.connection is not None)
            await db.adapter.auto_commit()
            return rest, during

        task = asyncio.create_task(other())
        held.append(cls.using(db).iter())
        first = await held[0].__anext__()
        handed.set()
        rest, during = await asyncio.wait_for(task, 5)
        whole = _free(db)
        committed = _committed(db, "other")
    finally:
        await db.close()
    assert (first.n, rest, during, committed, whole) == (
        0,
        list(range(1, ROWS)) if end == "rows run out" else None,
        (0, True),
        1,
        True,
    )


async def test_a_child_task_that_iterates_on_its_parents_pin_leaves_the_parents_unit_to_the_parent(
    tmp_path,
):
    db = await _database("file, pool of 1", tmp_path)
    try:
        cls = await _seeded(db)
        await db.ensure_table("other")
        await db.adapter.execute(
            "INSERT INTO other (data) VALUES ('{}')"
        )  # the parent's unit, not committed

        async def child():
            return [m.n async for m in cls.using(db).iter()]

        got = await asyncio.wait_for(asyncio.create_task(child()), 5)
        during = (_committed(db, "other"), db.adapter.connection is not None)
        await db.adapter.auto_commit()
        whole, committed = _free(db), _committed(db, "other")
    finally:
        await db.close()
    assert (got, during, committed, whole) == (list(range(ROWS)), (0, True), 1, True)


async def test_a_write_the_loop_sends_and_leaves_uncommitted_is_committed_by_its_own_commit(
    tmp_path,
):
    db = await _database("file, pool of 4", tmp_path)
    try:
        cls = await _seeded(db)
        await db.ensure_table("other")
        async for m in cls.using(db).iter():
            await db.adapter.execute("INSERT INTO other (data) VALUES ('{}')")  # not committed
            break
        await _settle(db)  # the iterator left by the break is closed
        await db.adapter.commit()
        committed = _committed(db, "other")
        await db.adapter.auto_commit()
        whole = _free(db)
    finally:
        await db.close()
    assert (committed, whole) == (1, True)


async def test_a_write_on_the_raw_connection_before_the_loop_stops_keeps_the_connection_until_its_commit(
    tmp_path,
):
    db = await _database("file, pool of 1", tmp_path)
    try:
        cls = await _seeded(db)
        await db.ensure_table("other")
        async for m in cls.using(db).iter():
            # behind the adapter, after the statement's last read, and not committed
            await db.adapter.connection.execute("INSERT INTO other (data) VALUES ('{}')")
            break
        await _settle(db)
        held, before = not _free(db), _committed(db, "other")
        await db.adapter.auto_commit()
        after, whole = _committed(db, "other"), _free(db)
    finally:
        await db.close()
    assert (held, before, after, whole) == (True, 0, 1, True)


@pytest.mark.parametrize("unit", ["no transaction", "transaction"])
async def test_a_write_after_an_iteration_a_bad_row_stopped_goes_through_after_another_connections_commit(
    unit, tmp_path
):
    db = await _database("file, pool of 1", tmp_path)
    other = sqlite3.connect(db.adapter.path)
    try:
        await db.ensure_table("docs")
        await db.insert_many("docs", [{"n": i} for i in range(ROWS)])
        other.execute("UPDATE docs SET data = '{not json' WHERE _id = 3")
        other.commit()
        given = []
        async with db.transaction() if unit == "transaction" else contextlib.nullcontext():
            # the error is kept, and with it the iterator's frames and the cursor they hold
            with pytest.raises(json.JSONDecodeError) as stopped:
                async for doc in (await db.query("docs")).iter_dicts():
                    given.append(doc["n"])
        await _settle(db)
        other.execute("INSERT INTO docs (data) VALUES ('{\"n\": -1}')")
        other.commit()
        await db.insert_document("docs", {"n": -2})
        committed, whole = _committed(db, "docs"), _free(db)
    finally:
        other.close()
        await db.close()
    assert (given, stopped.type is not None, committed, whole) == ([0, 1], True, ROWS + 2, True)


async def test_rows_another_task_deletes_after_the_first_row_are_still_given(tmp_path):
    db = await _database("file, pool of 4", tmp_path)
    try:
        cls = await _seeded(db)
        it = cls.using(db).iter()
        first = await it.__anext__()
        await asyncio.wait_for(asyncio.create_task(cls.using(db).delete_all()), 5)
        rest = [m._id async for m in it]
        left, whole = await cls.using(db).count(), _free(db)
    finally:
        await db.close()
    assert ([first._id] + rest, left, whole) == (list(range(1, ROWS + 1)), 0, True)


async def test_closing_the_database_after_the_first_row_ends_the_iteration_with_not_connected(
    tmp_path,
):
    db = await _database("file, pool of 4", tmp_path)
    cls = await _seeded(db)
    it = cls.using(db).iter()
    await it.__anext__()
    await asyncio.wait_for(db.close(), 5)
    with pytest.raises(NotConnectedError):
        await asyncio.wait_for(it.__anext__(), 5)
    assert await anext(it, None) is None


class Doc(AsyncSQLerModel):
    __tablename__ = "promoted_later"
    status: str = ""


async def test_a_statement_sent_again_after_another_object_changed_the_table_gives_each_row_once(
    tmp_path,
):
    db = await _database("file, pool of 4", tmp_path)
    try:
        await Doc.save_many([Doc(status=f"s{i}") for i in range(3)], db=db)
        qs = Doc.using(db)
        await qs.all()  # the database object learns the table
        other = SQLerDB.on_disk(db.adapter.path)
        other.ensure_table(
            Doc.__tablename__, promoted={"status": "TEXT"}
        )  # moves every status into a column
        other.close()
        with query_logger.capture() as sent:
            got = [(m._id, m.status) async for m in qs.iter()]
        refused = [s.error is not None for s in sent if s.sql.startswith("SELECT _id")]
    finally:
        await db.close()
    assert (got, refused) == ([(1, "s0"), (2, "s1"), (3, "s2")], [True, False])


# --- cost -------------------------------------------------------------------------------


@pytest.fixture
def trips(monkeypatch):
    """The name of each call queued on a connection's thread, in the order queued."""
    names: list = []
    real = aiosqlite.Connection._execute

    async def counted(self, fn, *args, **kwargs):
        names.append(fn.__name__)
        return await real(self, fn, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "_execute", counted)
    return names


async def test_all_all_dicts_and_first_send_and_await_what_they_did_and_iter_reads_only(
    tmp_path, trips
):
    db = await _database("file, pool of 4", tmp_path)
    try:
        cls = await _seeded(db, rows=3)
        qs, q = cls.using(db), await db.query(cls.__tablename__)
        await qs.all()
        trips.clear()
        with query_logger.capture() as sent:
            await qs.all()
            await q.all_dicts()
            await qs.first()
        eager = list(trips)
        trips.clear()
        with query_logger.capture() as streamed:
            got = [m.n async for m in qs.iter()]
        lazy = list(trips)
    finally:
        await db.close()
    assert ([s.sql.split()[0] for s in sent], eager) == (
        ["SELECT"] * 3,
        ["execute", "fetch", "close", "commit"] * 3,
    )
    # the statement and its reads, as aiosqlite's own iteration makes them (the last finds no row)
    assert (got, [s.sql.split()[0] for s in streamed], lazy) == (
        [0, 1, 2],
        ["SELECT"],
        ["execute", "fetch", "fetch"],
    )
