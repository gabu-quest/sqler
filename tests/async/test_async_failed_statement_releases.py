"""A statement that fails must not leave its task holding a pooled connection.

Outside an explicit transaction, a failed statement (an error from SQLite, a
cancellation, a failed commit, a failed ``BEGIN``) rolls the connection back and
returns it to the pool, so a long-lived task that fails once does not starve
every other task. Inside an explicit transaction the pin stays (the caller
decides), and the transaction's end returns the connection whether its commit or
rollback works.

Most tests use a pool of one, so a connection kept by mistake blocks the next
task at once. Every wait is bounded at one second.
"""

import asyncio
import os
import sqlite3
import tempfile
import time

import aiosqlite
import pytest
import pytest_asyncio
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.exceptions import QueryError

BOUND_S = 1.0
CANCEL_DELIVERED_S = 0.5  # a cancellation reaches the caller within this, whatever SQLite is doing
BUSY_MS = 5000  # on_disk's busy timeout: a blocked statement waits this long in the SQLite thread

# (statement, params, the sqlite3 error it raises)
FAILING = {
    "syntax_error": ("INSERT INTO t (v) VALUSE ('x')", None, sqlite3.OperationalError),
    "constraint_violation": ("INSERT INTO t (v) VALUES (?)", ["dup"], sqlite3.IntegrityError),
    "param_count_mismatch": ("INSERT INTO t (v) VALUES (?)", ["a", "b"], sqlite3.ProgrammingError),
}


@pytest_asyncio.fixture
async def make_adapter():
    made: list[AsyncSQLiteAdapter] = []
    with tempfile.TemporaryDirectory() as tmpdir:

        async def make(
            pool_size: int = 1, busy_ms: int = 0, journal: str = "WAL"
        ) -> AsyncSQLiteAdapter:
            """busy_ms=0 (the default): a lock is an error at once, never a wait."""
            path = os.path.join(tmpdir, f"t{len(made)}.db")
            pragmas = [
                "PRAGMA foreign_keys = ON",
                f"PRAGMA busy_timeout = {busy_ms}",
                f"PRAGMA journal_mode = {journal}",
                "PRAGMA synchronous = OFF",  # no fsync: a slow disk must not slow the turns
            ]
            adapter = AsyncSQLiteAdapter(path, pragmas=pragmas, pool_size=pool_size)
            made.append(adapter)
            await adapter.connect()
            await adapter.execute("CREATE TABLE t (v TEXT NOT NULL UNIQUE)")
            await adapter.auto_commit()
            await adapter.execute("INSERT INTO t (v) VALUES ('dup')")
            await adapter.auto_commit()
            return adapter

        try:
            yield make
        finally:
            for adapter in made:
                await adapter.close()


@pytest_asyncio.fixture
async def loop_errors():
    """Collect exceptions that reach the test's event loop (a put on a full pool raises QueueFull there).

    Async, so it runs in the loop the test runs in and reads that loop.
    """
    loop = asyncio.get_running_loop()
    errors: list[dict] = []
    old = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    yield errors
    loop.set_exception_handler(old)


@pytest.mark.asyncio
async def test_loop_errors_sees_an_error_that_reaches_the_running_loop(loop_errors):
    asyncio.get_running_loop().call_exception_handler({"message": "probe"})
    assert [ctx["message"] for ctx in loop_errors] == ["probe"]


async def _write(adapter: AsyncSQLiteAdapter, value: str) -> None:
    await adapter.execute("INSERT INTO t (v) VALUES (?)", [value])
    await adapter.auto_commit()


async def _in_task(coro) -> object:
    return await asyncio.wait_for(asyncio.ensure_future(coro), BOUND_S)


async def _values(adapter: AsyncSQLiteAdapter) -> list[str]:
    async def read():
        cur = await adapter.execute("SELECT v FROM t ORDER BY rowid")
        rows = await cur.fetchall()
        await cur.close()
        await adapter.auto_commit()
        return [r[0] for r in rows]

    return await _in_task(read())


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _distinct_pooled(adapter: AsyncSQLiteAdapter) -> int:
    conns = [adapter._pool.get_nowait() for _ in range(adapter._pool.qsize())]
    for c in conns:
        adapter._pool.put_nowait(c)
    return len({id(c) for c in conns})


