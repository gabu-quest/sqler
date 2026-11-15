"""An async cursor returns only what its statement saw while the connection was its own.

``aiosqlite`` steps a statement when its rows are fetched. Once another context sends a
statement on the cursor's connection (a task that took it from the pool, a child task that
shares the pin, a sibling in a shared transaction, the rollback that ends a transaction),
a lazy fetch would see that context's work. The rows a live cursor has not read yet are
read first, in the connection's thread before that statement, and served from memory.
While only the cursor's own task uses the connection it reads lazily, as SQLite does.

Every wait is bounded, so a hang fails instead of stalling the suite.
"""

import asyncio
import gc
import sqlite3
import threading
import time

import aiosqlite
import pytest
import pytest_asyncio
from sqler.adapter.asynchronous import (
    _AIOSQLITE_CALLS,
    _AIOSQLITE_PRIVATE,
    AsyncSQLiteAdapter,
    _Cursor,
    _in_thread,
    _InThread,
)
from sqler.exceptions import ConnectionError as SQLerConnectionError
from sqler.exceptions import NotConnectedError
from sqler.logging import query_logger

pytestmark = pytest.mark.asyncio

N = 2000
FIVE_MILLION = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5000000) SELECT x FROM c"
)


@pytest_asyncio.fixture(params=["in_memory", "on_disk"])
async def ad(request, tmp_path):
    adapter = (
        AsyncSQLiteAdapter.in_memory(acquire_timeout=10)
        if request.param == "in_memory"
        else AsyncSQLiteAdapter.on_disk(str(tmp_path / "h.db"), acquire_timeout=10)
    )
    await adapter.connect()
    await adapter.execute("CREATE TABLE t (n INTEGER, v TEXT)")
    await adapter.executemany("INSERT INTO t VALUES (?, 'clean')", [(n,) for n in range(N)])
    await adapter.auto_commit()
    try:
        yield adapter
    finally:
        await asyncio.wait_for(adapter.close(), 10)


@pytest_asyncio.fixture
async def mem():
    adapter = AsyncSQLiteAdapter.in_memory(acquire_timeout=10)
    await adapter.connect()
    await adapter.execute("CREATE TABLE t (n INTEGER, v TEXT)")
    await adapter.executemany("INSERT INTO t VALUES (?, 'clean')", [(n,) for n in range(N)])
    await adapter.auto_commit()
    try:
        yield adapter
    finally:
        await asyncio.wait_for(adapter.close(), 10)


@pytest.fixture
def read_ahead(monkeypatch):
    """How many rows each read ahead read in the connection's thread, in the order they ran."""
    counts: list = []
    real = _InThread.read_rest

    def counted(db, cursor):
        rows, error = real(db, cursor)
        counts.append(len(rows))
        return rows, error

    monkeypatch.setattr(_InThread, "read_rest", staticmethod(counted))
    return counts


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


def _tally(rows) -> str:
    values = [r[1] for r in rows]
    return f"{len(values)} rows; {values.count('clean')} clean, {values.count('dirty')} dirty"


async def _whole(adapter: AsyncSQLiteAdapter) -> None:
    for _ in range(1000):  # a reclaim may still be running
        if adapter.pool_stats().in_use_connections == 0:
            return
        await asyncio.sleep(0.01)
    assert adapter.pool_stats().in_use_connections == 0


async def _dirty_writer(adapter: AsyncSQLiteAdapter, inside: asyncio.Event, go: asyncio.Event):
    """Begin a transaction, insert N dirty rows, wait for ``go``, roll back."""
    await adapter.begin_transaction()
    try:
        await adapter.executemany("INSERT INTO t VALUES (?, 'dirty')", [(n,) for n in range(N)])
        inside.set()
        await go.wait()
    finally:
        await adapter.end_transaction(commit=False)


async def _read_while_another_context_writes(adapter, cur, read):
    inside, go = asyncio.Event(), asyncio.Event()
    writer = asyncio.create_task(_dirty_writer(adapter, inside, go))
    try:
        await asyncio.wait_for(inside.wait(), 5)
        return await asyncio.wait_for(read(cur), 10)
    finally:
        go.set()
        await asyncio.wait_for(writer, 10)


# --- the six cells of the measured gap -------------------------------------------------


