"""Waiting for a pooled async connection is bounded, and the pool's state is readable.

``acquire_timeout`` bounds the wait for a free connection; a wait that runs out
raises ``ConnectionPoolExhaustedError`` and leaves the waiting task holding
nothing. ``pool_stats()`` reports the pool's size, what is free and in use, and
counts checkouts, timeouts and wait times. Every wait is bounded at one second.
"""

import asyncio
import os
import sqlite3
import tempfile
import time

import pytest
import pytest_asyncio
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import ConnectionPoolExhaustedError, NotConnectedError
from sqler.pool import PoolStats

BOUND_S = 1.0
PRAGMAS = [
    "PRAGMA busy_timeout = 1000",  # two writers take turns
    "PRAGMA journal_mode = WAL",
    # no fsync: with it a commit can take 100 ms on a busy disk, and one connection writing
    # back to back then starves the other past its busy timeout; these tests are about the pool
    "PRAGMA synchronous = OFF",
]


@pytest_asyncio.fixture
async def make_adapter():
    made: list[AsyncSQLiteAdapter] = []
    with tempfile.TemporaryDirectory() as tmpdir:

        async def make(pool_size: int, **kwargs) -> AsyncSQLiteAdapter:
            path = os.path.join(tmpdir, f"t{len(made)}.db")
            adapter = AsyncSQLiteAdapter(path, pragmas=list(PRAGMAS), pool_size=pool_size, **kwargs)
            made.append(adapter)
            await adapter.connect()
            await adapter.execute("CREATE TABLE t (v TEXT NOT NULL UNIQUE)")
            await adapter.auto_commit()
            return adapter

        try:
            yield make
        finally:
            for adapter in made:
                await adapter.close()


async def _write(adapter: AsyncSQLiteAdapter, value: str) -> None:
    await adapter.execute("INSERT INTO t (v) VALUES (?)", [value])
    await adapter.auto_commit()


async def _in_task(coro):
    return await asyncio.wait_for(asyncio.ensure_future(coro), BOUND_S)


async def _waiters(adapter: AsyncSQLiteAdapter, count: int) -> None:
    """Wait (bounded) until exactly ``count`` tasks are waiting for a pooled connection."""

    async def poll():
        while adapter._waiting != count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), BOUND_S)


async def _values(adapter: AsyncSQLiteAdapter) -> list[str]:
    async def read():
        cur = await adapter.execute("SELECT v FROM t ORDER BY rowid")
        rows = await cur.fetchall()
        await cur.close()
        await adapter.auto_commit()
        return [r[0] for r in rows]

    return await _in_task(read())


# ---- item 4: the wait for a connection times out ----


def test_acquire_timeout_defaults_to_30_seconds_and_the_adapter_factories_pass_it_through():
    assert AsyncSQLiteAdapter("x.db").acquire_timeout == 30.0
    assert AsyncSQLiteAdapter.on_disk("x.db").acquire_timeout == 30.0
    assert AsyncSQLiteAdapter.on_disk("x.db", acquire_timeout=1.5).acquire_timeout == 1.5
    assert AsyncSQLiteAdapter.in_memory(acquire_timeout=None).acquire_timeout is None


