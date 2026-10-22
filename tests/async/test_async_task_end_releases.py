"""A task that ends while holding a pooled connection must not keep it.

The async adapter pins a pooled connection to the task that uses it and returns
it on ``auto_commit`` / ``end_transaction`` / ``__aexit__``. A task cancelled
between its statement and that release (``wait_for`` timing out, an outer
``cancel()``) used to end with the connection still out of the pool and its
write transaction open: every other writer then waited out the busy timeout and
failed with "database is locked".

Statements are made to hang by patching ``aiosqlite.Cursor.fetchone``, so no
test depends on timing luck. Every wait has an upper bound well inside the
5 s busy timeout of ``on_disk``.
"""

import asyncio
import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from sqler.adapter.asynchronous import AsyncSQLiteAdapter

BOUND_S = 2.5  # a write that waits on a leaked lock takes the full 5 s busy timeout


@pytest.fixture
def hang_fetchone(monkeypatch):
    """While ``state["on"]`` is true, ``Cursor.fetchone`` sleeps (to be cancelled)."""
    real = aiosqlite.Cursor.fetchone
    state = {"on": False}

    async def fetchone(self):
        if state["on"]:
            await asyncio.sleep(60)
        return await real(self)

    monkeypatch.setattr(aiosqlite.Cursor, "fetchone", fetchone)
    return state


@pytest_asyncio.fixture
async def disk_adapter():
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = AsyncSQLiteAdapter.on_disk(os.path.join(tmpdir, "t.db"))
        await adapter.connect()
        await adapter.execute("CREATE TABLE t (v TEXT)")
        await adapter.auto_commit()
        try:
            yield adapter
        finally:
            await adapter.close()


async def _write(adapter: AsyncSQLiteAdapter, value: str) -> None:
    await adapter.execute("INSERT INTO t (v) VALUES (?)", [value])
    await adapter.auto_commit()


async def _values(adapter: AsyncSQLiteAdapter) -> list[str]:
    return await asyncio.wait_for(asyncio.ensure_future(_read(adapter)), BOUND_S)


async def _read(adapter: AsyncSQLiteAdapter) -> list[str]:
    cur = await adapter.execute("SELECT v FROM t ORDER BY rowid")
    rows = await cur.fetchall()
    await cur.close()
    await adapter.auto_commit()
    return [r[0] for r in rows]


async def _pool_full(adapter: AsyncSQLiteAdapter) -> None:
    """Wait (bounded) until every pooled connection is back in the queue."""
    async def poll():
        while adapter._pool.qsize() != adapter._pool_size:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), BOUND_S)


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_task_cancelled_mid_statement_does_not_leave_the_database_locked(
    disk_adapter, hang_fetchone
):
    adapter = disk_adapter

    async def write_then_read():
        await adapter.execute("INSERT INTO t (v) VALUES ('lost')")
        cur = await adapter.execute("SELECT count(*) FROM t")
        await cur.fetchone()  # hangs; cancelled by wait_for while the INSERT's transaction is open
        await adapter.auto_commit()

    hang_fetchone["on"] = True
    with pytest.raises(asyncio.TimeoutError):
        # its own task (wait_for on a bare coroutine would run it in this test's task)
        await asyncio.wait_for(asyncio.ensure_future(write_then_read()), 0.3)
    hang_fetchone["on"] = False

    # Another task's write goes through at once, not after the 5 s busy timeout.
    await asyncio.wait_for(asyncio.ensure_future(_write(adapter, "kept")), BOUND_S)
    await _pool_full(adapter)
    assert await _values(adapter) == ["kept"]


@pytest.mark.asyncio
async def test_task_cancelled_between_acquire_and_first_statement(disk_adapter):
    adapter = disk_adapter
    acquired = asyncio.Event()

    async def hold():
        await adapter._acquire()
        acquired.set()
        await asyncio.sleep(60)

    task = asyncio.ensure_future(hold())
    await asyncio.wait_for(acquired.wait(), BOUND_S)
    assert adapter._pool.qsize() == adapter._pool_size - 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _pool_full(adapter)
    await asyncio.wait_for(asyncio.ensure_future(_write(adapter, "kept")), BOUND_S)
    assert await _values(adapter) == ["kept"]