@pytest.mark.parametrize("how", ["its transaction ended", "auto_commit", "still pinned"])
async def test_a_cursor_read_while_another_context_writes_on_its_connection_returns_its_statements_rows(ad, how):
    if how == "its transaction ended":
        await ad.begin_transaction()
        cur = await ad.execute("SELECT n, v FROM t")
        await ad.end_transaction(commit=True)
    else:
        cur = await ad.execute("SELECT n, v FROM t")
        if how == "auto_commit":
            await ad.auto_commit()
    got = await _read_while_another_context_writes(ad, cur, lambda c: c.fetchall())
    assert _tally(got) == "2000 rows; 2000 clean, 0 dirty"
    await ad.auto_commit()
    await _whole(ad)


async def test_a_cursor_that_read_10_rows_gets_the_other_1990_clean_and_in_order(mem):
    cur = await mem.execute("SELECT n, v FROM t ORDER BY rowid")
    first = await cur.fetchmany(10)
    await mem.auto_commit()
    rest = await _read_while_another_context_writes(mem, cur, lambda c: c.fetchall())
    assert ([r[0] for r in first + rest], _tally(rest)) == (list(range(N)), "1990 rows; 1990 clean, 0 dirty")


async def test_rows_the_cursors_own_transaction_wrote_and_rolled_back_are_served(mem):
    await mem.begin_transaction()
    await mem.executemany("INSERT INTO t VALUES (?, 'mine')", [(n,) for n in range(5)])
    cur = await mem.execute("SELECT n, v FROM t")
    await mem.end_transaction(commit=False)
    values = [r[1] for r in await asyncio.wait_for(cur.fetchall(), 10)]
    assert (len(values), values.count("clean"), values.count("mine")) == (N + 5, N, 5)
    count = await mem.execute("SELECT count(*) FROM t")
    assert (await count.fetchone())[0] == N
    await mem.auto_commit()


# --- hostile inputs ----------------------------------------------------------------------


async def test_a_child_task_that_drops_the_table_leaves_the_parents_cursor_its_rows(mem):
    cur = await mem.execute("SELECT n, v FROM t")

    async def drop():
        await mem.execute("DROP TABLE t")

    await asyncio.wait_for(asyncio.create_task(drop()), 10)
    got = await asyncio.wait_for(cur.fetchall(), 10)
    await mem.auto_commit()
    tables = await mem.execute("SELECT count(*) FROM sqlite_master WHERE name = 't'")
    assert (_tally(got), (await tables.fetchone())[0]) == ("2000 rows; 2000 clean, 0 dirty", 0)
    await mem.auto_commit()


