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
import logging
import os
import sqlite3
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
    loop = asyncio.get_running_loop()
    errors: list[dict] = []  # a second put on a full pool raises QueueFull inside the loop
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))

    async def many_writes():
        for i in range(50):  # one long-lived task, many checkouts
            await _write(adapter, str(i))

    try:
        await asyncio.wait_for(asyncio.ensure_future(many_writes()), BOUND_S)
        await _settle()
        await _pool_full(adapter)
        # another task's write still succeeds, and no put hit a full pool
        await asyncio.wait_for(asyncio.ensure_future(_write(adapter, "after")), BOUND_S)
        await _settle()
    finally:
        loop.set_exception_handler(old_handler)
    assert errors == []
    assert adapter._pool.qsize() == adapter._pool_size
    # The pool still holds exactly pool_size distinct connections.
    conns = [adapter._pool.get_nowait() for _ in range(adapter._pool_size)]
    assert len({id(c) for c in conns}) == adapter._pool_size
    for c in conns:
        adapter._pool.put_nowait(c)
    # internals
    assert adapter._leases == set()
    assert adapter._reclaims == set()


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
    reclaims_while_child_runs = set(adapter._reclaims)
    qsize_while_child_runs = adapter._pool.qsize()
    go_on.set()
    await asyncio.wait_for(holder["child"], BOUND_S)
    await _pool_full(adapter)
    # behaviour: nothing was rolled back under the child, the connection is back once
    assert await _values(adapter) == ["parent", "child-1", "child-2"]
    assert adapter._pool.qsize() == adapter._pool_size
    # internals, as seen while the child was running
    assert reclaims_while_child_runs == set()
    assert qsize_while_child_runs == adapter._pool_size - 1


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
            try:
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
                pending_before_close = len(adapter._reclaims)
                await asyncio.wait_for(adapter.close(), BOUND_S)
                reclaims_after_first_close = set(adapter._reclaims)

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
                qsize_after_second_close = adapter._pool.qsize()
                reclaims_after_second_close = set(adapter._reclaims)
            finally:
                await adapter.close()  # a failed assertion must not leave aiosqlite threads running
    finally:
        loop.set_exception_handler(old_handler)
    # behaviour: no exception reached the loop, no warning was raised, nothing is in the pool
    assert errors == []
    assert [str(w.message) for w in recwarn] == []
    assert qsize_after_second_close == 0
    # internals
    assert pending_before_close == 1
    assert reclaims_after_first_close == set()
    assert reclaims_after_second_close == set()


@pytest.fixture
def warnings_log(caplog):
    caplog.set_level(logging.WARNING, logger="sqler.adapter.asynchronous")
    return caplog


async def _leave_a_connection_behind(adapter: AsyncSQLiteAdapter) -> None:
    acquired = asyncio.Event()

    async def hold():
        await adapter.execute("INSERT INTO t (v) VALUES ('lost')")
        acquired.set()
        await asyncio.sleep(60)

    task = asyncio.ensure_future(hold())
    await asyncio.wait_for(acquired.wait(), BOUND_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_failed_rollback_replaces_the_connection_and_keeps_the_pool_size(
    disk_adapter, warnings_log, monkeypatch
):
    adapter = disk_adapter
    before = list(adapter._all_conns)

    real = aiosqlite.Connection.rollback
    failed: list[aiosqlite.Connection] = []

    async def rollback(self):
        if not failed:
            failed.append(self)
            raise sqlite3.OperationalError("disk I/O error")
        return await real(self)

    monkeypatch.setattr(aiosqlite.Connection, "rollback", rollback)
    await _leave_a_connection_behind(adapter)
    await _pool_full(adapter)

    assert adapter._pool.qsize() == adapter._pool_size
    assert len(adapter._all_conns) == adapter._pool_size
    assert failed[0] not in adapter._all_conns  # the broken one is gone, a fresh one took its place
    assert len(set(adapter._all_conns) - set(before)) == 1
    messages = [r.getMessage() for r in warnings_log.records if r.levelno == logging.WARNING]
    assert len(messages) == 1
    assert "disk I/O error" in messages[0]
    # the pool works: every slot can be checked out and written through
    await asyncio.wait_for(
        asyncio.gather(*(asyncio.ensure_future(_write(adapter, str(i))) for i in range(4))), BOUND_S
    )
    await _pool_full(adapter)
    assert sorted(await _values(adapter)) == ["0", "1", "2", "3"]  # 'lost' was not committed


@pytest.mark.asyncio
async def test_rollback_failing_because_the_adapter_closed_adds_no_connection_and_no_warning(
    disk_adapter, warnings_log, monkeypatch
):
    adapter = disk_adapter
    owned = list(adapter._all_conns)  # the fake close below forgets them; close them at the end

    async def rollback(self):
        adapter._all_conns.clear()  # the adapter closed while the rollback was running
        raise ValueError("Connection closed")

    monkeypatch.setattr(aiosqlite.Connection, "rollback", rollback)
    await _leave_a_connection_behind(adapter)

    async def rollback_ran():
        while adapter._all_conns:  # emptied by the fake rollback
            await asyncio.sleep(0.01)

    await asyncio.wait_for(rollback_ran(), BOUND_S)
    await _settle()

    assert adapter._all_conns == []
    assert adapter._pool.qsize() == adapter._pool_size - 1  # nothing put back, nothing opened
    assert [r for r in warnings_log.records if r.levelno >= logging.WARNING] == []
    for conn in owned:
        await conn.close()
