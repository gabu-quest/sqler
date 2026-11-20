"""The async twin of ``tests/test_db_reads_create_nothing.py``.

A read through ``AsyncSQLerDB`` creates no table, waits for no write lock on a pooled
connection that read the schema before the table existed, and on a missing table keeps the
uncommitted statements of the task that reads.
"""

import asyncio
import sqlite3
import time
import warnings

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.exceptions import NoAdapterError
from sqler.logging import query_logger
from sqler.query import SQLerExpression
from sqler.query.async_query import AsyncSQLerQuery

pytestmark = pytest.mark.asyncio


class AThing(AsyncSQLerModel):
    n: int = 0


@pytest_asyncio.fixture
async def path(tmp_path):
    return str(tmp_path / "areads.db")


@pytest_asyncio.fixture
async def db(path):
    d = AsyncSQLerDB.on_disk(path)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


def _tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def _rows(path, table):
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


async def test_reads_of_a_missing_table_give_empty_and_create_nothing(db, path):
    assert await db.find_document("ghost", 1) is None
    assert await db.find_documents("ghost", [1, 2]) == []
    assert await db.find_document_with_version("ghost", 1) is None
    q = await db.query("ghost")
    assert await q.count() == 0
    assert await q.all() == []
    assert await q.first() is None
    assert await q.exists() is False
    assert "ghost" not in _tables(path)
    assert db.pool_stats().in_use_connections == 0


async def test_a_write_after_a_read_of_a_missing_table_creates_it(db):
    assert await db.find_document("ghost", 1) is None
    rid = await db.insert_document("ghost", {"n": 1})
    assert await db.find_document("ghost", rid) == {"n": 1, "_id": rid}


async def test_a_table_dropped_by_another_connection_reads_as_missing(db, path):
    rid = await db.insert_document("gone", {"n": 1})
    assert await db.find_document("gone", rid) == {"n": 1, "_id": rid}
    other = sqlite3.connect(path)
    other.execute("DROP TABLE gone")
    other.commit()
    other.close()
    assert await db.find_document("gone", rid) is None
    assert await db.find_documents("gone", [rid]) == []
    assert await (await db.query("gone")).count() == 0


async def test_a_pooled_read_does_not_wait_for_the_write_lock_on_a_stale_schema(db, path):
    # the pool's other connections read the schema before this table existed
    rid = await db.insert_document("docs", {"n": 1})
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO docs (data) VALUES ('{\"n\": 2}')")
    try:
        start = time.perf_counter()
        for _ in range(4):  # every connection of the pool, once
            assert await asyncio.wait_for(db.find_document("docs", rid), 3) == {"n": 1, "_id": rid}
        assert await asyncio.wait_for(db.find_documents("docs", [rid]), 3) == [{"n": 1, "_id": rid}]
        assert await asyncio.wait_for(AThing.using(db, "docs").count(), 3) == 1
        waited = time.perf_counter() - start
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert waited < 1.0, f"the reads waited {waited:.2f}s"


READS = {
    "using_all": lambda d: AThing.using(d, "absent").all(),
    "using_count": lambda d: AThing.using(d, "absent").count(),
    "find_document": lambda d: d.find_document("absent", 1),
    "find_documents": lambda d: d.find_documents("absent", [1]),
}


@pytest.mark.parametrize("read", list(READS))
async def test_a_read_of_a_missing_table_keeps_uncommitted_work(db, path, read):
    await db.insert_document("kept", {"n": 1})
    cur = await db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")  # uncommitted
    await cur.close()
    await READS[read](db)
    await db.adapter.commit()
    await db.adapter.auto_commit()
    assert _rows(path, "kept") == 2
    assert db.pool_stats().in_use_connections == 0


async def test_a_write_that_fails_still_rolls_back_uncommitted_work(db, path):
    await db.insert_document("kept", {"n": 1})
    await db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await db.adapter.execute("INSERT INTO absent (data) VALUES ('{}')")
    assert _rows(path, "kept") == 1


# --- the query is the one place where a missing table reads as empty --------------------
#
# The reference is an existing table with no rows, in the same database. Every method of the
# query that runs a statement on the query's own table gives on a missing table what it gives
# on that reference, creates nothing, and sends one statement (counted in the adapter's log).


