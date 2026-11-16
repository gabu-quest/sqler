"""The async adapter and raw work that SQLite holds in a transaction the adapter did not open.

A transaction does not begin over such work: ``begin_transaction`` raises
``TransactionStateError``, sends nothing, and the work stays pending on the pinned
connection for ``commit()`` or a raw ``ROLLBACK``. A statement that fails outside a
transaction ends all that was sent since the last commit, but for a library caller that
judges the failure itself. Memory (a pool of one) and a file (a pool of two).
"""

import asyncio
import contextvars
import sqlite3
from typing import Optional

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.adapter import AsyncSQLiteAdapter
from sqler.exceptions import TransactionStateError
from sqler.models import async_integrity

pytestmark = pytest.mark.asyncio

BOUND_S = 5.0
PENDING = (
    "a transaction cannot begin: SQLite holds a transaction on this connection that the adapter did not "
    "open (a raw BEGIN, or a raw write not committed yet); nothing was sent and that work is still pending: "
    'keep it with db.adapter.commit() or drop it with db.adapter.execute("ROLLBACK"), then begin again'
)


@pytest_asyncio.fixture(params=["memory", "file"])
async def db(request, tmp_path):
    """A database whose table ``t (v UNIQUE)`` holds ``before``, committed; nothing is pinned."""
    if request.param == "memory":
        adapter = AsyncSQLiteAdapter(":memory:", pool_size=1, acquire_timeout=BOUND_S)
    else:
        adapter = AsyncSQLiteAdapter(str(tmp_path / "a.db"), pool_size=2, acquire_timeout=BOUND_S)
    database = AsyncSQLerDB(adapter)
    await database.connect()
    await raw(adapter, "CREATE TABLE t (v TEXT NOT NULL UNIQUE)")
    await raw(adapter, "INSERT INTO t VALUES ('before')")
    await adapter.auto_commit()
    try:
        yield database
    finally:
        await database.close()


async def raw(adapter: AsyncSQLiteAdapter, sql: str, params: Optional[list] = None) -> None:
    await (await adapter.execute(sql, params)).close()


async def pending(adapter: AsyncSQLiteAdapter) -> tuple[bool, list[str]]:
    """Whether SQLite counts a transaction open on this task's connection, and the rows it sees there."""
    cur = await adapter.execute("SELECT v FROM t ORDER BY rowid")
    rows = [r[0] for r in await cur.fetchall()]
    return adapter._pinned().in_transaction, rows


@pytest.mark.parametrize(
    "work", [["INSERT INTO t VALUES ('raw')"], ["BEGIN", "INSERT INTO t VALUES ('raw')"]]
)
async def test_a_transaction_after_raw_uncommitted_work_is_refused_and_the_work_stays_pending(
    db, work
):
    adapter = db.adapter
    for sql in work:
        await raw(adapter, sql)
    conn = adapter._pinned()
    sent: list[str] = []
    await conn.set_trace_callback(sent.append)
    with pytest.raises(TransactionStateError) as raised:
        async with db.transaction():
            await raw(adapter, "INSERT INTO t VALUES ('in the transaction')")
    await conn.set_trace_callback(None)
    assert (str(raised.value), sent, adapter._pinned() is conn) == (PENDING, [], True)
    assert await pending(adapter) == (True, ["before", "raw"])
    await adapter.commit()  # the first way out: keep the work
    async with db.transaction():
        await raw(adapter, "INSERT INTO t VALUES ('after')")
    assert await pending(adapter) == (False, ["before", "raw", "after"])
    await adapter.auto_commit()


async def test_the_refused_work_can_be_rolled_back_with_a_raw_rollback(db):
    adapter = db.adapter
    await raw(adapter, "INSERT INTO t VALUES ('raw')")
    with pytest.raises(TransactionStateError):
        await adapter.begin_transaction()
    await raw(adapter, "ROLLBACK")  # the second way out: drop the work
    await adapter.begin_transaction()
    await adapter.end_transaction(commit=True)
    assert await pending(adapter) == (False, ["before"])
    await adapter.auto_commit()


