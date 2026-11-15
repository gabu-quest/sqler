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
import threading
import time
from contextlib import nullcontext

import pytest
from sqler import SQLerDB
from sqler.adapter.synchronous import SQLiteAdapter
from sqler.exceptions import NotConnectedError

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
        "BEGIN IMMEDIATE", "INSERT INTO t VALUES ('a')", a_end,
        "BEGIN IMMEDIATE", "INSERT INTO t VALUES ('b')", b_end,
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
    assert sent == ["BEGIN IMMEDIATE", "INSERT INTO t VALUES ('a')", "ROLLBACK", "BEGIN ", "INSERT INTO t VALUES ('b')", "COMMIT"]


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
    assert sent == ["BEGIN ", "INSERT INTO t VALUES ('x')", "ROLLBACK", "BEGIN ", "INSERT INTO t VALUES ('b')", "COMMIT"]


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
            (sqlite3.OperationalError, "database is locked: another thread's transaction holds the in-memory connection")
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
        "BEGIN IMMEDIATE", "INSERT INTO t VALUES ('a')", "ROLLBACK",
        "BEGIN IMMEDIATE", "INSERT INTO t VALUES ('b')", "COMMIT",
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
        "BEGIN IMMEDIATE", "INSERT INTO t VALUES ('a')", "ROLLBACK",
        "BEGIN IMMEDIATE", "INSERT INTO t VALUES ('b')", "COMMIT",
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
            "BEGIN IMMEDIATE", "SAVEPOINT sp_1", "SAVEPOINT sp_2", "INSERT INTO t VALUES ('deep')",
            "RELEASE SAVEPOINT sp_2", "RELEASE SAVEPOINT sp_1", "COMMIT",
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


def test_a_cursor_read_while_another_threads_transaction_is_open_yields_what_its_statement_sorted(db):
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
    database = SQLerDB.in_memory(shared=False) if kind == "memory" else SQLerDB.on_disk(str(tmp_path / "w.db"))
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
    database = SQLerDB.in_memory(shared=False) if kind == "memory" else SQLerDB.on_disk(str(tmp_path / "w.db"))
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
        assert time.monotonic() < deadline, f"{database.adapter._shared._waiting} of {n} threads came to wait"
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
        "INSERT INTO t VALUES ('plain-1')", "INSERT INTO t VALUES ('plain-2')", "INSERT INTO t VALUES ('txn')",
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


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="needs a real signal to the main thread")
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


def test_a_cursor_read_across_another_threads_transaction_and_its_rollback_holds_only_committed_rows(db):
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


@pytest.mark.parametrize("inside", [False, True], ids=["outside-a-transaction", "inside-its-transaction"])
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
            (sqlite3.OperationalError, "database is locked: another thread's transaction holds the in-memory connection")
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
            database.upsert_with_version("docs", None, {"by": "a"}, None)  # creates the versioned table
            # ends inside the block: its transaction is rolled back, the table with it

        thread, raised = _start(a)
        thread.join(10)
        assert raised == []
        _id, version = database.upsert_with_version("docs", None, {"by": "main"}, None)
        assert (_id, version) == (1, 0)
        assert database.find_document_with_version("docs", 1) == {"_id": 1, "_version": 0, "by": "main"}
    finally:
        database.close()