def _page(p):
    return (p.items, p.total, p.total_pages, p.page, p.per_page)


async def _apage(awaitable):
    return _page(await awaitable)


async def _streamed(iterator):
    return [row async for row in iterator]


QUERY_CALLS = {
    "all": (lambda q: q.all(), []),
    "first": (lambda q: q.first(), None),
    "all_dicts": (lambda q: q.all_dicts(), []),
    "iter_dicts": (lambda q: _streamed(q.iter_dicts()), []),
    "first_dict": (lambda q: q.first_dict(), None),
    "count": (lambda q: q.count(), 0),
    "sum": (lambda q: q.sum("n"), None),
    "avg": (lambda q: q.avg("n"), None),
    "min": (lambda q: q.min("n"), None),
    "max": (lambda q: q.max("n"), None),
    "grouped_count": (lambda q: q.group_by("n").count(), []),
    "grouped_sum": (lambda q: q.group_by("n").sum("n"), []),
    "grouped_avg": (lambda q: q.group_by("n").avg("n"), []),
    "grouped_min": (lambda q: q.group_by("n").min("n"), []),
    "grouped_max": (lambda q: q.group_by("n").max("n"), []),
    "exists": (lambda q: q.exists(), False),
    "distinct_values": (lambda q: q.distinct_values("n"), []),
    "paginate": (lambda q: _apage(q.paginate(1, 5)), ([], 0, 0, 1, 5)),
    "update": (lambda q: q.update(n=1), 0),
    "update_one": (lambda q: q.update_one(n=1), None),
    "update_n": (lambda q: q.update_n(2, k=1), []),
    "delete": (lambda q: q.delete(), 0),
}


async def _db_query(d, t):
    return await d.query(t)


async def _by_hand(d, t):
    return AsyncSQLerQuery(table=t, adapter=d.adapter)


QUERY_ROUTES = {"db_query": _db_query, "by_hand": _by_hand}
# what a read of a missing table sends: a query from the database looks at the table first, as a
# model's query set does; one built by hand on the adapter sends its own statement only, and
# before an update the read of its table's columns (whether the update moves a version)
SENT = {"db_query": 2, "by_hand": 1}


def _sent(route, name):
    return SENT[route] + (route == "by_hand" and name.startswith("update"))


async def _model_query(d, table):
    cls = type(f"ABound_{table}", (AThing,), {"__tablename__": table, "__module__": __name__})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(d)
    if table != "absent":
        await d.ensure_table(table)
    return cls.query()


async def _using(d, t):
    return AThing.using(d, t)


QS_CALLS = {
    "all": (lambda q: q.all(), []),
    "first": (lambda q: q.first(), None),
    "as_dicts": (lambda q: q.as_dicts(), []),
    "iter": (lambda q: _streamed(q.iter()), []),
    "count": (lambda q: q.count(), 0),
    "sum": (lambda q: q.sum("n"), None),
    "avg": (lambda q: q.avg("n"), None),
    "min": (lambda q: q.min("n"), None),
    "max": (lambda q: q.max("n"), None),
    "grouped_count": (lambda q: q.group_by("n").count(), []),
    "grouped_sum": (lambda q: q.group_by("n").sum("n"), []),
    "grouped_avg": (lambda q: q.group_by("n").avg("n"), []),
    "grouped_min": (lambda q: q.group_by("n").min("n"), []),
    "grouped_max": (lambda q: q.group_by("n").max("n"), []),
    "exists": (lambda q: q.exists(), False),
    "distinct_values": (lambda q: q.distinct_values("n"), []),
    "paginate": (lambda q: _apage(q.paginate(1, 5)), ([], 0, 0, 1, 5)),
    "update": (lambda q: q.update(n=1), 0),
    "update_one": (lambda q: q.update_one(n=1), None),
    "update_n": (lambda q: q.update_n(2, k=1), []),
    "delete_all": (lambda q: q.delete_all(), 0),
}

QS_ROUTES = {"using": _using, "model_query": _model_query}