@pytest.mark.asyncio
async def test_task_cancelled_inside_explicit_transaction_rolls_back(
    disk_adapter, hang_fetchone
):
    adapter = disk_adapter

    async def txn():
        await adapter.begin_transaction()
        await adapter.execute("INSERT INTO t (v) VALUES ('a')")
        await adapter.execute("INSERT INTO t (v) VALUES ('b')")
        cur = await adapter.execute("SELECT count(*) FROM t")
        await cur.fetchone()  # hangs inside BEGIN IMMEDIATE
        await adapter.end_transaction(commit=True)

    hang_fetchone["on"] = True
    task = asyncio.ensure_future(txn())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(task, 0.3)
    hang_fetchone["on"] = False

    await asyncio.wait_for(asyncio.ensure_future(_write(adapter, "kept")), BOUND_S)
    await _pool_full(adapter)
    assert await _values(adapter) == ["kept"]  # the cancelled transaction's writes are undone


@pytest.mark.asyncio
async def test_task_that_released_normally_then_ends_is_not_returned_twice(disk_adapter):
    adapter = disk_adapter

    async def many_writes():
        for i in range(50):  # one long-lived task, many checkouts
            await _write(adapter, str(i))

    await asyncio.wait_for(asyncio.ensure_future(many_writes()), BOUND_S)
    await _settle()
    assert adapter._pool.qsize() == adapter._pool_size
    assert adapter._leases == set()
    assert adapter._reclaims == set()
    # The pool still holds exactly pool_size distinct connections.
    conns = [adapter._pool.get_nowait() for _ in range(adapter._pool_size)]
    assert len({id(c) for c in conns}) == adapter._pool_size
    for c in conns:
        adapter._pool.put_nowait(c)


@pytest.mark.asyncio
async def test_child_task_keeps_the_connection_when_its_parent_ends_first(disk_adapter):
    adapter = disk_adapter
    child_started = asyncio.Event()
    go_on = asyncio.Event()
    holder: dict[str, asyncio.Task] = {}

    async def child():
        await adapter.execute("INSERT INTO t (v) VALUES ('child-1')")
        child_started.set()
        await go_on.wait()
        await adapter.execute("INSERT INTO t (v) VALUES ('child-2')")
        await adapter.auto_commit()

    async def parent():
        await adapter.execute("INSERT INTO t (v) VALUES ('parent')")
        holder["child"] = asyncio.ensure_future(child())  # copies the context: same connection
        await child_started.wait()
        # ends without releasing; the child is still using the connection

    await asyncio.wait_for(asyncio.ensure_future(parent()), BOUND_S)
    await _settle()
    assert adapter._reclaims == set()  # no rollback under the child
    assert adapter._pool.qsize() == adapter._pool_size - 1
    go_on.set()
    await asyncio.wait_for(holder["child"], BOUND_S)
    await _pool_full(adapter)
    assert await _values(adapter) == ["parent", "child-1", "child-2"]


@pytest.mark.asyncio
async def test_child_inheriting_a_returned_connection_takes_a_fresh_one(disk_adapter):
    adapter = disk_adapter
    released = asyncio.Event()
    holder: dict[str, asyncio.Task] = {}

    async def child():
        await released.wait()
        await _write(adapter, "child")

    async def parent():
        await adapter.execute("INSERT INTO t (v) VALUES ('parent')")
        holder["child"] = asyncio.ensure_future(child())  # inherits the pin, never acquires before release
        await adapter.auto_commit()
        released.set()

    await asyncio.wait_for(asyncio.ensure_future(parent()), BOUND_S)
    await asyncio.wait_for(holder["child"], BOUND_S)
    await _settle()
    assert adapter._pool.qsize() == adapter._pool_size
    assert await _values(adapter) == ["parent", "child"]


@pytest.mark.asyncio
async def test_close_with_a_reclaim_pending_raises_and_warns_nothing(recwarn):
    loop = asyncio.get_running_loop()
    errors: list[dict] = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = AsyncSQLiteAdapter.on_disk(os.path.join(tmpdir, "t.db"))
            await adapter.connect()
            acquired = asyncio.Event()

            async def hold():
                await adapter.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
                acquired.set()
                await asyncio.sleep(60)

            # 1. the owner ends, its reclaim is scheduled, close() runs before it does
            task = asyncio.ensure_future(hold())
            await asyncio.wait_for(acquired.wait(), BOUND_S)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(adapter._reclaims) == 1
            await asyncio.wait_for(adapter.close(), BOUND_S)
            assert adapter._reclaims == set()

            # 2. the adapter closes first, the owner ends afterwards
            await adapter.connect()
            acquired.clear()
            task = asyncio.ensure_future(hold())
            await asyncio.wait_for(acquired.wait(), BOUND_S)
            await asyncio.wait_for(adapter.close(), BOUND_S)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await _settle()
            assert adapter._reclaims == set()
            assert adapter._pool.qsize() == 0
    finally:
        loop.set_exception_handler(old_handler)
    assert errors == []
    assert [str(w.message) for w in recwarn] == []
