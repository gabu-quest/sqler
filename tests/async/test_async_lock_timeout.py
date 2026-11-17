"""How long a statement of the async adapter waits for a lock: ``timeout_ms``.

The same number and the same rules as the sync adapter's (``tests/test_lock_timeout.py``):
however the caller says it (the ``timeout_ms`` argument of the constructor or of a factory,
or a ``busy_timeout`` pragma in ``pragmas``), after ``connect()`` ``adapter.timeout_ms``,
SQLite's ``busy_timeout`` on every pooled connection and the measured wait are the same.
``acquire_timeout`` is another wait (for a pooled connection) and is not touched here.
"""

import asyncio
import sqlite3
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB
from sqler.adapter import AsyncSQLiteAdapter
from sqler.exceptions import NotConnectedError


def _locked_by_another_connection(path: str) -> sqlite3.Connection:
    """A second connection on ``path`` that holds the write lock until it is closed."""
    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO t VALUES ('held')")
    return other


async def _seconds_until_locked(adapter: AsyncSQLiteAdapter) -> float:
    """Insert while another connection holds the write lock; the seconds until it raises."""
    start = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="^database is locked$"):
        await adapter.execute("INSERT INTO t VALUES ('mine')")
    return time.monotonic() - start


async def _file_adapter(path: str, **kwargs) -> AsyncSQLiteAdapter:
    adapter = AsyncSQLiteAdapter(path, **kwargs)
    await adapter.connect()
    await adapter.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    await adapter.auto_commit()
    return adapter


async def _busy_timeouts(adapter: AsyncSQLiteAdapter) -> list[int]:
    """``busy_timeout`` on every pooled connection, all taken at once by as many tasks."""
    taken = asyncio.Barrier(adapter._pool_size)

    async def read() -> int:
        async with adapter:  # pins one connection to this task until the block ends
            await taken.wait()  # every task holds its own connection now
            cur = await adapter.execute("PRAGMA busy_timeout")
            return (await cur.fetchone())[0]

    return list(await asyncio.gather(*(read() for _ in range(adapter._pool_size))))


# ---- the wait on a file database ----


@pytest.mark.asyncio
async def test_timeout_ms_is_the_wait_for_another_connections_lock(tmp_path, clock_tick):
    path = str(tmp_path / "wait.db")
    adapter = await _file_adapter(path, timeout_ms=200)
    other = _locked_by_another_connection(path)
    try:
        elapsed = await _seconds_until_locked(adapter)
    finally:
        other.close()
        await adapter.close()
    assert 0.2 - clock_tick <= elapsed < 2.5


@pytest.mark.asyncio
async def test_timeout_ms_zero_raises_at_once(tmp_path):
    path = str(tmp_path / "zero.db")
    adapter = await _file_adapter(path, timeout_ms=0, pool_size=2)
    other = _locked_by_another_connection(path)
    try:
        elapsed = await _seconds_until_locked(adapter)
        busy = await _busy_timeouts(adapter)
    finally:
        other.close()
        await adapter.close()
    assert (busy, elapsed < 2.5) == ([0, 0], True)  # the wait this replaces was 5 s


@pytest.mark.asyncio
async def test_a_timeout_python_would_truncate_is_the_exact_number(tmp_path):
    adapter = await _file_adapter(str(tmp_path / "exact.db"), timeout_ms=1001, pool_size=2)
    try:
        assert (adapter.timeout_ms, await _busy_timeouts(adapter)) == (1001, [1001, 1001])
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_the_adapter_made_with_a_path_only_waits_five_seconds(tmp_path):
    """What a caller that passes nothing else (``AsyncSQLiteAdapter(path=...)``) has always had."""
    adapter = AsyncSQLiteAdapter(path=str(tmp_path / "plain.db"))
    await adapter.connect()
    try:
        assert (adapter.timeout_ms, await _busy_timeouts(adapter)) == (5000, [5000])
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_the_lock_held_by_a_second_async_adapter_is_waited_for_timeout_ms(tmp_path, clock_tick):
    path = str(tmp_path / "two.db")
    holder = await _file_adapter(path)
    waiter = await _file_adapter(path, timeout_ms=200)
    try:
        async with holder:
            await holder.execute("BEGIN IMMEDIATE")
            await holder.execute("INSERT INTO t VALUES ('held')")
            elapsed = await _seconds_until_locked(waiter)
            await holder.execute("ROLLBACK")
    finally:
        await waiter.close()
        await holder.close()
    assert 0.2 - clock_tick <= elapsed < 2.5