async def _answers(d, path, route, call):
    await d.ensure_table("present")
    reference = await call(await route(d, "present"))
    q = await route(d, "absent")
    with query_logger.capture() as sent:
        result = await call(q)
    return (
        reference,
        result,
        len(sent),
        "absent" in _tables(path),
        d.pool_stats().in_use_connections,
    )


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
@pytest.mark.parametrize("name", list(QUERY_CALLS))
async def test_the_query_of_a_missing_table_answers_as_an_empty_one(db, path, route, name):
    call, empty = QUERY_CALLS[name]
    assert await _answers(db, path, QUERY_ROUTES[route], call) == (
        empty,
        empty,
        _sent(route, name),
        False,
        0,
    )


@pytest.mark.parametrize("route", list(QS_ROUTES))
@pytest.mark.parametrize("name", list(QS_CALLS))
async def test_the_queryset_of_a_missing_table_answers_as_an_empty_one(db, path, route, name):
    call, empty = QS_CALLS[name]
    # two statements: the look at the table its database remembers nothing of, then its own
    assert await _answers(db, path, QS_ROUTES[route], call) == (empty, empty, 2, False, 0)


async def test_db_query_reads_through_its_database_on_the_adapters_own_connection(db):
    q = await db.query("absent")
    assert (q._adapter is db.adapter, q._db is db) == (True, True)


# --- hostile inputs ----------------------------------------------------------------------


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
async def test_a_missing_table_read_in_a_transaction_keeps_the_write_and_commits(db, path, route):
    await db.insert_document("kept", {"n": 1})
    async with db.transaction():
        await db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
        q = await QUERY_ROUTES[route](db, "absent")
        read = (await q.all(), await q.count())
        inside = await (await db.query("kept")).count()
    assert (read, inside, _rows(path, "kept"), "absent" in _tables(path)) == (([], 0), 2, 2, False)
    assert db.pool_stats().in_use_connections == 0


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
async def test_writes_of_a_missing_table_in_a_transaction_leave_it_usable(db, path, route):
    await db.insert_document("kept", {"n": 1})
    async with db.transaction():
        q = await QUERY_ROUTES[route](db, "absent")
        answers = (
            await q.update(n=1),
            await q.update_one(n=1),
            await q.update_n(2, k=1),
            await q.delete(),
        )
        await db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
    assert (answers, _rows(path, "kept"), "absent" in _tables(path)) == ((0, None, [], 0), 2, False)
    assert db.pool_stats().in_use_connections == 0


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
@pytest.mark.parametrize("name", ["all", "count", "update", "delete"])
async def test_a_statement_naming_another_missing_table_still_raises(db, route, name):
    await db.insert_document("here", {"n": 1})
    joined = SQLerExpression("EXISTS (SELECT 1 FROM other_missing)")
    q = (await QUERY_ROUTES[route](db, "here")).filter(joined)
    with pytest.raises(sqlite3.OperationalError, match="^no such table: other_missing$"):
        await QUERY_CALLS[name][0](q)
    assert db.pool_stats().in_use_connections == 0


@pytest.mark.parametrize(("own", "named"), [("a", "ab"), ("ab", "a")])
async def test_a_missing_table_whose_name_shares_a_prefix_is_not_the_own_table(db, own, named):
    await db.insert_document(own, {"n": 1})
    q = (await db.query(own)).filter(SQLerExpression(f"EXISTS (SELECT 1 FROM {named})"))
    with pytest.raises(sqlite3.OperationalError, match=f"^no such table: {named}$"):
        await q.count()


@pytest.mark.parametrize("name", list(QUERY_CALLS))
async def test_a_query_with_no_adapter_still_raises(name):
    with pytest.raises(NoAdapterError, match="No adapter set for query"):
        await QUERY_CALLS[name][0](AsyncSQLerQuery(table="absent"))


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
async def test_a_table_dropped_after_the_query_was_built_reads_as_empty(db, path, route):
    await db.insert_document("gone", {"n": 1})
    q = await QUERY_ROUTES[route](db, "gone")
    other = sqlite3.connect(path)
    other.execute("DROP TABLE gone")
    other.commit()
    other.close()
    assert (await q.all_dicts(), await q.count(), await q.group_by("n").count()) == ([], 0, [])
    assert db.pool_stats().in_use_connections == 0
