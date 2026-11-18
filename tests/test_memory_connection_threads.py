"""Threads on the one connection of a plain ``":memory:"`` database.

Every thread shares that connection, so a transaction on it belongs to the thread that
opened it: while it is open, every statement of another thread waits until it ends (at most
the adapter's ``timeout_ms``, as a second connection waits for a locked file database). Each
thread's transaction keeps or loses only its own rows, as on a file database, where each
thread has its own connection.
"""

import json
import signal
import sqlite3
import sys
import threading
import time
from contextlib import nullcontext

import pytest
from exception_points import (
    RAISED,
    Skipped,
    needs_libc_raise,
    raising_at,
    set_skipped_once_it_waits_in,
    stop_once_and_interrupt,
    trace_raising_at_line,
    tracing,
)

from sqler import SQLerDB
from sqler.adapter.synchronous import SQLiteAdapter
from sqler.exceptions import NotConnectedError, TransactionStateError

TRACED = ("BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT", "INSERT")


class Interrupt(BaseException):
    """Stands for ``KeyboardInterrupt``: an exception that is not an ``Exception``."""


@pytest.fixture
def db():
    database = SQLerDB(SQLiteAdapter(":memory:"))
    database.adapter.execute("CREATE TABLE t (v TEXT)")
    database.adapter.auto_commit()
    yield database
    database.close()


def _trace(database: SQLerDB) -> list[str]:
    """The transaction statements and inserts sent on the connection from now on."""
    sent: list[str] = []
    database.adapter._conn().set_trace_callback(
        lambda sql: sent.append(sql) if sql.split()[0].upper() in TRACED else None
    )
    return sent


def _rows(database: SQLerDB) -> list[str]:
    rows = sorted(r[0] for r in database.adapter.execute("SELECT v FROM t").fetchall())
    database.adapter.auto_commit()
    return rows


def _insert(database: SQLerDB, value: str) -> None:
    database.adapter.execute("INSERT INTO t VALUES (?)", [value])
    database.adapter.auto_commit()


def _wait_for_waiter(database: SQLerDB, past: threading.Event) -> None:
    """Return once another thread waits for the connection's turn, or got past where it
    would wait (it sets ``past``); fails after 10 s."""
    deadline = time.monotonic() + 10
    while not past.is_set():
        shared = getattr(database.adapter, "_shared", None)
        if shared is not None and shared._waiting > 0:
            return
        assert time.monotonic() < deadline, "no thread came to wait for the turn"
        time.sleep(0.001)


def _start(target) -> tuple[threading.Thread, list[BaseException]]:
    raised: list[BaseException] = []

    def body():
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            raised.append(exc)

    thread = threading.Thread(target=body)
    thread.start()
    return thread, raised


# ---- each thread's transaction keeps or loses only its own rows ----


@pytest.mark.parametrize("a_commits", [True, False], ids=["a-commits", "a-raises"])
@pytest.mark.parametrize("b_commits", [True, False], ids=["b-commits", "b-raises"])
def test_two_threads_transactions_each_keep_or_lose_only_their_own_row(db, a_commits, b_commits):
    sent = _trace(db)
    a_in, b_past = threading.Event(), threading.Event()

    def b():
        assert a_in.wait(10)
        with pytest.raises(RuntimeError) if not b_commits else nullcontext():
            with db.transaction():
                _insert(db, "b")
                b_past.set()
                if not b_commits:
                    raise RuntimeError("b undoes")

    thread, raised = _start(b)
    with pytest.raises(RuntimeError) if not a_commits else nullcontext():
        with db.transaction():
            _insert(db, "a")
            a_in.set()
            _wait_for_waiter(db, b_past)
            if not a_commits:
                raise RuntimeError("a undoes")
    thread.join(10)
    assert raised == []
    assert _rows(db) == [v for v, kept in (("a", a_commits), ("b", b_commits)) if kept]
    a_end = "COMMIT" if a_commits else "ROLLBACK"
    b_end = "COMMIT" if b_commits else "ROLLBACK"
    assert sent == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('a')",
        a_end,
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('b')",
        b_end,
    ]


def test_twenty_threads_each_with_a_transaction_of_two_inserts_keep_forty_rows(db):
    sent = _trace(db)
    start = threading.Barrier(20)

    def work(n: int) -> None:
        start.wait(10)
        with db.transaction():
            _insert(db, f"{n:02}-1")
            _insert(db, f"{n:02}-2")

    started = [_start(lambda n=n: work(n)) for n in range(20)]
    for thread, _ in started:
        thread.join(30)
    assert [raised for _, raised in started] == [[]] * 20
    assert _rows(db) == sorted(f"{n:02}-{i}" for n in range(20) for i in (1, 2))
    assert (sent.count("BEGIN IMMEDIATE"), sent.count("COMMIT")) == (20, 20)
    assert [s for s in sent if s.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK"))] == []


# ---- a plain statement of another thread waits ----


def test_another_threads_plain_insert_waits_and_survives_the_rollback(db):
    sent = _trace(db)
    a_in, b_past = threading.Event(), threading.Event()

    def b():
        assert a_in.wait(10)
        _insert(db, "b")
        b_past.set()

    thread, raised = _start(b)
    with pytest.raises(RuntimeError):
        with db.transaction():
            _insert(db, "a")
            a_in.set()
            _wait_for_waiter(db, b_past)
            raise RuntimeError("a undoes")
    thread.join(10)
    assert (raised, _rows(db)) == ([], ["b"])
    # the waiter's insert was sent after the holder's end
    assert sent == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('a')",
        "ROLLBACK",
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('b')",
        "COMMIT",
    ]


def test_another_thread_waits_for_a_statement_and_its_commit(db):
    sent = _trace(db)
    holder, b_past = threading.Event(), threading.Event()

    def b():
        assert holder.wait(10)
        _insert(db, "b")
        b_past.set()

    thread, raised = _start(b)
    with pytest.raises(RuntimeError):
        with db.adapter:  # rolls back on the way out
            db.adapter.execute("INSERT INTO t VALUES ('x')")  # opens the implicit transaction
            holder.set()
            _wait_for_waiter(db, b_past)
            raise RuntimeError("undo x")
    thread.join(10)
    assert (raised, _rows(db)) == ([], ["b"])
    # the waiter's insert was sent after the holder's end
    assert sent == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('x')",
        "ROLLBACK",
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('b')",
        "COMMIT",
    ]


# ---- the wait is bounded ----


@pytest.mark.parametrize("call", ["execute", "begin_transaction", "commit"])
def test_the_wait_for_another_threads_transaction_runs_out_after_timeout_ms(call):
    database = SQLerDB(SQLiteAdapter(":memory:", timeout_ms=50))
    adapter = database.adapter
    adapter.execute("CREATE TABLE t (v TEXT)")
    adapter.auto_commit()
    calls = {
        "execute": lambda: adapter.execute("SELECT v FROM t"),
        "begin_transaction": adapter.begin_transaction,
        "commit": adapter.commit,
    }
    try:
        with database.transaction():
            _insert(database, "a")
            thread, raised = _start(calls[call])
            thread.join(10)
        assert [(type(e), str(e)) for e in raised] == [
            (
                sqlite3.OperationalError,
                "database is locked: another thread's transaction holds the in-memory connection",
            )
        ]
        assert _rows(database) == ["a"]
    finally:
        database.close()