@pytest.mark.asyncio
async def test_wait_that_times_out_while_the_holder_is_in_a_transaction(make_adapter):
    adapter = await make_adapter(pool_size=1, acquire_timeout=0.1)
    in_txn = asyncio.Event()
    finish = asyncio.Event()
    seen: dict = {}

    async def holder():
        await adapter.begin_transaction()
        await adapter.execute("INSERT INTO t (v) VALUES ('holder')")
        in_txn.set()
        await finish.wait()
        await adapter.end_transaction(commit=True)

    async def waiter():
        with pytest.raises(ConnectionPoolExhaustedError) as info:
            await adapter.execute("INSERT INTO t (v) VALUES ('waiter')")
        seen["message"] = str(info.value)
        seen["connection"] = adapter.connection

    running = asyncio.ensure_future(holder())
    await asyncio.wait_for(in_txn.wait(), BOUND_S)
    await _in_task(waiter())
    stats_after_timeout = adapter.pool_stats()
    finish.set()
    await asyncio.wait_for(running, BOUND_S)
    await _in_task(_write(adapter, "after"))

    assert seen == {
        "message": "no pooled connection became free within 0.1 s (pool size 1)",
        "connection": None,
    }
    assert stats_after_timeout.total_timeouts == 1
    assert stats_after_timeout.available_connections == 0
    assert stats_after_timeout.in_use_connections == 1
    assert await _values(adapter) == ["holder", "after"]
    assert adapter._pool.qsize() == 1


@pytest.mark.asyncio
async def test_acquire_timeout_none_waits_until_the_holder_releases(make_adapter):
    adapter = await make_adapter(pool_size=1, acquire_timeout=None)
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        await adapter.execute("INSERT INTO t (v) VALUES ('holder')")
        held.set()
        await release.wait()
        await adapter.auto_commit()

    running = asyncio.ensure_future(holder())
    await asyncio.wait_for(held.wait(), BOUND_S)
    waiting = asyncio.ensure_future(_write(adapter, "waiter"))
    await _waiters(adapter, 1)
    assert waiting.done() is False
    release.set()
    await asyncio.wait_for(asyncio.gather(running, waiting), BOUND_S)
    assert await _values(adapter) == ["holder", "waiter"]


@pytest.mark.asyncio
async def test_close_while_a_task_waits_raises_in_the_waiter(make_adapter):
    adapter = await make_adapter(pool_size=1, acquire_timeout=None)
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        await adapter.execute("INSERT INTO t (v) VALUES ('holder')")
        held.set()
        await release.wait()
        await adapter.auto_commit()  # its connection went with close(): nothing to commit

    running = asyncio.ensure_future(holder())
    await asyncio.wait_for(held.wait(), BOUND_S)
    waiting = asyncio.ensure_future(_write(adapter, "waiter"))
    await _waiters(adapter, 1)  # the waiter is now blocked on the pool
    await asyncio.wait_for(adapter.close(), BOUND_S)
    with pytest.raises(NotConnectedError, match="closed while waiting for a pooled connection"):
        await asyncio.wait_for(waiting, BOUND_S)
    release.set()
    await asyncio.wait_for(running, BOUND_S)
    assert adapter._pool.qsize() == 0
    assert adapter._all_conns == []


# ---- item 5: pool_stats ----


@pytest.mark.asyncio
async def test_pool_stats_of_a_fresh_pool(make_adapter):
    adapter = await make_adapter(pool_size=3)
    stats = adapter.pool_stats()
    assert isinstance(stats, PoolStats)
    assert stats.total_connections == 3
    assert stats.max_connections == 3
    assert stats.available_connections == 3
    assert stats.in_use_connections == 0
    assert stats.total_checkouts == 1  # the fixture's CREATE TABLE
    assert stats.total_timeouts == 0


@pytest.mark.asyncio
async def test_pool_stats_counts_one_checkout_per_pin(make_adapter):
    adapter = await make_adapter(pool_size=2)
    before = adapter.pool_stats().total_checkouts
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        await adapter.execute("INSERT INTO t (v) VALUES ('a')")
        await adapter.execute("INSERT INTO t (v) VALUES ('b')")  # same pin, no new checkout
        held.set()
        await release.wait()
        await adapter.auto_commit()

    running = asyncio.ensure_future(holder())
    await asyncio.wait_for(held.wait(), BOUND_S)
    during = adapter.pool_stats()
    release.set()
    await asyncio.wait_for(running, BOUND_S)
    after = adapter.pool_stats()

    assert (during.available_connections, during.in_use_connections) == (1, 1)
    assert during.total_checkouts - before == 1
    assert (after.available_connections, after.in_use_connections) == (2, 0)
    assert after.total_checkouts - before == 1