async def _fail_then_keep_running(adapter, fail, after_fail):
    """Run a task that fails once, records what it holds, waits, then writes again.

    ``fail`` is an async callable that must raise. Returns what the task saw.
    """
    failed = asyncio.Event()
    go_on = asyncio.Event()
    seen: dict = {}

    async def task():
        with pytest.raises(BaseException) as info:
            await fail()
        seen["error"] = info.value
        seen["connection_after_fail"] = adapter.connection
        failed.set()
        await go_on.wait()
        await after_fail()

    running = asyncio.ensure_future(task())
    await asyncio.wait_for(failed.wait(), BOUND_S)
    seen["qsize_while_failer_runs"] = adapter._pool.qsize()
    # another task gets the connection while the failing task is still running
    await _in_task(_write(adapter, "other"))
    go_on.set()
    await asyncio.wait_for(running, BOUND_S)
    return seen


# ---- item 2: a failed statement outside a transaction returns the connection ----


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(FAILING))
async def test_failed_statement_outside_a_transaction_returns_the_connection(
    make_adapter, loop_errors, case
):
    adapter = await make_adapter(pool_size=1)
    sql, params, error = FAILING[case]

    seen = await _fail_then_keep_running(
        adapter,
        lambda: adapter.execute(sql, params),
        lambda: _write(adapter, "same"),
    )

    assert isinstance(seen["error"], error)
    assert seen["connection_after_fail"] is None
    assert seen["qsize_while_failer_runs"] == 1
    assert await _values(adapter) == ["dup", "other", "same"]
    assert adapter._pool.qsize() == 1
    assert loop_errors == []


@pytest.mark.asyncio
async def test_failed_statement_rolls_back_the_tasks_uncommitted_write(make_adapter):
    adapter = await make_adapter(pool_size=1)

    async def write_then_fail():
        await adapter.execute("INSERT INTO t (v) VALUES ('uncommitted')")
        await adapter.execute("INSERT INTO t (v) VALUES ('dup')")

    seen = await _fail_then_keep_running(adapter, write_then_fail, lambda: _write(adapter, "same"))

    assert isinstance(seen["error"], sqlite3.IntegrityError)
    assert await _values(adapter) == ["dup", "other", "same"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(FAILING))
async def test_failed_statement_inside_a_transaction_keeps_the_pin_until_its_end(
    make_adapter, loop_errors, case
):
    adapter = await make_adapter(pool_size=1)
    sql, params, error = FAILING[case]
    seen: dict = {}

    async def txn():
        await adapter.begin_transaction()
        await adapter.execute("INSERT INTO t (v) VALUES ('in-txn')")
        pinned = adapter.connection
        with pytest.raises(error):
            await adapter.execute(sql, params)
        seen["same_pin"] = adapter.connection is pinned
        seen["qsize_in_txn"] = adapter._pool.qsize()
        await adapter.end_transaction(commit=False)
        seen["connection_after_end"] = adapter.connection

    await _in_task(txn())
    await _in_task(_write(adapter, "other"))

    assert seen == {"same_pin": True, "qsize_in_txn": 0, "connection_after_end": None}
    assert await _values(adapter) == ["dup", "other"]
    assert adapter._pool.qsize() == 1
    assert loop_errors == []