@pytest.mark.asyncio
async def test_a_statement_cancelled_while_it_waits_for_the_lock_gives_its_connection_back(tmp_path):
    path = str(tmp_path / "cancel.db")
    adapter = await _file_adapter(path, timeout_ms=4000)
    loop = asyncio.get_running_loop()
    sent = asyncio.Event()

    def traced(sql: str) -> None:  # runs in the connection's thread
        # the transaction Python opens for the write begins with BEGIN IMMEDIATE, which is the
        # statement that waits for the write lock
        if sql.startswith("BEGIN IMMEDIATE"):
            loop.call_soon_threadsafe(sent.set)

    (conn,) = adapter._all_conns
    await conn.set_trace_callback(traced)
    other = _locked_by_another_connection(path)
    try:
        task = asyncio.create_task(adapter.execute("INSERT INTO t VALUES ('mine')"))
        await asyncio.wait_for(sent.wait(), 10)  # SQLite has the statement: it waits for the lock
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        other.close()  # the lock goes: the waiting statement ends in its thread
    try:
        await asyncio.wait_for(asyncio.gather(*adapter._reclaims), 10)
        assert adapter._pool.qsize() == adapter._pool_size
        await adapter.execute("INSERT INTO t VALUES ('after')")
        await adapter.auto_commit()
        cur = await adapter.execute("SELECT v FROM t")
        rows = [r[0] for r in await cur.fetchall()]
        await adapter.auto_commit()
        assert (rows, adapter._pool.qsize()) == (["after"], adapter._pool_size)
    finally:
        await adapter.close()


# ---- every way in passes the number on ----