@pytest.mark.asyncio
async def test_fifty_tasks_on_a_pool_of_two_with_every_third_failing(make_adapter):
    adapter = await make_adapter(pool_size=2, acquire_timeout=BOUND_S)
    await _in_task(_write(adapter, "dup"))
    before = adapter.pool_stats().total_checkouts
    all_done = asyncio.Event()
    done = 0
    failures = 0

    async def one(i: int):
        nonlocal done, failures
        value = "dup" if i % 3 == 2 else str(i)  # every third statement hits the UNIQUE constraint
        try:
            await _write(adapter, value)
        except sqlite3.IntegrityError:
            failures += 1
        done += 1
        if done == 50:
            all_done.set()
        await all_done.wait()  # every task stays alive until all 50 have run their statement

    tasks = [asyncio.ensure_future(one(i)) for i in range(50)]
    await asyncio.wait_for(all_done.wait(), BOUND_S * 3)
    stats_while_alive = adapter.pool_stats()
    await asyncio.wait_for(asyncio.gather(*tasks), BOUND_S)

    assert failures == 16  # i % 3 == 2 for 16 values of i in range(50)
    assert stats_while_alive.available_connections == 2
    assert stats_while_alive.in_use_connections == 0
    assert stats_while_alive.total_connections == 2
    assert stats_while_alive.total_checkouts - before == 50
    assert stats_while_alive.total_timeouts == 0
    assert len(await _values(adapter)) == 35


@pytest.mark.asyncio
async def test_pool_stats_wait_times_are_measured(make_adapter):
    adapter = await make_adapter(pool_size=1, acquire_timeout=None)
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        await adapter.execute("INSERT INTO t (v) VALUES ('holder')")
        held.set()
        await release.wait()
        await adapter.auto_commit()

    running = asyncio.ensure_future(holder())
    await asyncio.wait_for(held.wait(), BOUND_S)
    before_wait = time.perf_counter()
    waiting = asyncio.ensure_future(_write(adapter, "waiter"))
    await _waiters(adapter, 1)
    seen_waiting = time.perf_counter()
    await asyncio.sleep(0)  # hand the loop over once while it waits: its wait spans this
    released = time.perf_counter()
    release.set()
    await asyncio.wait_for(asyncio.gather(running, waiting), BOUND_S)
    after_wait = time.perf_counter()
    stats = adapter.pool_stats()

    # the waiter's wait began before it was seen waiting and ended after the release;
    # it began after the test started it and ended before the test saw it done
    shortest_ms = (released - seen_waiting) * 1000
    longest_ms = (after_wait - before_wait) * 1000
    assert stats.total_checkouts == 3  # the fixture, the holder, the waiter
    assert stats.total_timeouts == 0
    assert shortest_ms <= stats.max_wait_time_ms <= longest_ms
    # the other two checkouts found a free connection without waiting: their share of
    # the total is what is left after the waiter's wait, and that is at most its wait
    others_ms = stats.avg_wait_time_ms * 3 - stats.max_wait_time_ms
    assert 0.0 <= others_ms <= stats.max_wait_time_ms


@pytest.mark.asyncio
async def test_db_pool_stats_forwards_the_adapters():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = AsyncSQLerDB.on_disk(os.path.join(tmpdir, "t.db"))
        await db.connect()
        try:
            stats = db.pool_stats()
            assert (
                stats.total_connections,
                stats.available_connections,
                stats.in_use_connections,
            ) == (
                4,
                4,
                0,
            )
            assert stats.total_checkouts == 0
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_pool_stats_after_close(make_adapter):
    adapter = await make_adapter(pool_size=2)
    await adapter.close()
    stats = adapter.pool_stats()
    assert (stats.total_connections, stats.available_connections, stats.in_use_connections) == (
        0,
        0,
        0,
    )