@pytest.fixture
def hang_execute(monkeypatch):
    """While ``state["on"]`` is true, ``Connection.execute`` sleeps before it sends the statement.

    So a cancellation lands before SQLite sees anything; the tests under "item 1" below
    cancel a statement that is really waiting in the SQLite thread.
    """
    real = aiosqlite.Connection.execute
    state = {"on": False}

    async def execute(self, sql, parameters=None):
        if state["on"]:
            await asyncio.sleep(60)
        return await real(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    return state


@pytest.mark.asyncio
async def test_statement_cancelled_by_wait_for_before_it_reaches_sqlite_returns_the_connection(
    make_adapter, hang_execute, loop_errors
):
    adapter = await make_adapter(pool_size=1)

    async def cancelled_statement():
        hang_execute["on"] = True
        try:
            # wait_for awaits the coroutine in this task, so the task survives the timeout
            await asyncio.wait_for(adapter.execute("INSERT INTO t (v) VALUES ('lost')"), 0.05)
        finally:
            hang_execute["on"] = False

    seen = await _fail_then_keep_running(
        adapter, cancelled_statement, lambda: _write(adapter, "same")
    )

    assert isinstance(seen["error"], asyncio.TimeoutError)
    assert seen["connection_after_fail"] is None
    assert await _values(adapter) == ["dup", "other", "same"]
    assert adapter._pool.qsize() == 1
    assert loop_errors == []


@pytest.mark.asyncio
async def test_commit_that_fails_returns_a_working_connection(make_adapter, loop_errors):
    adapter = await make_adapter(pool_size=1)

    async def write_then_break_the_commit():
        await adapter.execute("INSERT INTO t (v) VALUES ('lost')")
        await adapter.connection.close()  # the commit now fails
        await adapter.auto_commit()

    seen = await _fail_then_keep_running(
        adapter, write_then_break_the_commit, lambda: _write(adapter, "same")
    )

    assert isinstance(seen["error"], ValueError)
    assert seen["connection_after_fail"] is None
    assert await _values(adapter) == ["dup", "other", "same"]
    assert adapter._pool.qsize() == 1
    assert len(adapter._all_conns) == 1
    assert loop_errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_transaction_end_that_fails_returns_a_working_connection(
    make_adapter, loop_errors, commit
):
    adapter = await make_adapter(pool_size=1)
    seen: dict = {}

    async def txn():
        await adapter.begin_transaction()
        await adapter.execute("INSERT INTO t (v) VALUES ('lost')")
        await adapter.connection.close()  # the commit or rollback now fails
        try:
            await adapter.end_transaction(commit=commit)
        except Exception as exc:  # a failed commit is raised; a failed rollback is not
            seen["raised"] = type(exc).__name__
        seen["connection_after_end"] = adapter.connection
        seen["in_transaction"] = adapter.in_transaction

    await _in_task(txn())
    await _in_task(_write(adapter, "other"))

    expected = {"connection_after_end": None, "in_transaction": False}
    if commit:
        expected["raised"] = "ValueError"
    assert seen == expected
    assert await _values(adapter) == ["dup", "other"]
    assert adapter._pool.qsize() == 1
    assert len(adapter._all_conns) == 1
    assert loop_errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_type", [None, RuntimeError], ids=["commit", "rollback"])
async def test_context_manager_exit_that_fails_returns_a_working_connection(
    make_adapter, loop_errors, exc_type
):
    adapter = await make_adapter(pool_size=1)

    async def use():
        await adapter.__aenter__()
        await adapter.execute("INSERT INTO t (v) VALUES ('lost')")
        await adapter.connection.close()
        with pytest.raises(ValueError):
            await adapter.__aexit__(exc_type, None, None)
        return adapter.connection

    assert await _in_task(use()) is None
    await _in_task(_write(adapter, "other"))
    assert await _values(adapter) == ["dup", "other"]
    assert adapter._pool.qsize() == 1
    assert loop_errors == []


@pytest.mark.asyncio
async def test_begin_that_fails_leaves_the_task_holding_nothing(make_adapter, loop_errors):
    adapter = await make_adapter(pool_size=1)
    locker = sqlite3.connect(adapter.path, isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")  # another process holds the write lock

        failed = asyncio.Event()
        go_on = asyncio.Event()
        seen: dict = {}

        async def task():
            with pytest.raises(sqlite3.OperationalError):
                await adapter.begin_transaction()
            seen["connection"] = adapter.connection
            seen["in_transaction"] = adapter.in_transaction
            failed.set()
            await go_on.wait()
            await _write(adapter, "same")

        running = asyncio.ensure_future(task())
        await asyncio.wait_for(failed.wait(), BOUND_S)
        seen["qsize"] = adapter._pool.qsize()
        locker.execute("ROLLBACK")
        await _in_task(_write(adapter, "other"))
        go_on.set()
        await asyncio.wait_for(running, BOUND_S)
    finally:
        locker.close()

    assert seen == {"connection": None, "in_transaction": False, "qsize": 1}
    assert await _values(adapter) == ["dup", "other", "same"]
    assert loop_errors == []


@pytest.mark.asyncio
async def test_child_whose_statement_fails_drops_its_share_and_the_parent_returns_it_once(
    make_adapter, loop_errors
):
    adapter = await make_adapter(pool_size=1)
    child_failed = asyncio.Event()
    child_go_on = asyncio.Event()
    seen: dict = {}

    async def child():
        with pytest.raises(sqlite3.IntegrityError):
            await adapter.execute("INSERT INTO t (v) VALUES ('dup')")
        seen["child_connection"] = adapter.connection
        child_failed.set()
        await child_go_on.wait()
        await _write(adapter, "child")

    async def parent():
        await adapter.execute("INSERT INTO t (v) VALUES ('parent')")
        pinned = adapter.connection
        holder = asyncio.ensure_future(child())  # inherits the pin
        await asyncio.wait_for(child_failed.wait(), BOUND_S)
        seen["parent_kept_pin"] = adapter.connection is pinned
        await adapter.auto_commit()
        return holder

    holder = await _in_task(parent())
    await _settle()
    seen["qsize_after_parent_released"] = adapter._pool.qsize()  # the child is still running
    await _in_task(_write(adapter, "other"))
    child_go_on.set()
    await asyncio.wait_for(holder, BOUND_S)
    await _settle()

    assert seen == {
        "child_connection": None,
        "parent_kept_pin": True,
        "qsize_after_parent_released": 1,
    }
    assert await _values(adapter) == ["dup", "parent", "other", "child"]
    assert adapter._pool.qsize() == 1
    assert _distinct_pooled(adapter) == 1
    assert loop_errors == []


# ---- item 3: executemany and executescript ----


@pytest.mark.asyncio
async def test_executemany_inside_a_transaction_is_rolled_back(make_adapter):
    adapter = await make_adapter(pool_size=1)

    async def txn():
        await adapter.begin_transaction()
        await adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"]])
        await adapter.end_transaction(commit=False)

    await _in_task(txn())
    assert await _values(adapter) == ["dup"]
    assert adapter._pool.qsize() == 1


@pytest.mark.asyncio
async def test_executemany_outside_a_transaction_commits_and_returns_the_connection(make_adapter):
    adapter = await make_adapter(pool_size=1)

    async def many():
        await adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"]])
        return adapter.connection

    assert await _in_task(many()) is None
    await _in_task(_write(adapter, "other"))
    assert await _values(adapter) == ["dup", "a", "b", "other"]
    assert adapter._pool.qsize() == 1


@pytest.mark.asyncio
async def test_executemany_with_an_empty_list_returns_the_connection(make_adapter):
    adapter = await make_adapter(pool_size=1)

    async def many():
        await adapter.executemany("INSERT INTO t (v) VALUES (?)", [])
        return adapter.connection

    assert await _in_task(many()) is None
    assert adapter._pool.qsize() == 1
    assert await _values(adapter) == ["dup"]


@pytest.mark.asyncio
async def test_executemany_that_fails_outside_a_transaction_returns_the_connection(make_adapter):
    adapter = await make_adapter(pool_size=1)

    seen = await _fail_then_keep_running(
        adapter,
        lambda: adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["dup"]]),
        lambda: _write(adapter, "same"),
    )

    assert isinstance(seen["error"], sqlite3.IntegrityError)
    assert seen["connection_after_fail"] is None
    assert await _values(adapter) == ["dup", "other", "same"]  # 'a' was rolled back