async def test_two_cursors_on_one_lease_one_half_read_both_keep_their_rows(mem):
    half = await mem.execute("SELECT n, v FROM t ORDER BY rowid")
    unread = await mem.execute("SELECT n, v FROM t ORDER BY rowid DESC")
    first = await half.fetchmany(N // 2)
    await mem.auto_commit()

    async def both(_):
        return await half.fetchall(), await unread.fetchall()

    rest, other = await _read_while_another_context_writes(mem, None, both)
    assert [r[0] for r in first + rest] == list(range(N))
    assert ([r[0] for r in other], _tally(other)) == (list(range(N - 1, -1, -1)), "2000 rows; 2000 clean, 0 dirty")


async def test_the_cursor_of_an_insert_has_nothing_to_read_after_a_hand_over(mem):
    cur = await mem.execute("INSERT INTO t VALUES (?, 'clean')", [N])
    lastrowid = cur.lastrowid
    await mem.auto_commit()
    got = await _read_while_another_context_writes(mem, cur, lambda c: c.fetchall())
    assert (got, cur.lastrowid, cur.rowcount, cur.description) == ([], lastrowid, 1, None)


async def test_async_for_left_by_break_then_the_connection_goes_back(mem):
    cur = await mem.execute("SELECT n, v FROM t ORDER BY rowid")
    async for row in cur:
        first = row
        break
    await mem.auto_commit()
    rest = await _read_while_another_context_writes(mem, cur, lambda c: c.fetchall())
    # aiosqlite's iteration fetches iter_chunk_size rows at a time: the rest of that chunk went with the loop
    chunk = cur.iter_chunk_size
    assert (first[0], [r[0] for r in rest]) == (0, list(range(chunk, N)))
    assert _tally(rest) == f"{N - chunk} rows; {N - chunk} clean, 0 dirty"


async def test_a_child_task_that_shares_the_pin_and_only_reads(mem):
    cur = await mem.execute("SELECT n, v FROM t ORDER BY rowid")

    async def child():
        c = await mem.execute("SELECT count(*) FROM t WHERE v = 'clean'")
        return (await c.fetchone())[0]

    counted = await asyncio.wait_for(asyncio.create_task(child()), 10)
    got = await asyncio.wait_for(cur.fetchall(), 10)
    await mem.auto_commit()
    assert (counted, [r[0] for r in got], _tally(got)) == (N, list(range(N)), "2000 rows; 2000 clean, 0 dirty")
    await _whole(mem)


async def test_twenty_tasks_sharing_one_transaction_each_with_an_unread_cursor(ad):
    await ad.begin_transaction()
    opened = 0
    all_open = asyncio.Event()
    written = asyncio.Event()

    async def reader():
        nonlocal opened
        cur = await ad.execute("SELECT n, v FROM t")
        opened += 1
        if opened == 20:
            all_open.set()
        await written.wait()
        return await cur.fetchall()

    readers = [asyncio.create_task(reader()) for _ in range(20)]
    await asyncio.wait_for(all_open.wait(), 10)
    await ad.executemany("INSERT INTO t VALUES (?, 'dirty')", [(n,) for n in range(N)])
    written.set()
    got = await asyncio.wait_for(asyncio.gather(*readers), 20)
    await ad.end_transaction(commit=False)
    assert [_tally(rows) for rows in got] == ["2000 rows; 2000 clean, 0 dirty"] * 20
    await _whole(ad)


async def test_closing_the_adapter_with_an_unread_cursor_alive_raises_not_connected_on_its_read(mem):
    cur = await mem.execute("SELECT n, v FROM t")
    await asyncio.wait_for(mem.close(), 10)
    with pytest.raises(NotConnectedError, match="closed"):
        await asyncio.wait_for(cur.fetchall(), 5)
    with pytest.raises(NotConnectedError, match="closed"):
        await asyncio.wait_for(cur.fetchone(), 5)


async def test_a_cursor_read_from_another_task_than_the_one_that_made_it(mem):
    cur = await mem.execute("SELECT n, v FROM t ORDER BY rowid")
    await mem.auto_commit()

    async def read_elsewhere(c):
        return await asyncio.create_task(c.fetchall())

    got = await _read_while_another_context_writes(mem, cur, read_elsewhere)
    assert ([r[0] for r in got], _tally(got)) == (list(range(N)), "2000 rows; 2000 clean, 0 dirty")


# --- item 2: a task alone on its connection reads lazily ---------------------------------


async def test_a_row_the_same_task_inserts_before_the_first_fetch_is_in_what_the_cursor_returns(mem):
    cur = await mem.execute("SELECT n, v FROM t ORDER BY rowid")
    await mem.execute("INSERT INTO t VALUES (?, 'later')", [N])
    got = await asyncio.wait_for(cur.fetchall(), 10)
    await mem.auto_commit()
    assert ([r[0] for r in got], got[-1][1]) == (list(range(N + 1)), "later")


async def test_fetchmany_10_of_five_million_rows_returns_at_once(mem):
    start = time.perf_counter()
    cur = await mem.execute(FIVE_MILLION)
    rows = await cur.fetchmany(10)
    elapsed = time.perf_counter() - start
    await cur.close()
    await mem.auto_commit()
    assert ([r[0] for r in rows], elapsed < 1.0) == (list(range(1, 11)), True)


# --- item 3: a cursor nobody holds costs nothing -----------------------------------------


@pytest.mark.parametrize("let_go", ["del", "close"])
async def test_a_cursor_let_go_is_not_read_when_its_connection_changes_hands(mem, let_go):
    cur = await mem.execute(FIVE_MILLION)
    assert len(await cur.fetchmany(10)) == 10
    if let_go == "del":
        del cur
        gc.collect()
    else:
        await cur.close()
    start = time.perf_counter()
    await mem.auto_commit()
    other = await asyncio.wait_for(asyncio.create_task(mem.execute("SELECT count(*) FROM t")), 10)
    count = (await other.fetchone())[0]
    await mem.auto_commit()
    elapsed = time.perf_counter() - start
    assert (count, elapsed < 0.5) == (N, True)


async def test_a_cursor_dropped_before_any_fetch_is_not_read_when_its_connection_changes_hands(mem, read_ahead):
    await mem.execute(FIVE_MILLION)  # the cursor is dropped at once: nobody can read its rows
    other = await asyncio.wait_for(asyncio.create_task(mem.execute("SELECT count(*) FROM t")), 30)
    count = (await other.fetchall())[0][0]
    await mem.auto_commit()
    assert (count, read_ahead) == (N, [])


# --- item 4: a cursor serving rows from memory answers as before -------------------------

STATEMENTS = {
    "rows": "SELECT n, v FROM t WHERE n < 10 ORDER BY rowid",
    "no-row": "SELECT n, v FROM t WHERE n < 0",
    "pragma": "PRAGMA table_info(t)",
    "returning": "INSERT INTO t (n, v) VALUES (-1, 'r1'), (-2, 'r2') RETURNING n, v",
    "plain-insert": "INSERT INTO t (n, v) VALUES (-3, 'p')",
}


async def _observe(adapter: AsyncSQLiteAdapter, sql: str, *, hand_over: bool) -> dict:
    cur = await adapter.execute(sql)
    cur.arraysize = 2
    if hand_over:  # a child task sharing the pin sends a statement: the rows are read first
        await asyncio.create_task(adapter.execute("UPDATE t SET v = 'changed' WHERE n >= 0"))
    seen = {"description": cur.description, "arraysize": cur.arraysize}
    seen["first"] = None if (row := await cur.fetchone()) is None else tuple(row)
    seen["two"] = [tuple(r) for r in await cur.fetchmany()]
    seen["three"] = [tuple(r) for r in await cur.fetchmany(3)]
    seen["rest"] = [tuple(r) async for r in cur]
    seen["after_last"] = await cur.fetchone()
    seen["fetchall_after"] = await cur.fetchall()
    seen["fetchmany_after"] = await cur.fetchmany(5)
    seen["rowcount"] = cur.rowcount
    seen["lastrowid"] = cur.lastrowid
    seen["row_factory"] = cur.row_factory
    seen["connection"] = cur.connection is adapter.connection._conn
    async with cur:
        pass
    with pytest.raises(sqlite3.ProgrammingError, match="closed cursor") as raised:
        await cur.fetchall()
    seen["after_close"] = str(raised.value)
    await adapter.end_transaction(commit=False)
    return seen


@pytest.mark.parametrize("statement", list(STATEMENTS))
async def test_a_cursor_read_ahead_answers_what_a_lazy_cursor_answers(statement, tmp_path):
    seen = {}
    for hand_over in (False, True):
        adapter = AsyncSQLiteAdapter.on_disk(str(tmp_path / f"o{hand_over}.db"), acquire_timeout=10)
        await adapter.connect()
        try:
            await adapter.execute("CREATE TABLE t (n INTEGER, v TEXT)")
            await adapter.executemany("INSERT INTO t VALUES (?, ?)", [(n, f"v{n}") for n in range(10)])
            await adapter.auto_commit()
            await adapter.begin_transaction()
            seen[hand_over] = await _observe(adapter, STATEMENTS[statement], hand_over=hand_over)
        finally:
            await adapter.close()
    assert seen[True] == seen[False]
    got = seen[True]
    served = ([got["first"]] if got["first"] is not None else []) + got["two"] + got["three"] + got["rest"]
    expected = {
        "rows": [(n, f"v{n}") for n in range(10)],
        "no-row": [],
        "pragma": [(0, "n", "INTEGER", 0, None, 0), (1, "v", "TEXT", 0, None, 0)],
        "returning": [(-1, "r1"), (-2, "r2")],
        "plain-insert": [],
    }[statement]
    assert (served, len(got["two"]), got["connection"]) == (expected, min(2, max(len(expected) - 1, 0)), True)


# --- items 5 and 6: rows that cannot be read; the read ahead cannot break the hand-over -----


async def _six_fetchones(cur) -> list:
    seen = []
    for _ in range(6):
        try:
            row = await asyncio.wait_for(cur.fetchone(), 5)
            seen.append(None if row is None else row[0])
        except sqlite3.OperationalError as exc:
            seen.append(f"raised {exc}")
    return seen


@pytest.mark.parametrize("hand_over", [False, True], ids=["read-lazily", "read-ahead"])
async def test_a_statement_whose_fifth_row_raises_raises_at_the_read_that_reaches_it(mem, hand_over):
    await mem.execute("CREATE TABLE j (v TEXT)")
    await mem.executemany("INSERT INTO j VALUES (?)", [('{"a": 1}',)] * 4 + [("{bad",)] + [('{"a": 2}',)] * 2)
    await mem.auto_commit()
    cur = await mem.execute("SELECT json(v) FROM j ORDER BY rowid")
    await mem.auto_commit()  # the connection goes back unread
    if hand_over:
        seen = await _read_while_another_context_writes(mem, cur, _six_fetchones)
    else:
        seen = await _six_fetchones(cur)
    await _whole(mem)
    count = await mem.execute("SELECT count(*) FROM t")
    # sqlite3 steps to the next row when it returns one: the fourth read steps the fifth row
    assert (seen, (await count.fetchone())[0]) == (['{"a":1}'] * 3 + ["raised malformed JSON", None, None], N)
    await mem.auto_commit()


@pytest.mark.parametrize("share", ["transaction", "pin"])
async def test_a_statement_cancelled_while_the_read_ahead_it_triggered_runs_keeps_the_cursors_rows(mem, share):
    million = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 1000000) SELECT x FROM c"
    if share == "transaction":
        await mem.begin_transaction()
    cur = await mem.execute(million)
    first = await cur.fetchone()
    other = asyncio.create_task(mem.execute("INSERT INTO t VALUES (-1, 'dirty')"))
    await asyncio.sleep(0)  # one turn: the other context's step has queued the read ahead, then its INSERT
    reading = cur._taking is not None and not cur._taking.done()
    other.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(other, 10)
    got = await asyncio.wait_for(cur.fetchall(), 20)
    if share == "transaction":
        await mem.end_transaction(commit=False)
    else:
        await mem.auto_commit()
    await _whole(mem)
    assert (reading, first[0], [r[0] for r in got]) == (True, 1, list(range(2, 1000001)))
    assert (mem.pool_stats().available_connections, mem.pool_stats().total_connections) == (1, 1)
    await asyncio.wait_for(mem.close(), 10)