# ---- a thread that ends with its transaction open ----


def test_a_thread_that_ends_inside_its_transaction_is_rolled_back_and_the_waiter_goes_on(db):
    sent = _trace(db)
    a_in, b_past = threading.Event(), threading.Event()

    def a():
        db.adapter.begin_transaction()
        _insert(db, "a")
        a_in.set()
        _wait_for_waiter(db, b_past)
        # ends without end_transaction

    def b():
        assert a_in.wait(10)
        with db.transaction():
            _insert(db, "b")
            b_past.set()

    first, raised_a = _start(a)
    second, raised_b = _start(b)
    first.join(10)
    second.join(10)
    assert (raised_a, raised_b, second.is_alive()) == ([], [], False)
    assert _rows(db) == ["b"]
    assert db.adapter._conn().in_transaction is False
    # the waiter began after the ended thread's transaction was rolled back
    assert sent == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('a')",
        "ROLLBACK",
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('b')",
        "COMMIT",
    ]


def test_a_thread_that_ends_holding_an_implicit_transaction_is_rolled_back(db):
    def a():
        db.adapter.execute("INSERT INTO t VALUES ('a')")  # never committed

    first, raised = _start(a)
    first.join(10)
    with db.transaction():
        _insert(db, "main")
    assert (raised, _rows(db)) == ([], ["main"])


def test_a_base_exception_in_a_transaction_body_rolls_back_and_frees_the_connection(db):
    sent = _trace(db)
    a_in, b_past = threading.Event(), threading.Event()

    def a():
        with db.transaction():
            _insert(db, "a")
            a_in.set()
            _wait_for_waiter(db, b_past)
            raise Interrupt

    def b():
        assert a_in.wait(10)
        with db.transaction():
            _insert(db, "b")
            b_past.set()

    first, raised_a = _start(a)
    second, raised_b = _start(b)
    first.join(10)
    second.join(10)
    assert ([type(e) for e in raised_a], raised_b) == ([Interrupt], [])
    assert _rows(db) == ["b"]
    # the waiter began after the interrupted transaction was rolled back
    assert sent == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('a')",
        "ROLLBACK",
        "BEGIN IMMEDIATE",
        "INSERT INTO t VALUES ('b')",
        "COMMIT",
    ]


# ---- close() while a thread waits ----


def test_close_from_another_thread_wakes_a_waiter_with_not_connected():
    database = SQLerDB(SQLiteAdapter(":memory:"))
    database.adapter.execute("CREATE TABLE t (v TEXT)")
    database.adapter.auto_commit()
    database.adapter.begin_transaction()
    _insert(database, "a")
    b_past = threading.Event()

    def b():
        database.adapter.begin_transaction()
        b_past.set()

    thread, raised = _start(b)
    _wait_for_waiter(database, b_past)
    database.close()
    thread.join(10)
    assert (thread.is_alive(), [type(e) for e in raised]) == (False, [NotConnectedError])


# ---- one thread nests inside its own transaction ----


@pytest.mark.parametrize("kind", ["memory", "file"])
def test_one_thread_nesting_three_deep_sends_savepoints(kind, tmp_path):
    path = ":memory:" if kind == "memory" else str(tmp_path / "nest.db")
    database = SQLerDB(SQLiteAdapter(path))
    try:
        database.adapter.execute("CREATE TABLE t (v TEXT)")
        database.adapter.auto_commit()
        sent = _trace(database)
        with database.transaction():
            with database.transaction():
                with database.transaction():
                    _insert(database, "deep")
        assert sent == [
            "BEGIN IMMEDIATE",
            "SAVEPOINT sp_1",
            "SAVEPOINT sp_2",
            "INSERT INTO t VALUES ('deep')",
            "RELEASE SAVEPOINT sp_2",
            "RELEASE SAVEPOINT sp_1",
            "COMMIT",
        ]
        assert _rows(database) == ["deep"]
    finally:
        database.close()


def test_the_transaction_depth_is_per_thread(db):
    seen: list[int] = []
    with db.transaction():
        thread, raised = _start(lambda: seen.append(db.adapter._txn_depth))
        thread.join(10)
        assert (raised, seen, db.adapter._txn_depth) == ([], [0], 1)


# ---- a cursor read while another thread's transaction is open ----


def test_a_cursor_read_while_another_threads_transaction_is_open_yields_what_its_statement_sorted(
    db,
):
    for v in ("1", "2"):
        _insert(db, v)
    cursor = db.adapter.execute("SELECT v FROM t ORDER BY v")
    a_in, read = threading.Event(), threading.Event()

    def a():
        with pytest.raises(RuntimeError):
            with db.transaction():
                _insert(db, "3")
                a_in.set()
                assert read.wait(10)
                raise RuntimeError("a undoes")

    thread, raised = _start(a)
    assert a_in.wait(10)
    # fetching runs no statement of the adapter, so it does not wait for the turn; the
    # sorted statement read its rows before the other thread's insert
    fetched = [r[0] for r in cursor.fetchall()]
    read.set()
    thread.join(10)
    assert (raised, fetched, _rows(db)) == ([], ["1", "2"], ["1", "2"])


# ---- ``with adapter:`` ends the block's own write ----


def _items(database: SQLerDB) -> list[dict]:
    rows = database.adapter.execute("SELECT data FROM items ORDER BY _id").fetchall()
    database.adapter.auto_commit()
    return [json.loads(r[0]) for r in rows]


@pytest.mark.parametrize("kind", ["memory", "file"])
def test_with_adapter_commits_the_block_on_every_kind_of_database(kind, tmp_path):
    database = (
        SQLerDB.in_memory(shared=False)
        if kind == "memory"
        else SQLerDB.on_disk(str(tmp_path / "w.db"))
    )
    try:
        database.bulk_upsert("items", [{"n": i} for i in range(3)])
        assert database.adapter._conn().in_transaction is False
        with database.transaction():
            database.insert_document("items", {"n": 3})
        assert [d["n"] for d in _items(database)] == [0, 1, 2, 3]
    finally:
        database.close()


@pytest.mark.parametrize("kind", ["memory", "file"])
def test_with_adapter_inside_the_threads_transaction_leaves_the_transaction_open(kind, tmp_path):
    database = (
        SQLerDB.in_memory(shared=False)
        if kind == "memory"
        else SQLerDB.on_disk(str(tmp_path / "w.db"))
    )
    try:
        database.ensure_table("items")
        with pytest.raises(RuntimeError):
            with database.transaction():
                database.insert_document("items", {"n": 1})  # before the block
                database.bulk_upsert("items", [{"n": 2}])  # inside ``with adapter:``
                raise RuntimeError("undo both writes")  # after the block
        assert _items(database) == []
    finally:
        database.close()