async def test_the_refused_work_keeps_its_pooled_connection_from_another_task(tmp_path):
    adapter = AsyncSQLiteAdapter(str(tmp_path / "pool.db"), pool_size=2, acquire_timeout=BOUND_S)
    await adapter.connect()
    try:
        await raw(adapter, "CREATE TABLE t (v TEXT NOT NULL UNIQUE)")
        await adapter.auto_commit()
        await raw(adapter, "INSERT INTO t VALUES ('raw')")
        mine = adapter._pinned()
        with pytest.raises(TransactionStateError):
            await adapter.begin_transaction()

        async def other() -> tuple[bool, list[str]]:
            seen = await pending(adapter)
            theirs = adapter._pinned()
            await adapter.auto_commit()
            return theirs is mine, seen[1]

        # a task of its own, pinned to nothing: it gets the other connection, and sees nothing pending
        task = asyncio.create_task(other(), context=contextvars.Context())
        assert await asyncio.wait_for(task, BOUND_S) == (False, [])
        assert (adapter._pinned() is mine, adapter.pool_stats().available_connections) == (True, 1)
        await adapter.commit()
        assert await pending(adapter) == (False, ["raw"])
        await adapter.auto_commit()
    finally:
        await adapter.close()


@pytest.mark.parametrize("left", ["unread-select", "pragma", "ddl", "failed-insert"])
async def test_raw_statements_that_leave_no_transaction_open_let_a_transaction_begin(db, left):
    """Passes on base: only a transaction SQLite holds refuses the begin."""
    adapter = db.adapter
    if left == "unread-select":
        await adapter.execute("SELECT v FROM t")
    elif left == "pragma":
        await raw(adapter, "PRAGMA user_version = 7")
    elif left == "ddl":
        await raw(adapter, "CREATE TABLE made (x)")
    else:
        with pytest.raises(sqlite3.OperationalError):
            await raw(adapter, "INSERT INTO absent VALUES (1)")
    async with db.transaction():
        await raw(adapter, "INSERT INTO t VALUES ('in')")
    assert await pending(adapter) == (False, ["before", "in"])
    await adapter.auto_commit()


async def test_a_write_that_opens_no_transaction_commits_raw_work_left_pending_with_it(db):
    """Passes on base: as sqlite3 does, a write outside a transaction commits what is pending."""
    await raw(db.adapter, "INSERT INTO t VALUES ('raw')")
    await db.insert_document("docs", {"n": 1})
    assert await pending(db.adapter) == (False, ["before", "raw"])
    await db.adapter.auto_commit()


async def test_closing_with_raw_work_pending_rolls_it_back(tmp_path):
    """Passes on base."""
    adapter = AsyncSQLiteAdapter(str(tmp_path / "close.db"), pool_size=1, acquire_timeout=BOUND_S)
    await adapter.connect()
    await raw(adapter, "CREATE TABLE t (v TEXT NOT NULL)")
    await adapter.auto_commit()
    await raw(adapter, "INSERT INTO t VALUES ('raw')")
    await adapter.close()
    await adapter.connect()
    try:
        assert await pending(adapter) == (False, [])
    finally:
        await adapter.close()


# ---- outside a transaction, what was sent since the last commit is one unit ----
#
# A statement that fails ends the unit: all of it is rolled back, the pin is given up and the
# error is raised. A library caller that reads a failure as an answer (the look for
# referrers) sends it with ``_execute_judged``, which ends nothing; ``_end_failed_statement``
# ends what ``execute`` would have ended and raises.


class ARawItem(AsyncSQLerModel):
    __tablename__ = "raw_items"
    name: str = ""


class ARawHolder(AsyncSQLerModel):
    __tablename__ = "raw_holders"
    target: Optional[ARawItem] = None


async def holder_and_unreadable_row(db, unreadable: str) -> int:
    """A ``raw_items`` row ``a`` that one ``raw_holders`` row refers to, and a row SQLite's JSON
    functions cannot read, read ``unreadable`` (first or last), committed."""
    adapter = db.adapter
    item = await ARawItem(name="a").save(db=db)
    await ARawHolder(target=item).save(db=db)
    unreadable_row = f"{item._id} raw_items"
    if unreadable == "first":
        await raw(adapter, "UPDATE raw_holders SET _id = 5")
        await raw(adapter, "INSERT INTO raw_holders (_id, data) VALUES (1, ?)", [unreadable_row])
    else:
        await raw(adapter, "INSERT INTO raw_holders (data) VALUES (?)", [unreadable_row])
    await adapter.commit()
    return item._id