async def test_a_statement_cancelled_inside_a_transaction_leaves_its_tasks_other_cursor_its_rows(mem):
    slow = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 8000000) "
        "SELECT count(*) FROM c"
    )
    await mem.begin_transaction()
    await mem.execute("INSERT INTO t VALUES (?, 'mine')", [N])
    cur = await mem.execute("SELECT n, v FROM t ORDER BY rowid")
    running = asyncio.create_task(mem.execute(slow))
    await asyncio.sleep(0.05)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await mem.end_transaction(commit=False)  # the connection goes to the reclaim
    got = await asyncio.wait_for(cur.fetchall(), 20)
    await _whole(mem)
    assert ([r[0] for r in got], got[-1][1]) == (list(range(N + 1)), "mine")


# --- cost: execute then fetchall sends what it sent before ---------------------------------


async def test_execute_then_fetchall_sends_one_statement_and_reads_nothing_ahead(mem, read_ahead, trips):
    with query_logger.capture() as captured:
        for _ in range(3):
            cur = await mem.execute("SELECT n FROM t WHERE n < 3")
            rows = await cur.fetchall()
            await mem.auto_commit()
    assert ([q.sql for q in captured], [r[0] for r in rows]) == (["SELECT n FROM t WHERE n < 3"] * 3, [0, 1, 2])
    # the trips to the connection's thread: the statement and the read, one each as aiosqlite's
    # own execute and fetchall make them, then auto_commit's commit
    assert (trips, read_ahead) == (["execute", "fetch", "commit"] * 3, [])