# ---- when the connection is free, every waiter goes on ----

# A waiter left asleep would wait this long; each test's own bound (10 s) is far below it.
LONG_MS = 120_000


@pytest.fixture
def long_db():
    database = SQLerDB(SQLiteAdapter(":memory:", timeout_ms=LONG_MS))
    database.adapter.execute("CREATE TABLE t (v TEXT)")
    database.adapter.auto_commit()
    yield database
    database.close()  # wakes a waiter a failed test left behind


def _wait_for_waiters(database: SQLerDB, n: int) -> None:
    """Return once ``n`` threads wait for the connection's turn; fails after 10 s."""
    deadline = time.monotonic() + 10
    while database.adapter._shared._waiting < n:
        assert time.monotonic() < deadline, (
            f"{database.adapter._shared._waiting} of {n} threads came to wait"
        )
        time.sleep(0.001)


def _join_all(started: list[tuple[threading.Thread, list[BaseException]]]) -> list[bool]:
    """Join every thread within one 10 s bound; whether each is still alive."""
    deadline = time.monotonic() + 10
    for thread, _ in started:
        thread.join(max(0.0, deadline - time.monotonic()))
    return [thread.is_alive() for thread, _ in started]


def _count(database: SQLerDB) -> int:
    return database.adapter.execute("SELECT count(*) FROM t").fetchall()[0][0]


def test_a_holder_and_two_plain_readers_both_go_on_when_it_commits(long_db):
    counts: list[int] = []
    long_db.adapter.begin_transaction()
    _insert(long_db, "a")
    started = [_start(lambda: counts.append(_count(long_db))) for _ in range(2)]
    _wait_for_waiters(long_db, 2)
    long_db.adapter.end_transaction(commit=True)
    assert _join_all(started) == [False, False]
    assert ([r for _, r in started], counts) == ([[], []], [1, 1])  # each read after the commit


def test_a_holder_and_five_mixed_waiters_all_go_on_when_it_commits(long_db):
    sent = _trace(long_db)
    counts: list[int] = []

    def in_transaction() -> None:
        with long_db.transaction():
            _insert(long_db, "txn")

    long_db.adapter.begin_transaction()
    _insert(long_db, "a")
    targets = [
        lambda: counts.append(_count(long_db)),
        lambda: counts.append(_count(long_db)),
        lambda: _insert(long_db, "plain-1"),
        lambda: _insert(long_db, "plain-2"),
        in_transaction,
    ]
    started = [_start(t) for t in targets]
    _wait_for_waiters(long_db, 5)
    long_db.adapter.end_transaction(commit=True)
    assert _join_all(started) == [False] * 5
    assert [r for _, r in started] == [[]] * 5
    assert _rows(long_db) == ["a", "plain-1", "plain-2", "txn"]
    # the order among waiters is not promised; each reader read after the holder's commit
    assert len(counts) == 2
    assert all(1 <= c <= 4 for c in counts)
    # every waiter's insert was sent after the holder's commit
    assert sent[:3] == ["BEGIN IMMEDIATE", "INSERT INTO t VALUES ('a')", "COMMIT"]
    assert sorted(s for s in sent[3:] if s.startswith("INSERT")) == [
        "INSERT INTO t VALUES ('plain-1')",
        "INSERT INTO t VALUES ('plain-2')",
        "INSERT INTO t VALUES ('txn')",
    ]


def test_a_holder_thread_that_ends_without_closing_frees_both_waiting_readers(long_db):
    counts: list[int] = []
    a_in, a_go = threading.Event(), threading.Event()

    def a():
        long_db.adapter.begin_transaction()
        _insert(long_db, "a")
        a_in.set()
        assert a_go.wait(10)
        # ends without end_transaction: its transaction is rolled back

    holder, raised_a = _start(a)
    assert a_in.wait(10)
    started = [_start(lambda: counts.append(_count(long_db))) for _ in range(2)]
    _wait_for_waiters(long_db, 2)
    a_go.set()
    holder.join(10)
    assert _join_all(started) == [False, False]
    assert (raised_a, [r for _, r in started], counts) == ([], [[], []], [0, 0])


# ---- a thread that leaves the wait by an exception holds nothing ----


@pytest.mark.skipif(
    not hasattr(signal, "pthread_kill"), reason="needs a real signal to the main thread"
)
def test_an_interrupted_waiter_holds_nothing_and_the_waiter_behind_it_goes_on(long_db):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("the interrupt goes to the main thread")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    a_in, a_go = threading.Event(), threading.Event()
    counts: list[int] = []

    def a():
        long_db.adapter.begin_transaction()
        _insert(long_db, "a")
        a_in.set()
        _wait_for_waiters(long_db, 2)  # the main thread and the reader wait
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)  # what Ctrl-C does
        assert a_go.wait(10)
        long_db.adapter.end_transaction(commit=True)

    holder, raised_a = _start(a)
    assert a_in.wait(10)
    reader = _start(lambda: counts.append(_count(long_db)))
    with pytest.raises(KeyboardInterrupt):
        long_db.adapter.execute("SELECT 1")  # waits behind the holder
    assert long_db.adapter._shared._waiting == 1  # only the reader still waits
    a_go.set()
    assert _join_all([(holder, raised_a), reader]) == [False, False]
    assert (raised_a, reader[1], counts) == ([], [], [1])
    assert _count(long_db) == 1  # the main thread goes on as if it had never asked
    closer = _start(long_db.close)
    assert _join_all([closer]) == [False]


# ---- the rows of a statement are read inside its turn ----


def _fill(database: SQLerDB, value: str, n: int) -> None:
    database.adapter.executemany("INSERT INTO t VALUES (?)", [(value,)] * n)


def _dirty_writer(database: SQLerDB, a_in: threading.Event, a_go: threading.Event):
    """A thread that inserts 2000 'dirty' rows in its transaction, waits, and rolls back."""

    def a():
        database.adapter.begin_transaction()
        _fill(database, "dirty", 2000)
        a_in.set()
        assert a_go.wait(10)
        database.adapter.end_transaction(commit=False)

    return _start(a)


@pytest.mark.parametrize("read", ["fetchall", "iteration"])
def test_a_cursor_read_late_holds_only_the_committed_rows(db, read):
    _fill(db, "clean", 2000)
    cursor = db.adapter.execute("SELECT v FROM t")  # no ORDER BY: SQLite steps at each fetch
    a_in, a_go = threading.Event(), threading.Event()
    thread, raised = _dirty_writer(db, a_in, a_go)
    assert a_in.wait(10)
    rows = [r[0] for r in (cursor.fetchall() if read == "fetchall" else cursor)]
    a_go.set()
    thread.join(10)
    assert (raised, len(rows), rows.count("clean")) == ([], 2000, 2000)