@pytest.mark.asyncio
async def test_executescript_outside_a_transaction_commits_and_returns_the_connection(make_adapter):
    adapter = await make_adapter(pool_size=1)

    async def script():
        await adapter.executescript(
            "INSERT INTO t (v) VALUES ('a'); INSERT INTO t (v) VALUES ('b');"
        )
        return adapter.connection

    assert await _in_task(script()) is None
    await _in_task(_write(adapter, "other"))
    assert await _values(adapter) == ["dup", "a", "b", "other"]
    assert adapter._pool.qsize() == 1


@pytest.mark.asyncio
async def test_executescript_that_fails_outside_a_transaction_returns_the_connection(make_adapter):
    adapter = await make_adapter(pool_size=1)

    seen = await _fail_then_keep_running(
        adapter,
        lambda: adapter.executescript("INSERT INTO t (v) VALUES ('dup');"),
        lambda: _write(adapter, "same"),
    )

    assert isinstance(seen["error"], sqlite3.IntegrityError)
    assert seen["connection_after_fail"] is None
    assert await _values(adapter) == ["dup", "other", "same"]


@pytest.mark.asyncio
async def test_executescript_inside_a_transaction_raises_and_commits_nothing(make_adapter):
    adapter = await make_adapter(pool_size=1)
    seen: dict = {}

    async def txn():
        await adapter.begin_transaction()
        await adapter.execute("INSERT INTO t (v) VALUES ('in-txn')")
        with pytest.raises(QueryError) as info:
            await adapter.executescript("INSERT INTO t (v) VALUES ('script');")
        seen["message"] = str(info.value)
        seen["in_transaction"] = adapter.in_transaction
        await adapter.end_transaction(commit=False)

    await _in_task(txn())
    assert seen == {
        "message": "executescript cannot run inside a transaction: "
        "SQLite commits the open transaction before it runs a script",
        "in_transaction": True,
    }
    assert await _values(adapter) == ["dup"]
    assert adapter._pool.qsize() == 1