# --- two statements sent at the same moment on one connection ------------------------------

WRITES = {
    "insert": ("executemany", "INSERT INTO t VALUES (?, 'dirty')", [(n,) for n in range(N, 2 * N)]),
    "update": ("execute", "UPDATE t SET v = 'dirty'", None),
    "delete": ("execute", "DELETE FROM t", None),
}
# what a reader whose statement is handed to the connection after the write gets
AFTER_WRITE = {
    "insert": "4000 rows; 2000 clean, 2000 dirty",
    "update": "2000 rows; 0 clean, 2000 dirty",
    "delete": "0 rows; 0 clean, 0 dirty",
    "script": "2000 rows; 0 clean, 2000 dirty",
}
BEFORE_WRITE = "2000 rows; 2000 clean, 0 dirty"


async def _share(adapter: AsyncSQLiteAdapter, share: str) -> None:
    """Let the tasks this one starts share its connection: a transaction, or a pin with nothing to commit."""
    if share == "transaction":
        await adapter.begin_transaction()
    else:
        await adapter.execute("SELECT 1")  # pins the connection, no transaction


async def _unshare(adapter: AsyncSQLiteAdapter, share: str) -> None:
    if share == "transaction":
        await adapter.end_transaction(commit=False)
    else:
        await adapter.auto_commit()