async def name(adapter: AsyncSQLiteAdapter) -> str:
    cur = await adapter.execute("SELECT json_extract(data, '$.name') FROM raw_items")
    return (await cur.fetchone())[0]


@pytest.mark.parametrize("unreadable", ["first", "last"])
async def test_a_look_for_referrers_that_reads_a_failed_row_keeps_raw_work_pending(db, unreadable):
    """The look reads a table holding a row SQLite's JSON functions cannot read: the failed
    read (at the statement when the row is first, while rows are fetched when it is last)
    is an answer to it, so it must not roll back what was pending before it."""
    adapter = db.adapter
    item_id = await holder_and_unreadable_row(db, unreadable)
    await raw(adapter, "UPDATE raw_items SET data = json_set(data, '$.name', 'raw')")  # not committed
    found = await async_integrity.async_find_referrers(db, "raw_items", item_id)
    assert [(table, paths) for table, _, paths in found] == [("raw_holders", {"paths": ["target"]})]
    assert await name(adapter) == "raw"
    await adapter.commit()
    assert await name(adapter) == "raw"
    await adapter.auto_commit()


async def test_a_look_whose_second_statement_fails_too_raises_and_ends_the_unit(db):
    """The unreadable row sends the look to its second statement, which SQLite stops (an
    interrupted read is no answer): the error is raised, the pin is given up, and the write
    sent before the look is rolled back with it."""
    adapter = db.adapter
    item_id = await holder_and_unreadable_row(db, "first")
    await raw(adapter, "UPDATE raw_items SET data = json_set(data, '$.name', 'raw')")  # not committed
    conn = adapter._pinned()
    armed: list[str] = []

    def stop_once() -> int:
        if armed:
            armed.clear()
            return 1
        return 0

    await conn.set_trace_callback(
        lambda sql: armed.append(sql) if sql.startswith("SELECT _id, data FROM raw_holders") and "json_" not in sql else None
    )
    await conn.set_progress_handler(stop_once, 1)
    try:
        with pytest.raises(sqlite3.OperationalError, match="^interrupted$"):
            await async_integrity.async_find_referrers(db, "raw_items", item_id)
    finally:
        await conn.set_progress_handler(None, 1)
        await conn.set_trace_callback(None)
    assert (adapter._pinned(), conn.in_transaction) == (None, False)
    assert await name(adapter) == "a"
    await adapter.auto_commit()


async def fail(adapter: AsyncSQLiteAdapter, failing: str) -> None:
    if failing == "constraint":
        await raw(adapter, "INSERT INTO t VALUES ('before')")
    elif failing == "refused-when-prepared":
        await raw(adapter, "SELECT (1, 2) = (SELECT 1, 2, 3)")
    else:
        await adapter.executemany("INSERT INTO t VALUES (?)", [["many"], ["before"]])


@pytest.mark.parametrize("failing", ["constraint", "refused-when-prepared", "executemany"])
async def test_a_statement_that_fails_after_pending_work_rolls_the_whole_unit_back(db, failing):
    """Passes on base: the failed statement ends all that was sent since the last commit and
    gives the pinned connection back, so no later commit makes part of the unit durable."""
    adapter = db.adapter
    await raw(adapter, "INSERT INTO t VALUES ('raw')")  # not committed
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        await fail(adapter, failing)
    assert adapter._pinned() is None
    assert await pending(adapter) == (False, ["before"])
    await adapter.auto_commit()


@pytest.mark.parametrize("failing", ["constraint", "refused-when-prepared", "executemany"])
async def test_a_statement_that_fails_with_nothing_pending_gives_its_connection_back(db, failing):
    """Passes on base: the transaction the failed statement opened is rolled back."""
    adapter = db.adapter
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        await fail(adapter, failing)
    assert adapter._pinned() is None
    assert await pending(db.adapter) == (False, ["before"])
    await adapter.auto_commit()


REFUSED_WRITE = "INSERT INTO t SELECT (1, 2) = (SELECT 1, 2, 3)"  # refused when prepared


