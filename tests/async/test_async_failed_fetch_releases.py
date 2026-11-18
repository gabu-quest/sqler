"""A fetch that fails gives its connection back; a cancelled statement in a transaction does not hold the caller.

Outside an explicit transaction, a ``fetchone``, ``fetchall``, ``fetchmany`` or ``async for``
that raises (or is cancelled) leaves the task holding no connection, and the pool is whole
when the caller sees the error. Inside ``async with db.transaction()`` nothing changes: the
transaction is still the caller's to end. A statement cancelled inside a transaction reaches
the caller at once; the connection goes back to the pool, rolled back, when its thread is done.

Pools of one, so a connection kept by mistake blocks the next task at once. Waits are bounded.
"""

import asyncio
import contextvars
import os
import sqlite3
import tempfile
import time

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB
from sqler.adapter.asynchronous import AsyncSQLiteAdapter

# first row reads fine, the second row's json() fails while the cursor is stepped
BAD_SECOND_ROW = "SELECT json(v) FROM t ORDER BY rowid"
# first row at once, the second only after scanning this many rows in the connection's thread
SLOW_SECOND_ROW = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 8000000) "
    "SELECT x FROM c WHERE x = 1 OR x = 8000000"
)
SLOW_COUNT = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 8000000) "
    "SELECT count(*) FROM c"
)
CANCEL_DELIVERED_S = 0.5
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def adapter():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "f.db")
        pragmas = [
            "PRAGMA busy_timeout = 5000",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = OFF",
        ]
        ad = AsyncSQLiteAdapter(path, pragmas=pragmas, pool_size=1, acquire_timeout=20)
        await ad.connect()
        await ad.execute("CREATE TABLE t (v TEXT)")
        await ad.execute("INSERT INTO t (v) VALUES ('{\"a\": 1}')")
        await ad.execute("INSERT INTO t (v) VALUES ('{bad')")
        await ad.auto_commit()
        try:
            yield ad
        finally:
            await ad.close()


async def _whole(ad: AsyncSQLiteAdapter) -> None:
    stats = ad.pool_stats()
    assert (stats.available_connections, stats.in_use_connections) == (1, 0)
    assert ad._pinned() is None


async def _fetchone(cur):
    await cur.fetchone()
    await cur.fetchone()


async def _fetchall(cur):
    await cur.fetchall()


async def _fetchmany(cur):
    await cur.fetchmany(5)


async def _iterate(cur):
    async for _ in cur:
        pass


FETCHES = {
    "fetchone": _fetchone,
    "fetchall": _fetchall,
    "fetchmany": _fetchmany,
    "iterate": _iterate,
}


@pytest.mark.parametrize("fetch", list(FETCHES))
async def test_failed_fetch_gives_the_connection_back(adapter, fetch):
    cur = await adapter.execute(BAD_SECOND_ROW)
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        await FETCHES[fetch](cur)
    await _whole(adapter)


async def test_failed_fetch_lets_another_task_read(adapter):
    cur = await adapter.execute(BAD_SECOND_ROW)
    with pytest.raises(sqlite3.OperationalError):
        await cur.fetchall()

    async def other():
        c = await adapter.execute("SELECT 1")
        row = await c.fetchone()
        await c.close()
        await adapter.auto_commit()
        return row[0]

    assert (
        await asyncio.wait_for(asyncio.create_task(other(), context=contextvars.Context()), 2) == 1
    )


async def test_failed_fetch_in_a_transaction_keeps_it(adapter):
    db = AsyncSQLerDB(adapter)
    async with db.transaction():
        await adapter.execute("INSERT INTO t (v) VALUES ('{\"a\": 2}')")
        cur = await adapter.execute(BAD_SECOND_ROW)
        with pytest.raises(sqlite3.OperationalError):
            await cur.fetchall()
        assert adapter._pinned() is not None and adapter.in_transaction
        await adapter.execute("INSERT INTO t (v) VALUES ('{\"a\": 3}')")
    await _whole(adapter)
    cur = await adapter.execute("SELECT count(*) FROM t WHERE v LIKE '{\"a\": %'")
    assert (await cur.fetchone())[0] == 3  # the first row and both inserts of the transaction
    await adapter.auto_commit()


async def test_cursor_keeps_its_interface(adapter):
    cur = await adapter.execute("SELECT v FROM t ORDER BY rowid")
    assert cur.description[0][0] == "v"
    assert len(await cur.fetchmany(1)) == 1
    assert (await cur.fetchone()) is not None
    await cur.close()
    cur = await adapter.execute("UPDATE t SET v = v WHERE 0")
    assert cur.rowcount == 0
    cur = await adapter.execute("INSERT INTO t (v) VALUES ('{}')")
    assert cur.lastrowid == 3
    await adapter.auto_commit()
    await _whole(adapter)


async def test_cancelled_fetch_gives_the_connection_back(adapter):
    cur = await adapter.execute(SLOW_SECOND_ROW)
    start = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(cur.fetchall(), 0.2)
    assert time.perf_counter() - start < CANCEL_DELIVERED_S
    assert adapter._pinned() is None
    # the pool of one is whole again once the connection's thread is done
    c = await asyncio.wait_for(adapter.execute("SELECT 1"), 30)
    assert (await c.fetchone())[0] == 1
    await c.close()
    await adapter.auto_commit()
    await _whole(adapter)


async def test_close_while_a_failed_fetch_returns_its_connection(adapter):
    cur = await adapter.execute(BAD_SECOND_ROW)

    async def failing():
        with pytest.raises(sqlite3.OperationalError):
            await cur.fetchall()

    task = asyncio.create_task(failing())
    await asyncio.wait_for(asyncio.gather(task, adapter.close()), 10)
    assert adapter.pool_stats().total_connections == 0


async def test_statement_cancelled_in_a_transaction_does_not_hold_the_caller(adapter):
    db = AsyncSQLerDB(adapter)
    await adapter.execute("INSERT INTO t (v) VALUES ('{\"a\": 9}')")  # pinned, uncommitted
    await adapter.auto_commit()
    start = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError):
        async with db.transaction():
            await adapter.execute("INSERT INTO t (v) VALUES ('{\"a\": 10}')")
            await asyncio.wait_for(adapter.execute(SLOW_COUNT), 0.2)
    waited = time.perf_counter() - start
    assert waited < CANCEL_DELIVERED_S, f"the caller waited {waited:.2f}s"
    # once the thread is done the connection is back, rolled back
    c = await asyncio.wait_for(
        adapter.execute("SELECT count(*) FROM t WHERE v = '{\"a\": 10}'"), 30
    )
    assert (await c.fetchone())[0] == 0
    await c.close()
    await adapter.auto_commit()
    await _whole(adapter)