FACTORIES = {
    "constructor": lambda path, **kw: AsyncSQLiteAdapter(path, **kw),
    "adapter-on-disk": lambda path, **kw: AsyncSQLiteAdapter.on_disk(path, **kw),
    "adapter-in-memory-shared": lambda path, **kw: AsyncSQLiteAdapter.in_memory(True, **kw),
    "adapter-in-memory-plain": lambda path, **kw: AsyncSQLiteAdapter.in_memory(False, **kw),
    "db-on-disk": lambda path, **kw: AsyncSQLerDB.on_disk(path, **kw).adapter,
    "db-in-memory-shared": lambda path, **kw: AsyncSQLerDB.in_memory(True, **kw).adapter,
    "db-in-memory-plain": lambda path, **kw: AsyncSQLerDB.in_memory(False, **kw).adapter,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", list(FACTORIES))
async def test_every_factory_passes_timeout_ms_to_every_connection(factory, tmp_path):
    adapter = FACTORIES[factory](str(tmp_path / "f.db"), timeout_ms=1234)
    await adapter.connect()
    try:
        assert (adapter.timeout_ms, await _busy_timeouts(adapter)) == (1234, [1234] * adapter._pool_size)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", list(FACTORIES))
async def test_every_factory_waits_five_seconds_by_default(factory, tmp_path):
    adapter = FACTORIES[factory](str(tmp_path / "f.db"))
    await adapter.connect()
    try:
        assert (adapter.timeout_ms, await _busy_timeouts(adapter)) == (5000, [5000] * adapter._pool_size)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_a_pool_of_four_reads_timeout_ms_on_all_four_connections(tmp_path):
    adapter = AsyncSQLiteAdapter.on_disk(str(tmp_path / "four.db"), timeout_ms=150)
    await adapter.connect()
    try:
        assert (adapter._pool_size, await _busy_timeouts(adapter)) == (4, [150, 150, 150, 150])
    finally:
        await adapter.close()


@pytest.mark.parametrize("value", [True, False, 1.5, -1, "100", 2**31], ids=repr)
def test_a_timeout_that_is_not_a_whole_number_of_milliseconds_raises_at_construction(value):
    with pytest.raises(ValueError, match="^timeout_ms must be a whole number of milliseconds") as raised:
        AsyncSQLiteAdapter(":memory:", timeout_ms=value)
    assert str(raised.value).endswith(f"not {value!r}")


@pytest.mark.parametrize("factory", ["on_disk", "in_memory"])
@pytest.mark.parametrize("owner", [AsyncSQLiteAdapter, AsyncSQLerDB], ids=["adapter", "db"])
def test_a_factory_given_a_negative_timeout_raises_before_it_opens_anything(owner, factory, tmp_path):
    args = (str(tmp_path / "never.db"),) if factory == "on_disk" else ()
    with pytest.raises(ValueError, match="not -1$"):
        getattr(owner, factory)(*args, timeout_ms=-1)
    assert list(tmp_path.iterdir()) == []


# ---- a busy_timeout pragma says the same number ----

PRAGMA_FORMS = ["PRAGMA busy_timeout=250;", "pragma BUSY_TIMEOUT = 250"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pragma", PRAGMA_FORMS)
async def test_a_busy_timeout_pragma_is_the_number_when_timeout_ms_is_not_given(pragma, tmp_path, clock_tick):
    path = str(tmp_path / "pragma.db")
    adapter = await _file_adapter(path, pragmas=[pragma], pool_size=2)
    other = _locked_by_another_connection(path)
    try:
        elapsed = await _seconds_until_locked(adapter)
        assert (adapter.timeout_ms, await _busy_timeouts(adapter)) == (250, [250, 250])
    finally:
        other.close()
        await adapter.close()
    assert 0.25 - clock_tick <= elapsed < 2.5


@pytest.mark.asyncio
async def test_timeout_ms_and_a_pragma_that_agree_are_one_number(tmp_path):
    adapter = await _file_adapter(
        str(tmp_path / "agree.db"), timeout_ms=250, pragmas=["PRAGMA busy_timeout = 250"], pool_size=2
    )
    try:
        assert (adapter.timeout_ms, await _busy_timeouts(adapter)) == (250, [250, 250])
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["file", ":memory:"])
async def test_timeout_ms_and_a_pragma_that_disagree_raise_at_connect_naming_both(path, tmp_path):
    adapter = AsyncSQLiteAdapter(
        str(tmp_path / "disagree.db") if path == "file" else path,
        pragmas=["PRAGMA busy_timeout = 250"],
        timeout_ms=300,
        pool_size=2,
    )
    # a second connect, nothing fixed, raises the same; so does a statement, which connects first
    for attempt in (adapter.connect, adapter.connect, lambda: adapter.execute("SELECT 1")):
        with pytest.raises(ValueError) as raised:
            await attempt()
        assert str(raised.value) == "timeout_ms=300 and the busy_timeout pragma (250) disagree: give one of them"
        # the connect that failed left nothing open and the adapter not connected
        assert (adapter._all_conns, adapter._pool.qsize(), adapter._connected) == ([], 0, False)
    await adapter.close()


# ---- a shared-cache in-memory database: the same wait ----
#
# SQLite calls no busy handler for a shared-cache table lock (SQLITE_LOCKED_SHAREDCACHE): the
# adapter waits for it itself, in the connection's thread, up to ``timeout_ms``. Two adapters on
# one named database are two connections; the holder keeps its lock until ``release()``, and
# ``pauses.first`` is set when the waiter starts to wait.


@pytest.fixture
def pauses(monkeypatch):
    """The pauses of the adapter's lock wait: ``count``, and ``first``, set at the first."""
    real = time.sleep
    seen = SimpleNamespace(count=0, first=threading.Event())

    def pause(seconds: float) -> None:
        seen.count += 1
        seen.first.set()
        real(seconds)

    monkeypatch.setattr(time, "sleep", pause)
    return seen


@pytest_asyncio.fixture
async def pair():
    """``make(timeout_ms)``: a holder and a waiter adapter on one new shared-cache database, with
    ``t`` holding 'a' and 'b' and ``u`` holding 'x' and 'y'; both closed at the end."""
    made: list[AsyncSQLiteAdapter] = []

    async def make(timeout_ms: int = 5000) -> tuple[AsyncSQLiteAdapter, AsyncSQLiteAdapter]:
        name = f"lock-wait-{uuid.uuid4().hex}"
        holder = AsyncSQLiteAdapter.in_memory(shared=True, name=name, timeout_ms=timeout_ms)
        waiter = AsyncSQLiteAdapter.in_memory(shared=True, name=name, timeout_ms=timeout_ms)
        made.extend((holder, waiter))
        await holder.connect()
        await waiter.connect()
        await holder.executescript(
            "CREATE TABLE t (v TEXT); CREATE TABLE u (v TEXT);"
            "INSERT INTO t VALUES ('a'), ('b'); INSERT INTO u VALUES ('x'), ('y');"
        )
        return holder, waiter

    yield make
    for adapter in reversed(made):
        await adapter.close()


async def _hold(adapter: AsyncSQLiteAdapter, kind: str):
    """Take the lock: ``write``, a transaction that has written 'held' into ``t``; ``read``, a
    SELECT over ``t`` with one row read. Returns the call that releases it (a commit for ``write``)."""
    if kind == "write":
        await adapter.begin_transaction()
        await adapter.execute("INSERT INTO t VALUES ('held')")
        return lambda: adapter.end_transaction(commit=True)
    cursor = await adapter.execute("SELECT v FROM t")
    assert (await cursor.fetchone())[0] == "a"

    async def release() -> None:
        await cursor.close()
        await adapter.auto_commit()

    return release


async def _values(adapter: AsyncSQLiteAdapter, table: str = "t") -> list[str]:
    cursor = await adapter.execute(f"SELECT v FROM {table} ORDER BY rowid")
    rows = [row[0] for row in await cursor.fetchall()]
    await adapter.auto_commit()
    return rows


async def _insert_mine(adapter: AsyncSQLiteAdapter) -> None:
    await adapter.execute("INSERT INTO t VALUES ('mine')")
    await adapter.auto_commit()


async def _many(adapter: AsyncSQLiteAdapter) -> None:
    await adapter.executemany("INSERT INTO t VALUES (?)", [["m1"], ["m2"], ["m3"]])


async def _many_inside(adapter: AsyncSQLiteAdapter) -> None:
    await adapter.begin_transaction()
    await adapter.executemany("INSERT INTO t VALUES (?)", [["m1"], ["m2"], ["m3"]])
    await adapter.end_transaction(commit=True)


async def _begin(adapter: AsyncSQLiteAdapter) -> None:
    await adapter.begin_transaction()
    await adapter.execute("INSERT INTO t VALUES ('mine')")
    await adapter.end_transaction(commit=True)


async def _inside(adapter: AsyncSQLiteAdapter) -> None:
    await adapter.begin_transaction()
    await adapter.execute("INSERT INTO u VALUES ('mine')")
    await adapter.execute("INSERT INTO t VALUES ('mine')")  # meets the reader's lock on t
    await adapter.end_transaction(commit=True)


# (the lock held, what the waiter does, what it returns, t at the end, u at the end)
WAITS = {
    "insert": ("write", _insert_mine, None, ["a", "b", "held", "mine"], ["x", "y"]),
    "select": ("write", _values, ["a", "b", "held"], ["a", "b", "held"], ["x", "y"]),
    "executemany": ("read", _many, None, ["a", "b", "m1", "m2", "m3"], ["x", "y"]),
    "executemany-inside-a-transaction": ("read", _many_inside, None, ["a", "b", "m1", "m2", "m3"], ["x", "y"]),
    "begin_transaction": ("write", _begin, None, ["a", "b", "held", "mine"], ["x", "y"]),
    "inside-a-transaction": ("read", _inside, None, ["a", "b", "mine"], ["x", "y", "mine"]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(WAITS))
async def test_a_statement_that_meets_another_adapters_table_lock_waits_for_it_and_runs_once(case, pair, pauses):
    kind, work, returned, t_rows, u_rows = WAITS[case]
    holder, waiter = await pair()
    release = await _hold(holder, kind)
    task = asyncio.ensure_future(work(waiter))
    try:
        assert await asyncio.to_thread(pauses.first.wait, 10)  # it waits ...
        assert not task.done()  # ... and has not finished while the lock is held
    finally:
        await release()
    assert await asyncio.wait_for(task, 30) == returned
    assert (await _values(waiter, "t"), await _values(waiter, "u")) == (t_rows, u_rows)
    assert waiter._pool.qsize() == 1


@pytest.mark.asyncio
async def test_an_adapter_that_connects_while_another_holds_an_uncommitted_table_waits_and_connects(pauses):
    """A new connection reads the schema with its first statement; a table another adapter has
    made and not committed locks it."""
    name = f"lock-wait-{uuid.uuid4().hex}"
    holder = AsyncSQLiteAdapter.in_memory(shared=True, name=name)
    newcomer = AsyncSQLiteAdapter.in_memory(shared=True, name=name)
    await holder.connect()
    try:
        await holder.begin_transaction()
        await holder.execute("CREATE TABLE made (v TEXT)")
        task = asyncio.ensure_future(newcomer.connect())
        try:
            assert await asyncio.to_thread(pauses.first.wait, 10)  # it waits ...
            assert not task.done()  # ... and has not connected while the lock is held
        finally:
            await holder.end_transaction(commit=True)
        await asyncio.wait_for(task, 30)
        assert await _values(newcomer, "made") == []
    finally:
        await newcomer.close()
        await holder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write", "read"])
async def test_a_lock_that_is_not_released_raises_sqlites_error_once_timeout_ms_has_passed(kind, pair, clock_tick):
    holder, waiter = await pair(timeout_ms=200)
    release = await _hold(holder, kind)
    try:
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as raised:
            await waiter.execute("INSERT INTO t VALUES ('mine')")
        elapsed = time.monotonic() - start
        # the failed write left nothing open: the connection is back in the pool
        settled = (waiter.in_transaction, waiter._pool.qsize())
    finally:
        await release()
    assert (str(raised.value).split(":")[0], raised.value.sqlite_errorcode) == (
        "database table is locked",
        sqlite3.SQLITE_LOCKED_SHAREDCACHE,
    )
    assert elapsed >= 0.2 - clock_tick
    assert settled == (False, 1)
    assert await _values(waiter) == (["a", "b", "held"] if kind == "write" else ["a", "b"])


@pytest.mark.asyncio
async def test_timeout_ms_zero_raises_the_shared_cache_lock_at_once(pair, pauses):
    holder, waiter = await pair(timeout_ms=0)
    release = await _hold(holder, "write")
    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            await waiter.execute("INSERT INTO t VALUES ('mine')")
    finally:
        await release()
    assert (raised.value.sqlite_errorcode, pauses.count) == (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 0)
    assert await _values(waiter) == ["a", "b", "held"]


@pytest.mark.asyncio
async def test_executemany_with_rows_from_an_iterator_raises_at_once(pair, pauses):
    """An iterator has given up the rows it was asked for: they cannot be sent a second time."""
    holder, waiter = await pair()
    release = await _hold(holder, "read")
    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            await waiter.executemany("INSERT INTO t VALUES (?)", iter([["m1"], ["m2"]]))
    finally:
        await release()
    assert (raised.value.sqlite_errorcode, pauses.count) == (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 0)
    assert await _values(waiter) == ["a", "b"]


@pytest.mark.asyncio
async def test_executescript_that_meets_the_lock_raises_at_once_with_its_earlier_statements_run(pair, pauses):
    """The statements before the one that met the lock have run: the script is not sent again."""
    holder, waiter = await pair()
    release = await _hold(holder, "read")
    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            await waiter.executescript("INSERT INTO u VALUES ('s1'); INSERT INTO t VALUES ('s2');")
    finally:
        await release()
    assert (raised.value.sqlite_errorcode, pauses.count) == (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 0)
    assert (await _values(waiter, "t"), await _values(waiter, "u")) == (["a", "b"], ["x", "y", "s1"])


@pytest.mark.asyncio
async def test_two_tasks_that_each_hold_what_the_other_wants_both_end_after_timeout_ms(pair, clock_tick):
    """The holder's transaction has written t and wants u; the other task reads u through the
    waiter and wants t through a third adapter. Neither can go on: each raises once its wait has passed."""
    holder, waiter = await pair(timeout_ms=300)
    third = AsyncSQLiteAdapter(holder.path, pragmas=list(holder.pragmas), timeout_ms=300)
    await third.connect()
    await _hold(holder, "write")  # ended below, rolled back
    cursor = await waiter.execute("SELECT v FROM u")
    assert (await cursor.fetchone())[0] == "x"

    async def timed(statement) -> tuple[int, float]:
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as raised:
            await statement
        return raised.value.sqlite_errorcode, time.monotonic() - start

    try:
        ends = await asyncio.wait_for(
            asyncio.gather(
                timed(holder.execute("INSERT INTO u VALUES ('holder-wants')")),
                timed(third.execute("INSERT INTO t VALUES ('third-wants')")),
            ),
            30,
        )
    finally:
        await cursor.close()
        await waiter.auto_commit()
        await holder.end_transaction(commit=False)
        await third.close()
    assert [code for code, _ in ends] == [sqlite3.SQLITE_LOCKED_SHAREDCACHE] * 2
    assert [elapsed >= 0.3 - clock_tick for _, elapsed in ends] == [True, True]
    assert (await _values(waiter, "t"), await _values(waiter, "u")) == (["a", "b"], ["x", "y"])


@pytest.mark.asyncio
async def test_a_waiter_whose_adapter_is_closed_while_it_waits_ends_with_an_error(pair, pauses):
    holder, waiter = await pair(timeout_ms=60_000)  # far longer than the test may take
    release = await _hold(holder, "write")
    try:
        task = asyncio.ensure_future(waiter.execute("INSERT INTO t VALUES ('mine')"))
        assert await asyncio.to_thread(pauses.first.wait, 10)
        await asyncio.wait_for(waiter.close(), 30)  # it does not wait on for the 60 s it was given
        with pytest.raises(NotConnectedError) as raised:
            await asyncio.wait_for(task, 30)
    finally:
        await release()
    assert str(raised.value) == "the adapter was closed while a statement waited for another connection's lock"
    assert await _values(holder) == ["a", "b", "held"]


# ---- connect: once at a time, and a failed one leaves nothing open ----

# The first connection takes the write lock for good (exclusive locking mode), so the
# second connection's setup waits ``timeout_ms`` for it and fails with "database is locked".
SECOND_OPEN_FAILS = ["PRAGMA locking_mode = EXCLUSIVE", "PRAGMA user_version = 7"]


def _threads_left(before: set[threading.Thread]) -> list[threading.Thread]:
    """The threads started since ``before`` that are still alive, each joined for up to 10 s."""
    started = [t for t in threading.enumerate() if t not in before]
    for thread in started:
        thread.join(10)
    return [t for t in started if t.is_alive()]


def _new_threads(before: set[threading.Thread]) -> int:
    return len([t for t in threading.enumerate() if t not in before])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "twice",
    ["in-a-row", "together", "beside-a-statement"],
)
async def test_the_pool_is_opened_once_however_connect_is_called(twice, tmp_path):
    before = set(threading.enumerate())
    adapter = AsyncSQLiteAdapter.on_disk(str(tmp_path / "once.db"))
    if twice == "in-a-row":
        await adapter.connect()
        await adapter.connect()
    elif twice == "together":
        await asyncio.gather(adapter.connect(), adapter.connect())
    else:
        await asyncio.gather(adapter.connect(), adapter.execute("SELECT 1"))
        await asyncio.gather(*adapter._reclaims)  # the statement's task ended: its connection goes back
    try:
        assert (len(adapter._all_conns), adapter._pool.qsize(), _new_threads(before)) == (4, 4, 4)
    finally:
        await adapter.close()
    assert _threads_left(before) == []


@pytest.mark.asyncio
async def test_connect_after_close_opens_the_pool_again(tmp_path):
    adapter = AsyncSQLiteAdapter.on_disk(str(tmp_path / "again.db"))
    await adapter.connect()
    await adapter.close()
    before = set(threading.enumerate())
    await adapter.connect()
    try:
        assert (len(adapter._all_conns), adapter._pool.qsize(), _new_threads(before)) == (4, 4, 4)
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_size", [2, 3], ids=["the-last-open-fails", "the-second-of-three-fails"])
async def test_a_connect_whose_second_open_fails_closes_what_it_opened_and_raises_that_error(pool_size, tmp_path):
    path = str(tmp_path / "fails.db")
    before = set(threading.enumerate())
    adapter = AsyncSQLiteAdapter(path, pragmas=SECOND_OPEN_FAILS, pool_size=pool_size, timeout_ms=50)
    with pytest.raises(sqlite3.OperationalError, match="^database is locked$"):
        await adapter.connect()
    assert (_threads_left(before), adapter._all_conns, adapter._pool.qsize(), adapter._connected) == (
        [],
        [],
        0,
        False,
    )
    probe = sqlite3.connect(path, timeout=0)  # the first connection's lock went with it
    try:
        assert probe.execute("PRAGMA user_version").fetchone() == (7,)
    finally:
        probe.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancels", [1, 2], ids=["while-opening", "again-while-cleaning-up"])
async def test_a_connect_cancelled_while_an_open_waits_closes_what_it_opened(cancels, tmp_path):
    path = str(tmp_path / "cancelled.db")
    before = set(threading.enumerate())
    adapter = AsyncSQLiteAdapter(path, pragmas=SECOND_OPEN_FAILS, pool_size=3, timeout_ms=2000)
    task = asyncio.create_task(adapter.connect())
    while _new_threads(before) < 2:  # the first connection is open, the second is being set up
        await asyncio.sleep(0)
    task.cancel()
    for _ in range(cancels - 1):
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (_threads_left(before), adapter._all_conns, adapter._connected) == ([], [], False)


class _ThirdOpenWaits(AsyncSQLiteAdapter):
    """Its third ``_open`` waits until the test lets it go: two connections are open meanwhile."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opens = 0
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    async def _open(self):
        self.opens += 1
        if self.opens == 3:
            self.reached.set()
            await self.go.wait()
        return await super()._open()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancels", [1, 2], ids=["once", "again-while-the-two-close"])
async def test_a_connect_cancelled_with_two_connections_open_closes_both(cancels, tmp_path):
    before = set(threading.enumerate())
    adapter = _ThirdOpenWaits(str(tmp_path / "two.db"), pool_size=3)
    task = asyncio.create_task(adapter.connect())
    await adapter.reached.wait()
    assert _new_threads(before) == 2
    task.cancel()
    if cancels == 2:
        await asyncio.sleep(0)  # one step: the task is closing the two connections
        assert (task.done(), _new_threads(before)) == (False, 2)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (_threads_left(before), adapter._all_conns, adapter._connected) == ([], [], False)


@pytest.mark.asyncio
async def test_a_connection_whose_close_raises_does_not_stop_the_others_closing(tmp_path):
    before = set(threading.enumerate())
    adapter = AsyncSQLiteAdapter(str(tmp_path / "raises.db"), pool_size=3)
    await adapter.connect()
    def thread_of(conn):  # aiosqlite's connection is its thread, or holds it
        return conn if isinstance(conn, threading.Thread) else conn._thread

    first, bad, last = adapter._all_conns
    real_close = bad.close

    async def close_raises():
        raise RuntimeError("this connection does not close")

    bad.close = close_raises
    try:
        await adapter.close()  # raises nothing: the caller is closing
        assert (adapter._all_conns, adapter._pool.qsize(), adapter._connected) == ([], 0, False)
        for conn in (first, last):
            thread_of(conn).join(10)
        assert [thread_of(conn).is_alive() for conn in (first, bad, last)] == [False, True, False]
    finally:
        await real_close()
    assert _threads_left(before) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["connect", "close"])
async def test_close_beside_connect_leaves_what_the_later_of_the_two_says(first, tmp_path):
    before = set(threading.enumerate())
    adapter = AsyncSQLiteAdapter.on_disk(str(tmp_path / "beside.db"))
    if first == "connect":  # the close waits for the connect and closes what it opened
        await asyncio.gather(adapter.connect(), adapter.close())
        assert (adapter._connected, adapter._all_conns, adapter._pool.qsize(), _threads_left(before)) == (False, [], 0, [])
    else:
        await asyncio.gather(adapter.close(), adapter.connect())
        try:
            assert (adapter._connected, len(adapter._all_conns), adapter._pool.qsize()) == (True, 4, 4)
        finally:
            await adapter.close()
        assert _threads_left(before) == []


@pytest.mark.asyncio
async def test_a_close_cancelled_while_its_connections_close_leaves_an_adapter_that_opens_again(tmp_path):
    before = set(threading.enumerate())
    adapter = AsyncSQLiteAdapter.on_disk(str(tmp_path / "cancelled_close.db"))
    await adapter.connect()
    task = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)  # one step: the close is waiting for its four connections
    assert (task.done(), adapter._all_conns) == (False, [])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (adapter._connected, adapter._pool.qsize(), _threads_left(before)) == (False, 0, [])
    try:
        cursor = await adapter.execute("SELECT 41 + 1")  # connects again
        assert await cursor.fetchall() == [(42,)]
    finally:
        await adapter.close()
    assert _threads_left(before) == []


# ---- a write outside a transaction takes the write lock first, where SQLite waits ----
#
# Async twin of the tests of the same name in tests/test_lock_timeout.py: the adapter's
# implicit transaction before a write is BEGIN IMMEDIATE, not a plain BEGIN.


class _RolledBack(Exception):
    pass


async def _search_classes(path: str, content_sync: bool, one_connection: bool = False):
    from sqler import AsyncFTSIndex, AsyncSearchableMixin, AsyncSQLerModel

    tag = uuid.uuid4().hex[:8]

    class Note(AsyncSearchableMixin, AsyncSQLerModel):
        __tablename__ = f"notes_{tag}"
        body: str = ""

    class Fresh(AsyncSQLerModel):
        __tablename__ = f"fresh_{tag}"
        n: int = 0

    db = AsyncSQLerDB.on_disk(path)  # waits 5 s for a lock
    if one_connection:  # the pool of one: the reset and the save meet the same connection
        db = AsyncSQLerDB(AsyncSQLiteAdapter(path, pragmas=db.adapter.pragmas, pool_size=1))
    await db.connect()
    Note._fts_index = AsyncFTSIndex(Note, ["body"], content_sync=content_sync, db=db)
    await Note._fts_index.create(db)
    return db, Note, Fresh


async def _reset_own_schema(db: AsyncSQLerDB, Fresh) -> None:
    """The connection's own rolled-back CREATE TABLE (see the sync twin)."""
    try:
        async with db.transaction():
            await Fresh(n=1).save(db=db)
            raise _RolledBack()
    except _RolledBack:
        pass


def _held_for(path: str, seconds: float) -> tuple[threading.Event, threading.Thread]:
    """Another connection takes the write lock now; once the returned event is set, it holds it
    ``seconds`` more and commits."""
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    started = threading.Event()

    def release():
        started.wait(10)
        time.sleep(seconds)
        holder.execute("COMMIT")
        holder.close()

    thread = threading.Thread(target=release)
    thread.start()
    return started, thread


@pytest.mark.asyncio
@pytest.mark.parametrize("between", ["nothing", "a read"])
@pytest.mark.parametrize("content_sync", [True, False], ids=["content_sync", "kept-by-hand"])
async def test_a_search_save_after_the_connections_own_schema_reset_waits_for_the_lock(
    content_sync, between, tmp_path, clock_tick
):
    path = str(tmp_path / "search-wait.db")
    db, Note, Fresh = await _search_classes(path, content_sync, one_connection=True)
    try:
        await Note(body="first").save(db=db)  # its statements are prepared and cached
        await _reset_own_schema(db, Fresh)
        if between == "a read":
            assert [n.body for n in await Note.search("first", db=db)] == ["first"]
        started, releaser = _held_for(path, 0.3)
        start = time.monotonic()
        started.set()
        await Note(body="second").save(db=db)
        elapsed = time.monotonic() - start
        await asyncio.to_thread(releaser.join, 10)
        found = sorted(n.body for n in await Note.search("first OR second", db=db))
    finally:
        await db.close()
    assert (0.3 - clock_tick <= elapsed < 5, found) == (True, ["first", "second"])


@pytest.mark.asyncio
async def test_eight_tasks_saving_search_rows_beside_first_saves_of_a_new_class_never_fail(tmp_path):
    from sqler import AsyncFTSIndex, AsyncSearchableMixin, AsyncSQLerModel

    path = str(tmp_path / "search-tasks.db")
    db, Note, Fresh = await _search_classes(path, True)

    class Kept(AsyncSearchableMixin, AsyncSQLerModel):
        __tablename__ = f"kept_{uuid.uuid4().hex[:8]}"
        body: str = ""

    Kept._fts_index = AsyncFTSIndex(Kept, ["body"], content_sync=False, db=db)
    await Kept._fts_index.create(db)
    stop = time.monotonic() + 1.0
    errors: list[str] = []
    saved = [0] * 8

    async def worker(i: int, cls) -> None:
        while time.monotonic() < stop:
            try:
                await _reset_own_schema(db, Fresh)
                await cls(body="hello").save(db=db)
                saved[i] += 1
            except sqlite3.OperationalError as exc:
                errors.append(f"{cls.__name__}: {exc}")

    try:
        await asyncio.gather(*(worker(i, (Note, Kept)[i % 2]) for i in range(8)))
        counts = [await Note.search_count("hello", db=db), await Kept.search_count("hello", db=db)]
    finally:
        await db.close()
    assert errors == []
    assert counts == [sum(saved[0::2]), sum(saved[1::2])]