# ---- WS-R fixes item 1: a cancellation of a statement running in the SQLite thread ----
#
# A second connection holds a lock, so the statement really waits in aiosqlite's
# thread (on the 5 s busy timeout). The cancellation must reach the caller at
# once; the connection comes back by itself once the statement ends there.


@pytest.fixture
def sent(monkeypatch):
    """Record (connection, sqlite3 method name) for every call aiosqlite hands to its thread."""
    real = aiosqlite.Connection._execute
    calls: list[tuple[aiosqlite.Connection, str]] = []

    async def _execute(self, fn, *args, **kwargs):
        calls.append((self, getattr(fn, "__name__", repr(fn))))
        return await real(self, fn, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "_execute", _execute)
    return calls


@pytest.fixture
def lock_holder():
    """Open a plain sqlite3 connection that holds the write lock or a read lock until released."""
    held: list[sqlite3.Connection] = []

    def hold(path: str, kind: str = "write") -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None)
        if kind == "write":
            conn.execute("BEGIN IMMEDIATE")
        else:  # a read transaction: in rollback-journal mode it keeps a commit from finishing
            conn.execute("BEGIN")
            conn.execute("SELECT count(*) FROM t").fetchall()
        held.append(conn)
        return conn

    yield hold
    for conn in held:  # released before the adapter fixture closes the adapter
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


async def _until(condition) -> None:
    """Wait (bounded) until ``condition()`` is true."""

    async def poll():
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), BOUND_S)


async def _in_sqlite_thread(sent: list, since: int, op: str, count: int = 1) -> None:
    """Wait until ``count`` ``op`` calls made after ``sent[since]`` have been taken by their thread."""
    await _until(
        lambda: sum(1 for conn, name in sent[since:] if name == op and conn._tx.empty()) >= count
    )