async def test_a_judged_failure_ends_nothing_and_the_second_call_ends_the_unit_and_raises(db):
    """The pin and the lease end on the second call as after a failed ``execute``: the
    connection is back in the pool."""
    adapter = db.adapter
    await raw(adapter, "INSERT INTO t VALUES ('first')")  # the unit's earlier write
    conn = adapter._pinned()
    with pytest.raises(sqlite3.OperationalError, match="^row value misused$") as refused:
        await adapter._execute_judged(REFUSED_WRITE)
    assert adapter._pinned() is conn
    assert await pending(adapter) == (True, ["before", "first"])
    with pytest.raises(sqlite3.OperationalError) as ended:
        await adapter._end_failed_statement(refused.value)
    assert (ended.value is refused.value, adapter._pinned(), conn.in_transaction) == (True, None, False)
    assert adapter.pool_stats().available_connections == adapter._pool_size
    assert await pending(adapter) == (False, ["before"])
    await adapter.auto_commit()


async def test_the_second_call_with_nothing_pending_raises_and_leaves_no_transaction(db):
    adapter = db.adapter
    with pytest.raises(sqlite3.OperationalError) as refused:
        await adapter._execute_judged(REFUSED_WRITE)
    with pytest.raises(sqlite3.OperationalError) as ended:
        await adapter._end_failed_statement(refused.value)
    assert (ended.value is refused.value, adapter._pinned()) == (True, None)
    assert await pending(adapter) == (False, ["before"])
    await adapter.auto_commit()


async def test_a_judged_failure_and_the_second_call_inside_a_transaction_end_nothing(db):
    adapter = db.adapter
    async with db.transaction():
        await raw(adapter, "INSERT INTO t VALUES ('a')")
        conn, lease = adapter._pinned(), adapter._task_lease.get()
        with pytest.raises(sqlite3.OperationalError) as refused:
            await adapter._execute_judged(REFUSED_WRITE)
        with pytest.raises(sqlite3.OperationalError):
            await adapter._end_failed_statement(refused.value)
        # the transaction keeps its connection, and the next statement has its turn at once
        assert (adapter._pinned() is conn, lease.returned, lease.broken, lease.turns) == (True, False, None, [])
        assert adapter._turn(lease, adapter._txn_savepoints.get()) is True
        await raw(adapter, "INSERT INTO t VALUES ('b')")
    assert await pending(adapter) == (False, ["before", "a", "b"])
    await adapter.auto_commit()


SLOW = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT count(*) FROM c"


async def test_a_cancellation_in_a_judged_statement_ends_the_unit(db):
    """A cancellation is never the caller's to judge: it ends the unit as in ``execute``. The
    task catches it and goes on: it holds no connection, and its commit makes nothing of the
    unit durable."""
    adapter = db.adapter
    sending = asyncio.Event()
    pinned: list = []

    async def unit() -> None:
        await raw(adapter, "INSERT INTO t VALUES ('first')")  # the unit's earlier write
        pinned.append(adapter._pinned() is not None)
        sending.set()
        try:
            await adapter._execute_judged(SLOW)  # queued in this step
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
        pinned.append(adapter._pinned() is not None)
        await adapter.commit()

    task = asyncio.create_task(unit(), context=contextvars.Context())
    await asyncio.wait_for(sending.wait(), BOUND_S)
    task.cancel()
    await asyncio.wait_for(task, BOUND_S)
    while adapter._reclaims:  # the rollback runs once the cancelled statement is done
        await asyncio.wait_for(asyncio.gather(*adapter._reclaims), BOUND_S)
    assert pinned == [True, False]
    assert [c.in_transaction for c in adapter._all_conns] == [False] * adapter._pool_size
    assert adapter.pool_stats().available_connections == adapter._pool_size
    assert await pending(adapter) == (False, ["before"])
    await adapter.auto_commit()


async def test_a_read_of_rows_that_fails_after_pending_work_rolls_the_whole_unit_back(db):
    """Passes on base: a failed read of a cursor's rows ends the unit as a failed statement does."""
    adapter = db.adapter
    await raw(adapter, "INSERT INTO t VALUES ('raw')")  # not committed
    cur = await adapter.execute("SELECT column1, json(column1) FROM (VALUES ('1'), ('2'), ('{'))")
    assert await cur.fetchone() == ("1", "1")
    with pytest.raises(sqlite3.OperationalError, match="^malformed JSON$"):
        await cur.fetchall()
    assert adapter._pinned() is None
    assert await pending(adapter) == (False, ["before"])
    await adapter.auto_commit()