def test_iter_dicts_read_late_holds_only_the_committed_documents():
    database = SQLerDB.in_memory(shared=False)
    try:
        database.insert_many("items", [{"v": "clean"}] * 2000)
        docs = database.query("items").iter_dicts()
        first = next(docs)  # the statement ran
        a_in, a_go = threading.Event(), threading.Event()

        def a():
            database.adapter.begin_transaction()
            database.insert_many("items", [{"v": "dirty"}] * 2000)
            a_in.set()
            assert a_go.wait(10)
            database.adapter.end_transaction(commit=False)

        thread, raised = _start(a)
        assert a_in.wait(10)
        values = [first["v"]] + [d["v"] for d in docs]
        a_go.set()
        thread.join(10)
        assert (raised, len(values), values.count("clean")) == ([], 2000, 2000)
    finally:
        database.close()


def test_a_cursor_read_across_another_threads_transaction_and_its_rollback_holds_only_committed_rows(
    db,
):
    _fill(db, "clean", 2000)
    cursor = db.adapter.execute("SELECT v FROM t")
    rows = [r[0] for r in cursor.fetchmany(1990)]
    a_in, a_go = threading.Event(), threading.Event()
    thread, raised = _dirty_writer(db, a_in, a_go)
    assert a_in.wait(10)
    rows += [r[0] for r in cursor.fetchmany(20)]  # while the other thread's rows are there
    a_go.set()
    thread.join(10)
    rows += [r[0] for r in cursor.fetchall()]  # after its rollback
    assert (raised, len(rows), rows.count("clean")) == ([], 2000, 2000)


def _observe(adapter: SQLiteAdapter, sql: str) -> dict:
    """Everything a caller reads from the object a statement returns."""
    cursor = adapter.execute(sql)
    seen = {"description": cursor.description, "arraysize": cursor.arraysize}
    seen["first"] = None if (row := cursor.fetchone()) is None else tuple(row)
    seen["three"] = [tuple(r) for r in cursor.fetchmany(3)]
    seen["three_more"] = [tuple(r) for r in cursor.fetchmany(3)]
    seen["rest"] = [tuple(r) for r in cursor]
    seen["after_last"] = cursor.fetchone()
    seen["fetchall_after"] = cursor.fetchall()
    seen["rowcount"] = cursor.rowcount
    seen["lastrowid"] = cursor.lastrowid
    cursor.close()
    return seen


STATEMENTS = {
    "rows": "SELECT n, v FROM t WHERE n > 1",
    "no-row": "SELECT n, v FROM t WHERE n > 100",
    "pragma": "PRAGMA table_info(t)",
    "returning": "INSERT INTO t (n, v) VALUES (20, 'r1'), (21, 'r2') RETURNING n, v",
    "plain-insert": "INSERT INTO t (n, v) VALUES (30, 'p')",
}


@pytest.mark.parametrize("statement", list(STATEMENTS))
def test_the_returned_object_answers_what_a_file_databases_cursor_answers(statement, tmp_path):
    seen = {}
    for kind in ("memory", "file"):
        adapter = SQLiteAdapter(":memory:" if kind == "memory" else str(tmp_path / "c.db"))
        adapter.connect()
        try:
            adapter.execute("CREATE TABLE t (n INTEGER PRIMARY KEY, v TEXT)")
            adapter.executemany("INSERT INTO t VALUES (?, ?)", [(n, f"v{n}") for n in range(1, 11)])
            seen[kind] = _observe(adapter, STATEMENTS[statement])
            adapter.auto_commit()
            seen[kind]["kept"] = adapter.execute("SELECT count(*) FROM t").fetchall()[0][0]
        finally:
            adapter.close()
    assert seen["memory"] == seen["file"]
    expected_kept = {"returning": 12, "plain-insert": 11}.get(statement, 10)
    assert seen["memory"]["kept"] == expected_kept


def test_the_thread_holding_a_transaction_reads_its_own_uncommitted_rows(db):
    with db.transaction():
        _insert(db, "mine")
        assert [r[0] for r in db.adapter.execute("SELECT v FROM t")] == ["mine"]
    assert _rows(db) == ["mine"]


def test_rows_read_before_close_are_served_after_it():
    adapter = SQLiteAdapter(":memory:")
    adapter.connect()
    adapter.execute("CREATE TABLE t (v TEXT)")
    adapter.executemany("INSERT INTO t VALUES (?)", [(str(n),) for n in range(5)])
    cursor = adapter.execute("SELECT v FROM t")
    adapter.close()
    assert [r[0] for r in cursor.fetchall()] == ["0", "1", "2", "3", "4"]


def test_a_select_of_100000_rows_is_read_whole(db):
    db.adapter.execute(
        "INSERT INTO t WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000) "
        "SELECT x FROM c"
    )
    db.adapter.auto_commit()
    assert len(db.adapter.execute("SELECT v FROM t").fetchall()) == 100000


@pytest.mark.parametrize(
    "inside", [False, True], ids=["outside-a-transaction", "inside-its-transaction"]
)
def test_a_select_whose_fifth_row_raises_fails_the_statement_and_frees_the_turn(db, inside):
    def fifth_raises(v):
        if v == "5":
            raise ValueError("row five")
        return v

    db.adapter._conn().create_function("fifth_raises", 1, fifth_raises)
    for n in range(1, 8):
        _insert(db, str(n))
    if inside:
        db.adapter.begin_transaction()
        _insert(db, "mine")
    with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
        db.adapter.execute("SELECT fifth_raises(v) FROM t ORDER BY rowid")
    if inside:
        db.adapter.end_transaction(commit=True)
    counts: list[int] = []
    other = _start(lambda: counts.append(_count(db)))
    assert _join_all([other]) == [False]
    assert (other[1], counts) == ([], [8 if inside else 7])


def test_insert_returning_waits_for_another_threads_transaction(long_db):
    returned: list[list[tuple]] = []

    def b():
        cursor = long_db.adapter.execute("INSERT INTO t VALUES ('b') RETURNING v")
        returned.append([tuple(r) for r in cursor.fetchall()])
        long_db.adapter.auto_commit()

    long_db.adapter.begin_transaction()
    _insert(long_db, "a")
    thread = _start(b)
    _wait_for_waiters(long_db, 1)
    long_db.adapter.end_transaction(commit=False)
    assert _join_all([thread]) == [False]
    assert (thread[1], returned, _rows(long_db)) == ([], [[("b",)]], ["b"])


def test_timeout_ms_zero_goes_on_when_free_and_raises_at_once_when_held():
    database = SQLerDB(SQLiteAdapter(":memory:", timeout_ms=0))
    try:
        database.adapter.execute("CREATE TABLE t (v TEXT)")
        database.adapter.auto_commit()
        assert _count(database) == 0
        database.adapter.begin_transaction()
        _insert(database, "a")
        other = _start(lambda: database.adapter.execute("SELECT 1"))
        assert _join_all([other]) == [False]
        assert [(type(e), str(e)) for e in other[1]] == [
            (
                sqlite3.OperationalError,
                "database is locked: another thread's transaction holds the in-memory connection",
            )
        ]
        database.adapter.end_transaction(commit=True)
    finally:
        database.close()