async def _write(adapter: AsyncSQLiteAdapter, write: str) -> None:
    if write == "script":
        await adapter.executescript("UPDATE t SET v = 'dirty';")
        return
    how, sql, params = WRITES[write]
    await getattr(adapter, how)(sql, params) if params is not None else await adapter.execute(sql)


async def _reader(adapter: AsyncSQLiteAdapter, turns: int) -> list:
    cur = await adapter.execute("SELECT n, v FROM t ORDER BY rowid")  # no sort: rows are stepped as fetched
    for _ in range(turns):
        await asyncio.sleep(0)  # give the loop turns: no wait on anything
    return await cur.fetchall()


SHARES = ["transaction", "pin"]


@pytest.mark.parametrize("turns", [0, 1, 5], ids=["reads-at-once", "one-turn", "five-turns"])
@pytest.mark.parametrize(
    "share,write",
    # executescript raises QueryError inside a transaction: a script is sent on a shared pin only
    [(share, write) for share in SHARES for write in ("insert", "update", "delete")] + [("pin", "script")],
)
async def test_a_select_handed_to_the_connection_before_a_siblings_write_is_answered_whole(mem, share, write, turns):
    await _share(mem, share)
    got, _ = await asyncio.wait_for(asyncio.gather(_reader(mem, turns), _write(mem, write)), 20)
    await _unshare(mem, share)
    assert (_tally(got), [r[0] for r in got] == list(range(N))) == (BEFORE_WRITE, True)
    assert sum(len(cursors) for cursors in mem._unread.values()) == 0
    await _whole(mem)


@pytest.mark.parametrize("write", ["insert", "update", "delete"])
@pytest.mark.parametrize("share", SHARES)
async def test_a_select_handed_to_the_connection_after_a_siblings_write_gets_what_the_write_left(mem, share, write):
    await _share(mem, share)
    _, got = await asyncio.wait_for(asyncio.gather(_write(mem, write), _reader(mem, 1)), 20)
    await _unshare(mem, share)
    assert _tally(got) == AFTER_WRITE[write]
    await _whole(mem)


@pytest.mark.parametrize("share", SHARES)
async def test_twenty_readers_and_one_writer_started_together_each_get_one_whole_answer(mem, share):
    await _share(mem, share)
    readers = [_reader(mem, 1) for _ in range(20)]
    results = await asyncio.wait_for(asyncio.gather(*readers[:10], _write(mem, "update"), *readers[10:]), 30)
    await _unshare(mem, share)
    got = [_tally(rows) for rows in results[:10] + results[11:]]
    assert got == [BEFORE_WRITE] * 10 + [AFTER_WRITE["update"]] * 10
    await _whole(mem)


@pytest.mark.parametrize("share", SHARES)
async def test_two_readers_and_no_writer_read_the_same_rows(mem, share):
    await _share(mem, share)
    one, two = await asyncio.wait_for(asyncio.gather(_reader(mem, 1), _reader(mem, 1)), 20)
    await _unshare(mem, share)
    assert (_tally(one), one == two) == (BEFORE_WRITE, True)


@pytest.mark.parametrize("share", SHARES)
async def test_a_reader_whose_statement_fails_while_a_write_is_queued_behind_it_leaves_no_cursor(mem, share):
    await _share(mem, share)

    async def failing_reader():
        await mem.execute("SELECT n FROM no_such_table")

    failed, _ = await asyncio.wait_for(
        asyncio.gather(failing_reader(), _write(mem, "update"), return_exceptions=True), 20
    )
    left = sum(len(cursors) for cursors in mem._unread.values())
    count = await mem.execute("SELECT count(*) FROM t WHERE v = 'dirty'")
    dirty = (await count.fetchone())[0]
    await _unshare(mem, share)
    assert (type(failed), str(failed), dirty, left) == (
        sqlite3.OperationalError,
        "no such table: no_such_table",
        N,
        0,
    )
    await _whole(mem)