async def _cancel_in_flight(adapter, sent, op: str, run, how: str) -> tuple[float, dict]:
    """Start ``run()`` in a task, wait until its ``op`` is in the SQLite thread, then cancel it.

    how="timeout": an asyncio timeout runs out inside the task (what ``wait_for`` does);
    the task records what it holds after the TimeoutError.
    how="cancel": ``task.cancel()``.
    Returns the seconds from the cancellation to the task having ended, and what it recorded.
    """
    since = len(sent)
    seen: dict = {}
    scope: dict = {}

    async def body():
        if how == "cancel":
            await run()
            return
        try:
            async with asyncio.timeout(None) as cm:
                scope["cm"] = cm
                await run()
        except TimeoutError:
            seen["connection"] = adapter.connection
            seen["in_transaction"] = adapter.in_transaction
            raise

    task = asyncio.ensure_future(body())
    await _in_sqlite_thread(sent, since, op)
    start = time.perf_counter()
    if how == "cancel":
        task.cancel()
    else:
        scope["cm"].reschedule(asyncio.get_running_loop().time())
    await asyncio.wait({task}, timeout=BOUND_S)
    elapsed = time.perf_counter() - start
    assert task.done(), f"the cancellation had not reached the caller after {BOUND_S} s"
    with pytest.raises(asyncio.CancelledError if how == "cancel" else TimeoutError):
        task.result()
    return elapsed, seen


async def _pool_whole(adapter: AsyncSQLiteAdapter) -> None:
    await _until(lambda: adapter._pool.qsize() == adapter._pool_size)


async def _begin_insert_end(adapter: AsyncSQLiteAdapter) -> None:
    await adapter.begin_transaction()
    await adapter.execute("INSERT INTO t (v) VALUES ('late')")
    await adapter.end_transaction(commit=True)