def test_twenty_threads_each_count_only_whole_transactions(db):
    start = threading.Barrier(20)
    counts: list[int] = []

    def work(n: int) -> None:
        start.wait(10)
        with db.transaction():
            _insert(db, f"{n:02}-1")
            _insert(db, f"{n:02}-2")
            counts.append(_count(db))

    started = [_start(lambda n=n: work(n)) for n in range(20)]
    assert _join_all(started) == [False] * 20
    assert [r for _, r in started] == [[]] * 20
    assert sorted(counts) == list(range(2, 41, 2))


def test_close_from_another_thread_wakes_three_waiters_with_not_connected(long_db):
    long_db.adapter.begin_transaction()
    _insert(long_db, "a")
    started = [_start(lambda: long_db.adapter.execute("SELECT 1")) for _ in range(3)]
    _wait_for_waiters(long_db, 3)
    closer = _start(long_db.close)
    assert _join_all([closer, *started]) == [False] * 4
    assert [[type(e) for e in r] for _, r in started] == [[NotConnectedError]] * 3


# ---- the open levels of a database object are per thread ----


def test_a_thread_left_inside_its_transaction_leaves_no_level_for_the_main_thread():
    database = SQLerDB.in_memory(shared=False)
    try:

        def a():
            database.transaction().__enter__()
            database.upsert_with_version(
                "docs", None, {"by": "a"}, None
            )  # creates the versioned table
            # ends inside the block: its transaction is rolled back, the table with it

        thread, raised = _start(a)
        thread.join(10)
        assert raised == []
        _id, version = database.upsert_with_version("docs", None, {"by": "main"}, None)
        assert (_id, version) == (1, 0)
        assert database.find_document_with_version("docs", 1) == {
            "_id": 1,
            "_version": 0,
            "by": "main",
        }
    finally:
        database.close()


# ---- an interrupt never leaves the turn held ----
#
# A statement or an end fails in SQLite while a ``KeyboardInterrupt`` is pending (Ctrl-C
# as SQLite stops it): Python raises the interrupt at the adapter's first instruction
# after the failure that can raise one. The turn's lock is free afterwards, nothing of the
# failed work is kept, and the thread's next statement runs. Thread-own connections (a
# file, a named shared-cache memory database) leave what they always left.


def _place_db(place: str, tmp_path) -> SQLerDB:
    """A database at ``place`` with ``t (v)`` holding ``before``, committed."""
    path = {
        "memory": ":memory:",
        "file": str(tmp_path / "interrupted.db"),
        "named-memory": f"file:interrupted-{tmp_path.name}?mode=memory&cache=shared",
    }[place]
    database = SQLerDB(SQLiteAdapter(path, timeout_ms=5000))
    database.adapter.execute("CREATE TABLE t (v TEXT)")
    _insert(database, "before")
    return database


def _in(database: SQLerDB, value: str) -> None:
    database.adapter.execute("INSERT INTO t VALUES (?)", [value])


def _nested(database: SQLerDB, commit: bool) -> None:
    a = database.adapter
    a.begin_transaction()
    _in(database, "outer")
    a.begin_transaction()
    _in(database, "nested")
    stop_once_and_interrupt(a._conn())
    a.end_transaction(commit=commit)


def _inside(database: SQLerDB, statement) -> None:
    database.adapter.begin_transaction()
    _in(database, "outer")
    stop_once_and_interrupt(database.adapter._conn())
    statement(database.adapter)


def _outside(database: SQLerDB, statement) -> None:
    stop_once_and_interrupt(database.adapter._conn())
    statement(database.adapter)


def _outermost_end(database: SQLerDB, commit: bool) -> None:
    database.adapter.begin_transaction()
    _in(database, "in the transaction")
    stop_once_and_interrupt(database.adapter._conn())
    database.adapter.end_transaction(commit=commit)


def _commit_of_a_pending_write(database: SQLerDB) -> None:
    _in(database, "pending")  # Python's sqlite3 opens a transaction for it
    stop_once_and_interrupt(database.adapter._conn())
    database.adapter.commit()


INTERRUPTED_AS = {
    "nested-rollback-to": lambda d: _nested(d, commit=False),
    "nested-release": lambda d: _nested(d, commit=True),
    "execute-in-a-transaction": lambda d: _inside(
        d, lambda a: a.execute("INSERT INTO t VALUES ('stopped')")
    ),
    "executemany-in-a-transaction": lambda d: _inside(
        d, lambda a: a.executemany("INSERT INTO t VALUES (?)", [["m1"], ["m2"]])
    ),
    "execute": lambda d: _outside(d, lambda a: a.execute("INSERT INTO t VALUES ('stopped')")),
    "executemany": lambda d: _outside(
        d, lambda a: a.executemany("INSERT INTO t VALUES (?)", [["m1"], ["m2"]])
    ),
    "executescript": lambda d: _outside(
        d, lambda a: a.executescript("INSERT INTO t VALUES ('s1');")
    ),
    "begin": lambda d: _outside(d, lambda a: a.begin_transaction()),
    "outermost-commit": lambda d: _outermost_end(d, commit=True),
    "outermost-rollback": lambda d: _outermost_end(d, commit=False),
    "commit": _commit_of_a_pending_write,
}
# What ending the levels left open reports, and the rows once the next statement committed.
# An interrupt pending as a commit outside a transaction fails skips the rollback after it
# (the first call after the failure raises it), on every kind of connection: the pending
# write stays in the transaction and the thread's next commit commits it.
AFTER_THE_INTERRUPT = {
    "nested-rollback-to": (["TransactionStateError"], ["after", "before"]),
    "nested-release": (["TransactionStateError"], ["after", "before"]),
    "execute-in-a-transaction": (["ended"], ["after", "before"]),
    "executemany-in-a-transaction": (["ended"], ["after", "before"]),
    "execute": ([], ["after", "before"]),
    "executemany": ([], ["after", "before"]),
    "executescript": ([], ["after", "before"]),
    "begin": ([], ["after", "before"]),
    "outermost-commit": ([], ["after", "before"]),
    "outermost-rollback": ([], ["after", "before"]),
    "commit": ([], ["after", "before", "pending"]),
}


def _end_open_levels(database: SQLerDB) -> list[str]:
    ends = []
    while database.adapter._txn_depth > 0:
        try:
            database.adapter.end_transaction(commit=False)
            ends.append("ended")
        except TransactionStateError:
            ends.append("TransactionStateError")
    return ends


# On a thread's own connection a BEGIN that fails runs no Python in the adapter after it:
# the interrupt is raised in the caller, with nothing to give back.
PLACES_AND_WAYS = [
    (place, how)
    for how in INTERRUPTED_AS
    for place in ("memory", "file", "named-memory")
    if how != "begin" or place == "memory"
]