@pytest.mark.parametrize("share", SHARES)
async def test_a_reader_cancelled_while_its_statement_is_in_flight_leaves_nothing(mem, share):
    await _share(mem, share)
    reader = asyncio.create_task(_reader(mem, 0))
    writer = asyncio.create_task(_write(mem, "update"))
    await asyncio.sleep(0)  # one turn: both statements are queued on the connection
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reader, 10)
    await asyncio.wait_for(writer, 10)
    left = sum(len(cursors) for cursors in mem._unread.values())
    await _unshare(mem, share)
    await _whole(mem)
    assert (left, mem.pool_stats().available_connections) == (0, 1)


@pytest.mark.parametrize("share", SHARES)
async def test_a_reader_cancelled_in_flight_with_a_siblings_write_behind_has_none_of_its_rows_read(
    mem, share, read_ahead
):
    await _share(mem, share)
    go = threading.Event()
    waiting = asyncio.create_task(_in_thread(mem.connection, lambda db: go.wait(10)))  # holds the thread
    reader = asyncio.create_task(_reader(mem, 0))
    writer = asyncio.create_task(_write(mem, "update"))
    await asyncio.sleep(0)  # one turn: the wait, the SELECT, its read ahead and the UPDATE are queued, in that order
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reader, 10)
    go.set()
    await asyncio.wait_for(asyncio.gather(waiting, writer), 10)
    await _unshare(mem, share)
    await _whole(mem)
    assert read_ahead == [0]  # the SELECT ran, and nobody can read its N rows


@pytest.mark.parametrize("given_up_at,stepped_to", [(None, []), (3, [0, 1, 2, 3])], ids=["before", "at-row-3"])
async def test_a_read_ahead_of_a_cursor_nobody_can_read_steps_no_further(mem, given_up_at, stepped_to):
    await mem.execute("SELECT 1")
    cur = _Cursor(mem, None, mem.connection)
    stepped: list = []

    def rows():
        for n in range(1000):
            stepped.append(n)
            if n == given_up_at:
                cur._lose("its statement was cancelled")
            yield (n,)

    cur._raw = rows()
    if given_up_at is None:
        cur._lose("its statement was cancelled")
    assert (_InThread.read_rest(None, cur), stepped) == (([], None), stepped_to)
    await mem.auto_commit()


@pytest.mark.parametrize(
    "how,kept",
    [
        ("unread", 1),  # the control: a cursor with rows left is kept
        ("failed", 0),
        ("insert", 0),
        ("fetchall", 0),
        ("fetchone to the end", 0),
        ("fetchmany short", 0),
        ("closed", 0),
    ],
)
async def test_a_cursor_with_nothing_left_to_read_is_not_kept_for_a_read_ahead(mem, how, kept):
    await mem.begin_transaction()  # the connection stays this task's: no other context takes a cursor off it
    if how == "failed":
        with pytest.raises(sqlite3.OperationalError, match="no such table: no_such_table") as held:
            await mem.execute("SELECT n FROM no_such_table")
    elif how == "insert":
        held = await mem.execute("INSERT INTO t VALUES (-1, 'x')")
    else:
        held = await mem.execute("SELECT n FROM t WHERE n < 3")
        if how == "fetchall":
            await held.fetchall()
        elif how == "fetchone to the end":
            while await held.fetchone() is not None:
                pass
        elif how == "fetchmany short":
            await held.fetchmany(10)
        elif how == "closed":
            await held.close()
    left = sum(len(cursors) for cursors in mem._unread.values())
    await mem.end_transaction(commit=False)
    assert (left, held is not None) == (kept, True)  # ``held`` keeps the cursor alive until counted


@pytest.mark.parametrize("share", SHARES)
async def test_a_select_with_no_rows_beside_a_siblings_write_leaves_no_cursor(mem, share):
    await _share(mem, share)

    async def empty():
        cur = await mem.execute("SELECT n, v FROM t WHERE n < 0")
        return await cur.fetchall()

    got, _ = await asyncio.wait_for(asyncio.gather(empty(), _write(mem, "update")), 20)
    left = sum(len(cursors) for cursors in mem._unread.values())
    await _unshare(mem, share)
    assert (got, left) == ([], 0)