# (what runs, the sqlite3 call the cancellation lands in, lock kind, journal mode, rows at the end)
# A statement or BEGIN that was waiting is rolled back once it ends. A commit or a script that
# SQLite was already running finishes once the lock is free: the caller saw the cancellation,
# but the work it had sent is done.
IN_FLIGHT = {
    "execute": (
        lambda a: a.execute("INSERT INTO t (v) VALUES ('lost')"),
        "execute", "write", "WAL", ["dup"],
    ),
    "begin_immediate": (lambda a: a.begin_transaction(), "execute", "write", "WAL", ["dup"]),
    "executemany": (
        lambda a: a.executemany("INSERT INTO t (v) VALUES (?)", [["m1"], ["m2"]]),
        "executemany", "write", "WAL", ["dup"],
    ),
    "executescript": (
        lambda a: a.executescript(
            "INSERT INTO t (v) VALUES ('s1'); INSERT INTO t (v) VALUES ('s2');"
        ),
        "executescript", "write", "WAL", ["dup", "s1", "s2"],
    ),
    "auto_commit": (
        lambda a: _write(a, "late"), "commit", "read", "DELETE", ["dup", "late"],
    ),
    "end_transaction": (_begin_insert_end, "commit", "read", "DELETE", ["dup", "late"]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["timeout", "cancel"])
@pytest.mark.parametrize("case", list(IN_FLIGHT))
async def test_cancellation_in_the_sqlite_thread_reaches_the_caller_at_once(
    make_adapter, lock_holder, sent, loop_errors, case, how
):
    run, op, lock, journal, rows = IN_FLIGHT[case]
    adapter = await make_adapter(pool_size=1, busy_ms=BUSY_MS, journal=journal)
    holder = lock_holder(adapter.path, lock)

    elapsed, seen = await _cancel_in_flight(adapter, sent, op, lambda: run(adapter), how)
    qsize_while_sqlite_runs = adapter._pool.qsize()
    holder.execute("ROLLBACK")  # the lock holder ends; the statement finishes in its thread
    await _pool_whole(adapter)

    assert elapsed < CANCEL_DELIVERED_S
    if how == "timeout":
        assert seen == {"connection": None, "in_transaction": False}
    assert qsize_while_sqlite_runs == 0  # it comes back once, from the SQLite thread
    assert await _values(adapter) == rows
    await _in_task(_write(adapter, "after"))  # the connection works as before
    assert await _values(adapter) == rows + ["after"]
    assert adapter._pool.qsize() == 1
    assert _distinct_pooled(adapter) == 1
    assert loop_errors == []


@pytest.mark.asyncio
async def test_second_cancellation_while_the_connection_is_on_its_way_back(
    make_adapter, lock_holder, sent, loop_errors
):
    adapter = await make_adapter(pool_size=1, busy_ms=BUSY_MS)
    holder = lock_holder(adapter.path)

    elapsed, _ = await _cancel_in_flight(
        adapter, sent, "execute", lambda: adapter.execute("INSERT INTO t (v) VALUES ('lost')"),
        "timeout",
    )
    # a second task now waits for the one connection, which the SQLite thread still has
    second = asyncio.ensure_future(_write(adapter, "second"))
    await _until(lambda: adapter._waiting == 1)
    second.cancel()
    await asyncio.wait({second}, timeout=BOUND_S)
    waiting_after_cancel = adapter._waiting
    holder.execute("ROLLBACK")
    await _pool_whole(adapter)
    stats = adapter.pool_stats()

    assert elapsed < CANCEL_DELIVERED_S
    assert second.cancelled() is True
    assert waiting_after_cancel == 0
    assert (stats.available_connections, stats.in_use_connections) == (1, 0)
    assert await _values(adapter) == ["dup"]
    assert _distinct_pooled(adapter) == 1
    assert loop_errors == []


@pytest.mark.asyncio
async def test_twenty_tasks_on_a_pool_of_two_half_cancelled_while_a_lock_is_held(
    make_adapter, lock_holder, sent, loop_errors
):
    adapter = await make_adapter(pool_size=2, busy_ms=BUSY_MS)
    holder = lock_holder(adapter.path)
    since = len(sent)

    tasks = [asyncio.ensure_future(_write(adapter, f"v{i:02d}")) for i in range(20)]
    # tasks 0 and 1 hold the two connections, their statements waiting in the SQLite threads
    await _in_sqlite_thread(sent, since, "execute", count=2)
    await _until(lambda: adapter._waiting == 18)
    cancelled = {0, 1, 2, 4, 6, 8, 10, 12, 14, 16}  # the two mid-statement and eight waiters
    start = time.perf_counter()
    for i in cancelled:
        tasks[i].cancel()
    await asyncio.wait([tasks[i] for i in cancelled], timeout=BOUND_S)
    elapsed = time.perf_counter() - start
    holder.execute("ROLLBACK")
    kept = [tasks[i] for i in range(20) if i not in cancelled]
    await asyncio.wait_for(asyncio.gather(*kept), BOUND_S)
    await _pool_whole(adapter)

    assert elapsed < CANCEL_DELIVERED_S
    assert sorted(i for i in range(20) if tasks[i].cancelled()) == sorted(cancelled)
    assert sorted(await _values(adapter)) == sorted(
        ["dup"] + [f"v{i:02d}" for i in range(20) if i not in cancelled]
    )
    assert adapter._pool.qsize() == 2
    assert _distinct_pooled(adapter) == 2
    assert adapter._waiting == 0
    assert loop_errors == []


@pytest.mark.asyncio
async def test_close_while_a_cancelled_statements_connection_is_on_its_way_back(
    make_adapter, lock_holder, sent, loop_errors
):
    adapter = await make_adapter(pool_size=1, busy_ms=BUSY_MS)
    holder = lock_holder(adapter.path)

    elapsed, _ = await _cancel_in_flight(
        adapter, sent, "execute", lambda: adapter.execute("INSERT INTO t (v) VALUES ('lost')"),
        "cancel",
    )
    closing = asyncio.ensure_future(adapter.close())
    await _settle()
    closed_before_the_lock_ended = closing.done()  # close waits for the statement in the thread
    holder.execute("ROLLBACK")
    await asyncio.wait_for(closing, BOUND_S)

    check = sqlite3.connect(adapter.path)
    try:
        rows = [r[0] for r in check.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        check.close()
    assert elapsed < CANCEL_DELIVERED_S
    assert closed_before_the_lock_ended is False
    assert adapter._all_conns == []
    assert adapter._pool.qsize() == 0
    assert rows == ["dup"]
    assert loop_errors == []