@needs_libc_raise
@pytest.mark.parametrize(("place", "how"), PLACES_AND_WAYS)
def test_an_interrupt_pending_as_sqlite_fails_leaves_the_turn_free_and_nothing_half_done(
    tmp_path, place, how
):
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    database = _place_db(place, tmp_path)
    conn = database.adapter._conn()
    try:
        with pytest.raises(KeyboardInterrupt):
            INTERRUPTED_AS[how](database)
        conn.set_progress_handler(None, 1)
        if place == "memory":
            assert database.adapter._shared._turn._lock.locked() is False
        ends = _end_open_levels(database)
        _insert(database, "after")
        assert (ends, _rows(database)) == AFTER_THE_INTERRUPT[how]
    finally:
        database.close()


@needs_libc_raise
@pytest.mark.parametrize(
    "how",
    [
        "nested-rollback-to",
        "nested-release",
        "execute-in-a-transaction",
        "outermost-commit",
        "outermost-rollback",
    ],
)
def test_a_thread_waiting_for_the_turn_goes_on_after_the_holders_interrupted_end(tmp_path, how):
    """The holder's transaction is open while the reader comes to wait; the interrupt
    lands in the holder's thread, which then ends what it left open."""
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    database = _place_db("memory", tmp_path)
    counts: list[int] = []
    conn = database.adapter._conn()
    try:
        database.adapter.begin_transaction()
        _in(database, "held")
        reader = _start(lambda: counts.append(_count(database)))
        _wait_for_waiters(database, 1)
        with pytest.raises(KeyboardInterrupt):
            if how.startswith("nested"):
                _nested(database, commit=how == "nested-release")
            elif how == "execute-in-a-transaction":
                _inside(database, lambda a: a.execute("INSERT INTO t VALUES ('stopped')"))
            else:
                _outermost_end(database, commit=how == "outermost-commit")
        conn.set_progress_handler(None, 1)
        assert database.adapter._shared._turn._lock.locked() is False
        _end_open_levels(database)
        assert _join_all([reader]) == [False]
        assert (reader[1], counts, _rows(database)) == ([], [1], ["before"])
    finally:
        database.close()


# ---- a holder left behind by any exception keeps no other thread out ----
#
# An exception can reach a thread at the entry of ``take``, after ``take`` made it the
# holder (before the caller's ``try``), or at the entry of ``give``: a trace function, a
# signal in the main thread, or an exception another thread sets with
# ``PyThreadState_SetAsyncExc``. A holder left with no transaction open keeps no one out; a
# thread that ends holding an open transaction has it rolled back, whatever skipped its
# ``give``. Each thread has its end hook before it can hold anything.

LOCKED = "database is locked: another thread's transaction holds the in-memory connection"
SKIPPED_PARTS = [
    (point, where, how)
    for point in ("take-entry", "take-return", "give-entry")
    for where in ("main", "worker")
    for how in ("trace", "another-thread", "sigint")
    if how != "sigint" or where == "main"
]


def _memory_db(timeout_ms: int) -> SQLerDB:
    """A plain ``":memory:"`` database with ``t (v)`` holding ``before``, committed."""
    database = SQLerDB(SQLiteAdapter(":memory:", timeout_ms=timeout_ms))
    database.adapter.execute("CREATE TABLE t (v TEXT)")
    _insert(database, "before")
    return database


def _needs_sigint(how: str) -> None:
    if how != "sigint":
        return
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("a signal is handled in the main thread")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")


def _left_by_me(database: SQLerDB):
    """What the calling thread left: (the turn's lock is held, the thread is the holder,
    its end hook is there and names it), or the type of what reading it raised."""
    shared, local = database.adapter._shared, database.adapter._local
    try:
        return shared.lock.locked(), shared._holder is local.me, local.turn_end.token is local.me
    except AttributeError as exc:
        return type(exc)


def _worker_that_idles(
    body,
) -> tuple[tuple[threading.Thread, list[BaseException]], list, threading.Event]:
    """Start a thread that runs ``body`` (which returns what it reports), then waits for
    the returned event before it ends; returns once ``body`` ran."""
    report: list = []
    ran, go_on = threading.Event(), threading.Event()

    def work() -> None:
        try:
            report.append(body())
        finally:
            ran.set()
        assert go_on.wait(10)

    started = _start(work)
    assert ran.wait(10)
    return started, report, go_on


def _what_left(statement, point: str, how: str):
    """Run ``statement`` with an exception landed at ``point``: (what left it, where it landed)."""
    with raising_at(point, how) as fired:
        try:
            statement()
            left = None
        except BaseException as exc:  # noqa: BLE001 - what left is the result
            left = type(exc)
    return left, fired


@pytest.mark.parametrize(("point", "where", "how"), SKIPPED_PARTS)
def test_a_read_left_by_an_exception_leaves_no_holder_that_keeps_another_thread_out(
    point, where, how
):
    _needs_sigint(how)
    database = _memory_db(2000)
    shared = database.adapter._shared
    held = point != "take-entry"  # the stale holder stays named; it keeps no one out
    try:
        if where == "main":
            assert _what_left(lambda: _count(database), point, how) == (RAISED[how], [point])
            left = _left_by_me(database)
            counts: list[int] = []
            other = _start(lambda: counts.append(_count(database)))
            assert _join_all([other]) == [False]
            assert (other[1], counts) == ([], [1])
            assert left == (False, held, True)
        else:
            worker, report, go_on = _worker_that_idles(
                lambda: (_what_left(lambda: _count(database), point, how), _left_by_me(database))
            )
            assert report[0][0] == (RAISED[how], [point])
            assert _count(database) == 1  # while the worker idles
            assert report[0][1] == (False, held, True)
            go_on.set()
            assert _join_all([worker]) == [False]
            assert worker[1] == []
        assert (_count(database), shared._holder, shared.lock.locked()) == (1, None, False)
    finally:
        database.close()


@pytest.mark.parametrize("how", ["trace", "another-thread"])
def test_a_write_whose_give_is_skipped_keeps_its_transaction_until_its_thread_ends(how):
    database = _memory_db(200)
    shared = database.adapter._shared
    try:
        worker, report, go_on = _worker_that_idles(
            lambda: (
                _what_left(
                    lambda: database.adapter.execute("INSERT INTO t VALUES ('of the worker')"),
                    "give-entry",
                    how,
                ),
                _left_by_me(database),
                shared.conn.in_transaction,
            )
        )
        assert (report[0][0], report[0][2]) == ((Skipped, ["give-entry"]), True)
        with pytest.raises(sqlite3.OperationalError) as raised:
            _count(database)  # the worker's transaction is open while it idles
        assert str(raised.value) == LOCKED
        go_on.set()
        assert _join_all([worker]) == [False]
        assert worker[1] == []
        _insert(database, "after")
        assert (_rows(database), shared._holder, report[0][1]) == (
            ["after", "before"],
            None,
            (False, True, True),
        )
    finally:
        database.close()


@pytest.fixture
def unraisable(monkeypatch) -> list:
    """What ``__del__`` methods raise from now on (a thread's end hook among them)."""
    seen: list = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: seen.append(type(u.exc_value)))
    return seen