@pytest.mark.parametrize("share", SHARES)
async def test_an_insert_returning_in_flight_with_a_siblings_write_behind_returns_its_own_rows(mem, share):
    await _share(mem, share)

    async def returning():
        cur = await mem.execute("INSERT INTO t VALUES (-5, 'r'), (-6, 'r') RETURNING n, v")
        return [tuple(r) for r in await cur.fetchall()]

    got, _ = await asyncio.wait_for(asyncio.gather(returning(), _write(mem, "update")), 20)
    await _unshare(mem, share)
    assert got == [(-5, "r"), (-6, "r")]


# --- a connection replaced after a failed rollback ---------------------------------------------


async def test_a_cursor_on_a_connection_whose_reclaim_rollback_fails_raises_not_connected(mem):
    made = asyncio.Event()
    box: dict = {}

    async def leave_it():  # ends holding the connection: the reclaim rolls it back
        box["cur"] = await mem.execute("SELECT n, v FROM t ORDER BY rowid")
        box["raw"] = mem.connection
        await box["raw"].close()  # the read ahead and the rollback of the reclaim both fail
        made.set()

    await asyncio.wait_for(asyncio.create_task(leave_it()), 10)
    await asyncio.wait_for(made.wait(), 5)
    await _whole(mem)
    with pytest.raises(NotConnectedError, match="could not be read before its connection changed hands"):
        await asyncio.wait_for(box["cur"].fetchall(), 5)
    count = await mem.execute("SELECT count(*) FROM t")
    replaced = (box["raw"] in mem._all_conns, box["raw"] in mem._unread, len(mem._all_conns))
    assert ((await count.fetchone())[0], replaced) == (N, (False, False, 1))
    await mem.auto_commit()


# --- the private names of aiosqlite the adapter relies on ----------------------------------


@pytest.mark.parametrize("name", _AIOSQLITE_PRIVATE)
async def test_the_installed_aiosqlite_has_each_private_name_the_adapter_uses(name):
    owner, attribute = name.split(".")
    assert hasattr(getattr(aiosqlite, owner), attribute), f"aiosqlite {aiosqlite.__version__} has no {name}"


@pytest.mark.parametrize("name", _AIOSQLITE_PRIVATE)
async def test_a_missing_aiosqlite_name_is_raised_at_connect_naming_aiosqlite_its_version_and_the_name(name):
    owner, attribute = name.split(".")
    adapter = AsyncSQLiteAdapter.in_memory(acquire_timeout=5)
    with pytest.MonkeyPatch.context() as patch:
        patch.delattr(getattr(aiosqlite, owner), attribute)
        with pytest.raises(SQLerConnectionError) as raised:
            await asyncio.wait_for(adapter.connect(), 5)
    assert (str(raised.value), adapter._all_conns) == (
        f"aiosqlite {aiosqlite.__version__} has no {name}, which sqler's async adapter needs "
        "to keep a cursor's rows when its connection changes hands",
        [],
    )


# what each call would look like in an aiosqlite that no longer takes the adapter's arguments
_CHANGED_CALLS = {
    "Connection._execute": (aiosqlite.Connection, "_execute", lambda self, fn: None),
    "Cursor": (aiosqlite, "Cursor", lambda conn: None),
}


@pytest.mark.parametrize("call", _AIOSQLITE_CALLS)
async def test_a_call_aiosqlite_no_longer_takes_is_raised_at_connect_naming_aiosqlite_its_version_and_the_call(call):
    owner, attribute, changed = _CHANGED_CALLS[call]
    adapter = AsyncSQLiteAdapter.in_memory(acquire_timeout=5)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(owner, attribute, changed)
        with pytest.raises(SQLerConnectionError) as raised:
            await asyncio.wait_for(adapter.connect(), 5)
    assert (str(raised.value), adapter._all_conns) == (
        f"aiosqlite {aiosqlite.__version__}'s {call} does not take the {_AIOSQLITE_CALLS[call]} arguments "
        "sqler's async adapter passes it to keep a cursor's rows when its connection changes hands",
        [],
    )