def _ended_holder(database: SQLerDB, work, unraisable: list, meanwhile=lambda: None) -> int:
    """Run ``work`` in a thread that ends as the holder, with ``Skipped`` raised in its end
    hook (``end_of``) as it takes the turn's lock after the thread's end; returns its ident
    and what ``work`` returned. ``meanwhile`` runs while the thread still idles as the holder."""
    shared = database.adapter._shared
    worker, report, go_on = _worker_that_idles(lambda: (threading.get_ident(), work()))
    meanwhile()
    with shared.lock:  # the end hook waits for it
        go_on.set()
        set_skipped_once_it_waits_in(report[0][0], "end_of")
    assert _join_all([worker]) == [False]
    assert (worker[1], unraisable) == ([], [Skipped])
    return report[0]


def test_a_thread_whose_end_hook_raises_has_its_transaction_rolled_back_by_the_next_statement(
    unraisable,
):
    database = _memory_db(2000)

    def holder() -> None:
        database.adapter.begin_transaction()
        _in(database, "of the thread that ended")

    try:
        _ended_holder(database, holder, unraisable)
        _insert(database, "after")
        assert (_rows(database), database.adapter._shared._holder) == (["after", "before"], None)
    finally:
        database.close()


def test_a_new_thread_with_the_ident_of_a_dead_holder_joins_nothing_it_left(unraisable):
    """The dead thread's write is open, its ``give`` and its end hook were both skipped; a
    thread that gets its ident writes and commits only its own row."""
    database = _memory_db(2000)
    try:
        dead, left = _ended_holder(
            database,
            lambda: _what_left(
                lambda: _in(database, "of the thread that ended"), "give-entry", "trace"
            ),
            unraisable,
        )
        assert left == (Skipped, ["give-entry"])
        reused: list[bool] = []

        def writes_if_it_has_the_ident() -> None:
            reused.append(threading.get_ident() == dead)
            if reused[-1]:
                _insert(database, "of the new thread")

        for _ in range(100):  # the C library hands a joined thread's ident to the next one
            again = _start(writes_if_it_has_the_ident)
            assert _join_all([again]) == [False]
            assert again[1] == []
            if reused[-1]:
                break
        if not reused[-1]:
            pytest.skip("the C library gave none of 100 new threads the dead thread's ident")
        assert _rows(database) == ["before", "of the new thread"]
    finally:
        database.close()


def test_close_while_a_stale_holder_idles_closes_and_the_database_opens_again():
    database = _memory_db(2000)
    shared = database.adapter._shared
    try:
        worker, report, go_on = _worker_that_idles(
            lambda: _what_left(lambda: _count(database), "give-entry", "trace")
        )
        assert report == [(Skipped, ["give-entry"])]
        database.close()
        assert (shared._holder, shared.lock.locked()) == (None, False)
        go_on.set()
        assert _join_all([worker]) == [False]
        assert worker[1] == []
        database.adapter.connect()
        database.adapter.execute("CREATE TABLE t (v TEXT)")
        _insert(database, "again")
        assert _rows(database) == ["again"]
    finally:
        database.close()


def _read_in_a_block(database: SQLerDB) -> None:
    with database.adapter._holding() as conn:
        conn.execute("SELECT 1").fetchall()


@pytest.mark.parametrize("where", ["main", "worker"])
@pytest.mark.parametrize("how", ["trace", "another-thread"])
def test_a_block_holding_the_connection_whose_exit_is_skipped_keeps_no_other_thread_out(where, how):
    database = _memory_db(2000)
    shared = database.adapter._shared
    try:
        if where == "main":
            assert _what_left(lambda: _read_in_a_block(database), "give-entry", how) == (
                Skipped,
                ["give-entry"],
            )
            left = _left_by_me(database)
            counts: list[int] = []
            other = _start(lambda: counts.append(_count(database)))
            assert _join_all([other]) == [False]
            assert (other[1], counts) == ([], [1])
            assert left == (False, True, True)
        else:
            worker, report, go_on = _worker_that_idles(
                lambda: (
                    _what_left(lambda: _read_in_a_block(database), "give-entry", how),
                    _left_by_me(database),
                )
            )
            assert report[0][0] == (Skipped, ["give-entry"])
            assert _count(database) == 1  # while the worker idles
            assert report[0][1] == (False, True, True)
            go_on.set()
            assert _join_all([worker]) == [False]
            assert worker[1] == []
        assert (_count(database), shared._holder) == (1, None)
    finally:
        database.close()


def test_a_kept_rollback_whose_give_was_skipped_is_tried_by_the_next_statement_after_the_thread_ends():
    """The holder's rollback fails twice (the connection keeps its transaction), the give
    after it is skipped and the thread ends while rollbacks still fail; every later
    statement tries the rollback first and goes on once one succeeds."""
    database = _memory_db(200)
    conn = database.adapter._shared.conn
    left: list = []

    def holder() -> None:
        database.adapter.begin_transaction()
        _in(database, "of the thread that ended")
        conn.set_progress_handler(lambda: 1, 1)  # every statement is stopped
        left.append(
            _what_left(
                lambda: database.adapter.end_transaction(commit=False), "give-entry", "trace"
            )
        )

    try:
        ended = _start(holder)
        assert _join_all([ended]) == [False]
        assert (ended[1], left, conn.in_transaction) == ([], [(Skipped, ["give-entry"])], True)
        with pytest.raises(sqlite3.OperationalError) as raised:
            _insert(database, "refused")
        assert str(raised.value) == LOCKED
        conn.set_progress_handler(None, 1)
        _insert(database, "after")
        assert _rows(database) == ["after", "before"]
    finally:
        database.close()


def _waiting_count(
    database: SQLerDB,
) -> tuple[tuple[threading.Thread, list[BaseException]], list[int]]:
    """Start a thread that counts the rows; returns once it waits for the turn."""
    counts: list[int] = []
    waiter = _start(lambda: counts.append(_count(database)))
    _wait_for_waiters(database, 1)
    return waiter, counts


def test_a_thread_already_waiting_when_the_holders_give_is_skipped_goes_on_when_its_wait_runs_out():
    """The holder's commit runs and the ``give`` after it is skipped while another thread
    waits: no one wakes the waiter; at the end of its wait it reads the committed row."""
    database = _memory_db(2000)
    shared = database.adapter._shared
    opened, waited, go_on = threading.Event(), threading.Event(), threading.Event()
    left: list = []

    def holder() -> None:
        _in(database, "of the holder")
        opened.set()
        assert waited.wait(10)
        left.append(_what_left(database.adapter.commit, "give-entry", "trace"))
        assert go_on.wait(10)  # it lives on, with nothing open, while the other's wait runs out

    try:
        held = _start(holder)
        assert opened.wait(10)
        waiter, counts = _waiting_count(database)
        waited.set()
        assert _join_all([waiter]) == [False]
        go_on.set()
        assert _join_all([held]) == [False]
        assert (held[1], waiter[1], left, counts) == ([], [], [(Skipped, ["give-entry"])], [2])
        assert (shared._holder, shared.lock.locked()) == (None, False)
    finally:
        go_on.set()
        database.close()


def test_a_thread_already_waiting_when_the_holders_end_hook_raises_goes_on_when_its_wait_runs_out(
    unraisable,
):
    """The holder ends inside its write and its end hook raises while another thread
    waits: at the end of its wait the waiter rolls back what the dead thread left and reads."""
    database = _memory_db(3000)
    started: list = []
    try:
        _ended_holder(
            database,
            lambda: _in(database, "of the thread that ended"),
            unraisable,
            meanwhile=lambda: started.append(_waiting_count(database)),
        )
        waiter, counts = started[0]
        assert _join_all([waiter]) == [False]
        assert (waiter[1], counts, database.adapter._shared._holder) == ([], [1], None)
        assert _rows(database) == ["before"]
    finally:
        database.close()


def test_a_thread_that_is_not_the_holder_ends_while_the_turns_lock_is_held():
    """A thread that used the connection and holds nothing ends without the turn's lock:
    its end waits for no statement of another thread."""
    database = _memory_db(2000)
    shared = database.adapter._shared
    try:
        worker, report, go_on = _worker_that_idles(lambda: _count(database))
        with shared.lock:
            go_on.set()
            alive = _join_all([worker])
        assert (alive, worker[1], report, shared._holder) == ([False], [], [1], None)
    finally:
        database.close()


def test_a_block_holding_the_connection_keeps_another_thread_out_while_it_runs():
    """A block has no transaction open; while it runs, another thread's statement waits."""
    database = _memory_db(200)
    shared = database.adapter._shared
    inside, go_on = threading.Event(), threading.Event()
    open_in_the_block: list[bool] = []

    def block() -> None:
        with database.adapter._holding() as conn:
            conn.execute("SELECT 1").fetchall()
            open_in_the_block.append(conn.in_transaction)
            inside.set()
            assert go_on.wait(10)

    try:
        worker = _start(block)
        assert inside.wait(10)
        with pytest.raises(sqlite3.OperationalError) as raised:
            _count(database)
        assert (str(raised.value), open_in_the_block) == (LOCKED, [False])
        go_on.set()
        assert _join_all([worker]) == [False]
        assert (worker[1], _count(database), shared._holder) == ([], 1, None)
    finally:
        go_on.set()
        database.close()


def _unique_table(database: SQLerDB) -> None:
    database.adapter.execute("CREATE TABLE u (v TEXT UNIQUE)")
    database.adapter.execute("INSERT INTO u VALUES ('taken')")
    database.adapter.auto_commit()


def _ended_by_sqlite(database: SQLerDB) -> tuple[str, bool]:
    """Inside the calling thread's transaction, a write SQLite answers by rolling the whole
    transaction back: (the error's type name, SQLite still counts a transaction)."""
    try:
        database.adapter.execute("INSERT OR ROLLBACK INTO u VALUES ('taken')")
    except Exception as exc:  # noqa: BLE001 - its type is the result
        return type(exc).__name__, database.adapter._shared.conn.in_transaction
    return "nothing raised", database.adapter._shared.conn.in_transaction


def test_a_transaction_sqlite_ended_itself_keeps_another_thread_out_until_its_thread_ends_it():
    """The thread is still inside ``begin_transaction`` though SQLite counts nothing open:
    another thread's statement waits until the thread ends its transaction."""
    database = _memory_db(200)
    _unique_table(database)
    shared = database.adapter._shared
    go_on = threading.Event()
    seen: list = []

    def holder() -> None:
        database.adapter.begin_transaction()
        _in(database, "rolled back by SQLite")
        seen.append(_ended_by_sqlite(database))
        seen.append("idle")
        assert go_on.wait(10)
        with pytest.raises(TransactionStateError):  # it ends the broken transaction rolled back
            database.adapter.end_transaction(commit=False)

    try:
        worker = _start(holder)
        deadline = time.monotonic() + 10
        while seen[-1:] != ["idle"]:
            assert time.monotonic() < deadline and worker[0].is_alive(), seen
            time.sleep(0.001)
        with pytest.raises(sqlite3.OperationalError) as raised:
            _count(database)
        assert (str(raised.value), seen) == (LOCKED, [("TransactionStateError", False), "idle"])
        go_on.set()
        assert _join_all([worker]) == [False]
        assert (worker[1], _rows(database), shared._holder) == ([], ["before"], None)
    finally:
        go_on.set()
        database.close()


def test_a_thread_whose_transaction_broke_still_raises_after_another_thread_freed_the_turn():
    """SQLite ended the thread's whole transaction with a statement whose own ``give`` was
    skipped: the thread is left behind and another thread's statement frees the turn. The
    thread's own next statement raises as in any broken transaction, and its ends end it."""
    database = _memory_db(2000)
    _unique_table(database)
    adapter, shared = database.adapter, database.adapter._shared
    failed, freed = threading.Event(), threading.Event()
    seen: list = []

    def holder() -> None:
        adapter.begin_transaction()
        adapter.begin_transaction()
        ended = lambda: adapter.execute("INSERT OR ROLLBACK INTO u VALUES ('taken')")  # noqa: E731
        seen.append(_what_left(ended, "give-entry", "trace"))
        failed.set()
        assert freed.wait(10)
        with pytest.raises(TransactionStateError):
            _in(database, "after the failed end")
        for _ in range(2):  # the nested end sends nothing, the outermost one rolls back
            with pytest.raises(TransactionStateError):
                adapter.end_transaction(commit=True)
        _insert(database, "after the transaction")

    try:
        worker = _start(holder)
        assert failed.wait(10)
        freed_for_main = _count(database)  # frees the turn the thread was left with
        freed.set()
        assert _join_all([worker]) == [False]
        assert worker[1] == []
        assert seen == [(RAISED["trace"], ["give-entry"])]
        assert (freed_for_main, _rows(database), shared._holder) == (
            1,
            ["after the transaction", "before"],
            None,
        )
    finally:
        freed.set()
        database.close()


@pytest.mark.parametrize("place", ["memory", "file"])
def test_an_exception_at_any_line_of_an_outermost_rollback_leaves_no_transaction_to_join(
    tmp_path, place
):
    """Whatever line of ``end_transaction`` the exception lands on, the count and SQLite
    agree: the levels the count still has end rolled back, and the thread's next statement
    commits only its own row. Line by line until the end runs through."""
    landed: list[int] = []
    for line in range(100):
        folder = tmp_path / str(line)
        folder.mkdir()
        database = _place_db(place, folder)
        fired: list[int] = []
        try:
            database.adapter.begin_transaction()
            _in(database, "in the block")
            with tracing(trace_raising_at_line("end_transaction", line, fired)):
                try:
                    database.adapter.end_transaction(commit=False)
                except Skipped:
                    pass
            _end_open_levels(database)
            _insert(database, "after")
            assert (line, _rows(database)) == (line, ["after", "before"])
        finally:
            database.close()
        if not fired:
            break
        landed.extend(fired)
    assert (len(landed), line) == ({"memory": 13, "file": 12}[place], len(landed))
