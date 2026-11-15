import contextlib
import ctypes
import os
import signal
import threading
from sqlite3 import IntegrityError, OperationalError, ProgrammingError
from typing import Optional

import pytest
from exception_points import stop_once_and_interrupt
from sqler import SQLerDB, SQLerModel
from sqler.adapter import AdapterABC, NotConnectedError, SQLiteAdapter
from sqler.exceptions import TransactionStateError
from sqler.migrations import Migration, MigrationRunner
from sqler.models import integrity
from sqler.ops import vacuum


@pytest.fixture(params=[("memory", "in_memory"), ("disk", "on_disk")])
def adapter(request, tmp_path):
    """yields a connected db in memory and on disk"""
    path = request.param
    if path == "disk":
        path = str(tmp_path / "test.db")
    else:
        path = ":memory:"

    adapter = SQLiteAdapter(path)
    adapter.connect()
    yield adapter
    adapter.close()


def test_db_implements_abc():
    """verify the inheritance is cool"""
    # also pytest needs any test at all to not fail on github action?
    assert issubclass(SQLiteAdapter, AdapterABC)


def test_factories(tmp_path):
    """makes sure the factory funcs work"""
    mem_adapter = SQLiteAdapter.in_memory()
    mem_adapter.connect()

    disk_path = str(tmp_path / "factory_test.db")
    disk_adapter = SQLiteAdapter.on_disk(disk_path)
    disk_adapter.connect()

    for adapter in [mem_adapter, disk_adapter]:
        cursor = adapter.execute("PRAGMA user_version;")
        assert isinstance(cursor.fetchone()[0], int)

    assert os.path.exists(disk_path)


def test_execute_and_commit(oligo_adapter):
    """tests basic execution"""
    oligo_adapter.execute("INSERT INTO oligos (length) VALUES (?);", [100])
    oligo_adapter.commit()
    cursor = oligo_adapter.execute("SELECT length FROM oligos;")
    assert cursor.fetchone()[0] == 100


def test_executemany_batch_insert(oligo_adapter):
    """tests execute many"""
    values = [[i] for i in range(100)]
    oligo_adapter.executemany("INSERT INTO oligos(length) VALUES (?);", values)
    cursor = oligo_adapter.execute("SELECT COUNT(*) FROM oligos;")
    assert cursor.fetchone()[0] == 100


def test_executescript(oligo_adapter):
    """test multiline scripts"""
    script = """
    INSERT INTO oligos(length) VALUES (1), (2);
    INSERT INTO oligos(length) VALUES (3);
    """
    oligo_adapter.executescript(script)

    # this part should fail
    with pytest.raises(ProgrammingError):
        oligo_adapter.execute(script)

    cursor = oligo_adapter.execute("SELECT length FROM oligos ORDER BY length;")
    assert [row[0] for row in cursor.fetchall()] == [1, 2, 3]


def test_context_manager(tmp_path):
    """test context manager commit"""
    path = str(tmp_path / "context_manager.db")
    with SQLiteAdapter(path) as adapter:
        adapter.execute("CREATE TABLE cm(x TEXT);")
        adapter.execute("INSERT INTO cm(x) VALUES (?);", ["hi"])
        adapter.commit()
        cursor = adapter.execute("SELECT x FROM cm;")
        assert cursor.fetchone()[0] == "hi"


def test_close_then_error(oligo_adapter):
    """ensure close disables operations"""
    oligo_adapter.close()
    with pytest.raises(NotConnectedError):
        oligo_adapter.execute("SELECT 1;")
    with pytest.raises(NotConnectedError):
        oligo_adapter.commit()
    with pytest.raises(NotConnectedError):
        oligo_adapter.executemany("SELECT 1;", [])


def test_execute_invalid_sql(oligo_adapter):
    """invalid sql raises OperationalError"""
    with pytest.raises(OperationalError):
        oligo_adapter.execute("THIS IS NOT VALID SQL")


def test_executemany_empty_list(oligo_adapter):
    """executemany with empty list should do nothing, not error"""
    oligo_adapter.execute("CREATE TABLE test_empty(x INTEGER);")
    oligo_adapter.executemany("INSERT INTO test_empty(x) VALUES (?);", [])
    cursor = oligo_adapter.execute("SELECT COUNT(*) FROM test_empty;")
    assert cursor.fetchone()[0] == 0


def test_commit_without_connection():
    """commit before connect should error"""
    adapter = SQLiteAdapter(":memory:")
    with pytest.raises(NotConnectedError):
        adapter.commit()


def test_multiple_connects_and_closes(tmp_path):
    """connect/close multiple times, then ensure closed disables executes"""
    path = str(tmp_path / "multi.db")
    adapter = SQLiteAdapter(path)
    adapter.connect()
    adapter.connect()
    adapter.close()
    adapter.close()
    with pytest.raises(NotConnectedError):
        adapter.execute("SELECT 1;")


def test_pragma_report_in_memory():
    """pragma_report returns expected keys with in-memory values."""
    adapter = SQLiteAdapter.in_memory(shared=False)
    adapter.connect()
    try:
        report = adapter.pragma_report()
        assert isinstance(report, dict)
        expected_keys = {
            "foreign_keys", "busy_timeout", "journal_mode", "synchronous",
            "cache_size", "wal_autocheckpoint", "mmap_size", "temp_store",
        }
        assert set(report.keys()) == expected_keys
        assert report["foreign_keys"] == 1
        assert report["journal_mode"] == "memory"
        assert report["synchronous"] == 0  # OFF for in-memory
        assert report["cache_size"] == -32000
        assert report["temp_store"] == 2  # MEMORY = 2
    finally:
        adapter.close()


def test_pragma_report_on_disk(tmp_path):
    """pragma_report returns on_disk PRAGMA values."""
    path = str(tmp_path / "pragma_test.db")
    adapter = SQLiteAdapter.on_disk(path)
    adapter.connect()
    try:
        report = adapter.pragma_report()
        assert report["foreign_keys"] == 1
        assert report["journal_mode"] == "wal"
        assert report["synchronous"] == 1  # NORMAL = 1
        assert report["busy_timeout"] == 5000
        assert report["cache_size"] == -64000
        assert report["mmap_size"] == 268435456
        assert report["temp_store"] == 2  # MEMORY = 2
    finally:
        adapter.close()


def test_context_manager_rollback_on_exception(tmp_path):
    """rollback on exception: should not commit inserts"""
    path = str(tmp_path / "cm_rollback.db")
    try:
        with SQLiteAdapter(path) as adapter:
            adapter.execute("CREATE TABLE foo(x INTEGER);")
            adapter.execute("INSERT INTO foo(x) VALUES (1);")
            raise RuntimeError("Force rollback")
    except RuntimeError:
        pass
    # Data should NOT be committed if rollback works right
    adapter2 = SQLiteAdapter(path)
    adapter2.connect()
    cursor = adapter2.execute("SELECT COUNT(*) FROM foo;")
    assert cursor.fetchone()[0] == 0


# ---- commit() and the adapter block inside a transaction opened by begin_transaction ----

COMMIT_INSIDE = (
    "commit() cannot end a transaction opened by begin_transaction: nothing was committed and the "
    "transaction goes on; end it by leaving its db.transaction() block, or with end_transaction()"
)


@pytest.fixture
def table(adapter):
    """``adapter`` (memory and disk) with a table ``t (v)`` holding ``before``, committed."""
    adapter.execute("CREATE TABLE t (v TEXT NOT NULL)")
    adapter.execute("INSERT INTO t VALUES ('before')")
    adapter.commit()
    return adapter


def _rows(adapter) -> list[str]:
    rows = [r[0] for r in adapter.execute("SELECT v FROM t ORDER BY rowid").fetchall()]
    adapter.auto_commit()
    return rows


def _commit_refused(adapter) -> None:
    with pytest.raises(TransactionStateError) as raised:
        adapter.commit()
    assert str(raised.value) == COMMIT_INSIDE


@pytest.mark.parametrize("commit", [False, True], ids=["rolled-back", "committed"])
def test_commit_inside_a_transaction_raises_and_the_transaction_goes_on(table, commit):
    table.begin_transaction()
    table.execute("INSERT INTO t VALUES ('first')")
    _commit_refused(table)
    assert table._txn_depth == 1
    table.execute("INSERT INTO t VALUES ('second')")
    table.end_transaction(commit=commit)
    assert _rows(table) == (["before", "first", "second"] if commit else ["before"])


def test_commit_inside_a_nested_transaction_and_after_it_ended_raises(table):
    table.begin_transaction()
    table.execute("INSERT INTO t VALUES ('first')")
    table.begin_transaction()
    table.execute("INSERT INTO t VALUES ('nested')")
    _commit_refused(table)
    table.end_transaction(commit=True)
    _commit_refused(table)
    assert table._txn_depth == 1
    table.end_transaction(commit=False)
    assert _rows(table) == ["before"]


def test_commit_in_a_transaction_whose_nested_end_failed_raises_that_error(table):
    """Passes on base: pins that the broken transaction's own sentence wins."""
    table.begin_transaction()
    table.execute("INSERT INTO t VALUES ('first')")
    table.begin_transaction()
    table.execute("RELEASE SAVEPOINT sp_1")  # ended behind sqler's back
    with pytest.raises(TransactionStateError):
        table.end_transaction(commit=True)
    with pytest.raises(TransactionStateError) as raised:
        table.commit()
    assert str(raised.value).startswith("a nested transaction could not be committed")
    with pytest.raises(TransactionStateError):
        table.end_transaction(commit=True)
    assert _rows(table) == ["before"]


def test_commit_after_an_outermost_commit_failed_is_a_commit_outside_a_transaction(adapter):
    """Passes on base: the failed end left no transaction, and commit() does what it does outside one."""
    adapter.execute("PRAGMA foreign_keys = ON")
    adapter.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    adapter.execute("CREATE TABLE c (p INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
    adapter.commit()
    adapter.begin_transaction()
    adapter.execute("INSERT INTO c VALUES (7)")
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        adapter.end_transaction(commit=True)
    adapter.commit()
    assert (adapter._txn_depth, adapter.execute("SELECT count(*) FROM c").fetchone()[0]) == (0, 0)


@pytest.mark.parametrize("raises", [False, True], ids=["block-ends", "block-raises"])
def test_the_adapter_block_inside_a_transaction_leaves_it_alone(table, raises):
    """Passes on base: the sync block already leaves the thread's transaction to its own end."""
    table.begin_transaction()
    table.execute("INSERT INTO t VALUES ('first')")
    with contextlib.suppress(ValueError):
        with table:
            table.execute("INSERT INTO t VALUES ('block')")
            if raises:
                raise ValueError("inner")
    assert table._txn_depth == 1
    table.end_transaction(commit=False)
    assert _rows(table) == ["before"]


def test_the_adapter_block_around_a_transaction_commits_what_the_transaction_committed(table):
    """Passes on base: block outside, transaction inside."""
    with table:
        table.begin_transaction()
        table.execute("INSERT INTO t VALUES ('in')")
        table.end_transaction(commit=True)
        table.execute("INSERT INTO t VALUES ('block')")
    assert _rows(table) == ["before", "in", "block"]


def test_another_threads_commit_on_the_memory_connection_waits_and_commits_nothing():
    """Passes on base: the other thread is in no transaction; it waits for the turn and gives up."""
    adapter = SQLiteAdapter(":memory:", timeout_ms=50)
    adapter.connect()
    try:
        adapter.execute("CREATE TABLE t (v TEXT NOT NULL)")
        adapter.commit()
        adapter.begin_transaction()
        adapter.execute("INSERT INTO t VALUES ('mine')")
        raised: list[str] = []

        def other() -> None:
            try:
                adapter.commit()
            except BaseException as exc:  # noqa: BLE001 - what it raises is the result
                raised.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        assert raised == [
            "OperationalError: database is locked: another thread's transaction holds the in-memory connection"
        ]
        adapter.end_transaction(commit=False)
        assert _rows(adapter) == []
    finally:
        adapter.close()


def test_commit_on_an_adapter_closed_inside_a_transaction_raises_not_connected(table):
    """Passes on base: close() ends the thread's transaction state with the connection."""
    table.begin_transaction()
    table.execute("INSERT INTO t VALUES ('first')")
    table.close()
    with pytest.raises(NotConnectedError):
        table.commit()


def test_a_migration_runner_made_inside_a_transaction_is_rolled_back_with_it(tmp_path):
    db = SQLerDB.on_disk(str(tmp_path / "m.db"))
    try:
        migration = Migration(1, "make", up=lambda d: d.adapter.execute("CREATE TABLE made (x)"))
        with pytest.raises(ValueError, match="undo"):
            with db.transaction():
                result = MigrationRunner(db, [migration]).migrate()
                assert (result.success, [r.version for r in result.applied]) == (True, [1])
                raise ValueError("undo")
        names = [r[0] for r in db.adapter.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()]
        assert names == []
    finally:
        db.close()


def test_vacuum_inside_a_transaction_raises_and_the_transaction_goes_on(tmp_path):
    """Passes on base: SQLite refuses the VACUUM before the commit after it is reached."""
    db = SQLerDB.on_disk(str(tmp_path / "v.db"))
    try:
        db.adapter.execute("CREATE TABLE t (v TEXT NOT NULL)")
        db.adapter.commit()
        with pytest.raises(ValueError, match="undo"):
            with db.transaction():
                db.adapter.execute("INSERT INTO t VALUES ('first')")
                with pytest.raises(OperationalError, match="cannot VACUUM from within a transaction"):
                    vacuum(db)
                assert db.adapter._txn_depth == 1
                raise ValueError("undo")
        assert db.adapter.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    finally:
        db.close()


# ---- raw work that SQLite holds in a transaction the adapter did not open ----

PENDING = (
    "a transaction cannot begin: SQLite holds a transaction on this connection that the adapter did not "
    "open (a raw BEGIN, or a raw write not committed yet); nothing was sent and that work is still pending: "
    'keep it with db.adapter.commit() or drop it with db.adapter.execute("ROLLBACK"), then begin again'
)


def _pending(adapter) -> tuple[bool, list[str]]:
    """Whether SQLite counts a transaction open on the thread's connection, and the rows it sees."""
    return adapter._conn().in_transaction, [r[0] for r in adapter.execute("SELECT v FROM t ORDER BY rowid")]


@pytest.mark.parametrize("raw", [["INSERT INTO t VALUES ('raw')"], ["BEGIN", "INSERT INTO t VALUES ('raw')"]])
def test_a_transaction_after_raw_uncommitted_work_is_refused_and_the_work_stays_pending(table, raw):
    for sql in raw:
        table.execute(sql)
    sent: list[str] = []
    table._conn().set_trace_callback(sent.append)
    with pytest.raises(TransactionStateError) as raised:
        with SQLerDB(table).transaction():
            table.execute("INSERT INTO t VALUES ('in the transaction')")
    table._conn().set_trace_callback(None)
    assert (str(raised.value), sent, table._txn_depth) == (PENDING, [], 0)
    assert _pending(table) == (True, ["before", "raw"])
    table.commit()  # the first way out: keep the work
    with SQLerDB(table).transaction():
        table.execute("INSERT INTO t VALUES ('after')")
    assert _pending(table) == (False, ["before", "raw", "after"])


def test_the_refused_work_can_be_rolled_back_with_a_raw_rollback(table):
    table.execute("INSERT INTO t VALUES ('raw')")
    with pytest.raises(TransactionStateError):
        table.begin_transaction()
    table.execute("ROLLBACK")  # the second way out: drop the work
    table.begin_transaction()
    table.end_transaction(commit=True)
    assert _pending(table) == (False, ["before"])


@pytest.mark.parametrize(
    "raw",
    [
        lambda a: a.execute("SELECT v FROM t"),  # its cursor is left unread
        lambda a: a.execute("PRAGMA user_version = 7"),
        lambda a: a.execute("CREATE TABLE made (x)"),
        lambda a: pytest.raises(OperationalError, a.execute, "INSERT INTO absent VALUES (1)"),
    ],
    ids=["unread-select", "pragma", "ddl", "failed-insert"],
)
def test_raw_statements_that_leave_no_transaction_open_let_a_transaction_begin(table, raw):
    """Passes on base: only a transaction SQLite holds refuses the begin."""
    raw(table)
    with SQLerDB(table).transaction():
        table.execute("INSERT INTO t VALUES ('in')")
    assert _pending(table) == (False, ["before", "in"])


def test_a_write_that_opens_no_transaction_commits_raw_work_left_pending_with_it(table):
    """Passes on base: as sqlite3 does, a write outside a transaction commits what is pending."""
    table.execute("INSERT INTO t VALUES ('raw')")
    SQLerDB(table).insert_document("docs", {"n": 1})
    assert _pending(table) == (False, ["before", "raw"])


def test_closing_with_raw_work_pending_rolls_it_back(tmp_path):
    """Passes on base."""
    path = str(tmp_path / "close.db")
    adapter = SQLiteAdapter(path)
    adapter.connect()
    adapter.execute("CREATE TABLE t (v TEXT NOT NULL)")
    adapter.commit()
    adapter.execute("INSERT INTO t VALUES ('raw')")
    adapter.close()
    adapter.connect()
    try:
        assert _pending(adapter) == (False, [])
    finally:
        adapter.close()


def test_another_threads_raw_work_on_the_memory_connection_keeps_the_turn_from_a_begin():
    """Passes on base: the pending work is the other thread's turn; this thread waits for it,
    gives up, and the other thread's work is still pending for it to commit."""
    adapter = SQLiteAdapter(":memory:", timeout_ms=50)
    adapter.connect()
    try:
        adapter.execute("CREATE TABLE t (v TEXT NOT NULL)")
        adapter.commit()
        wrote, done = threading.Event(), threading.Event()
        raised: list[str] = []

        def other() -> None:
            adapter.execute("INSERT INTO t VALUES ('theirs')")
            wrote.set()
            done.wait(5)
            adapter.commit()

        thread = threading.Thread(target=other)
        thread.start()
        assert wrote.wait(5)
        try:
            adapter.begin_transaction()
        except BaseException as exc:  # noqa: BLE001 - what it raises is the result
            raised.append(f"{type(exc).__name__}: {exc}")
        done.set()
        thread.join()
        assert raised == [
            "OperationalError: database is locked: another thread's transaction holds the in-memory connection"
        ]
        assert _pending(adapter) == (False, ["theirs"])
    finally:
        adapter.close()


# ---- outside a transaction, what was sent since the last commit is one unit ----
#
# A statement that fails ends the unit: all of it is rolled back and the error is raised. A
# library caller that reads a failure as an answer (the look for referrers) sends it with
# ``_execute_judged``, which ends nothing; ``_end_failed_statement`` ends what ``execute``
# would have ended and raises.


class RawItem(SQLerModel):
    __tablename__ = "raw_items"
    name: str = ""


class RawHolder(SQLerModel):
    __tablename__ = "raw_holders"
    target: Optional[RawItem] = None


def _holder_and_unreadable_row(adapter, unreadable: str) -> tuple[SQLerDB, int]:
    """A ``raw_items`` row ``a`` that one ``raw_holders`` row refers to, and a row SQLite's JSON
    functions cannot read, read ``unreadable`` (first or last), committed."""
    db = SQLerDB(adapter)
    item = RawItem(name="a").save(db=db)
    RawHolder(target=item).save(db=db)
    unreadable_row = f"{item._id} raw_items"
    if unreadable == "first":
        adapter.execute("UPDATE raw_holders SET _id = 5")
        adapter.execute("INSERT INTO raw_holders (_id, data) VALUES (1, ?)", [unreadable_row])
    else:
        adapter.execute("INSERT INTO raw_holders (data) VALUES (?)", [unreadable_row])
    adapter.commit()
    return db, item._id


def _name(adapter) -> str:
    return adapter.execute("SELECT json_extract(data, '$.name') FROM raw_items").fetchone()[0]


@pytest.mark.parametrize("unreadable", ["first", "last"])
def test_a_look_for_referrers_that_reads_a_failed_row_keeps_raw_work_pending(adapter, unreadable):
    """The look reads a table holding a row SQLite's JSON functions cannot read: the failed
    read (at the statement when the row is first, while rows are fetched when it is last)
    is an answer to it, so it must not roll back what was pending before it."""
    db, item_id = _holder_and_unreadable_row(adapter, unreadable)
    adapter.execute("UPDATE raw_items SET data = json_set(data, '$.name', 'raw')")  # not committed
    found = integrity.find_referrers(db, "raw_items", item_id)
    assert [(table, paths) for table, _, paths in found] == [("raw_holders", {"paths": ["target"]})]
    assert _name(adapter) == "raw"
    adapter.commit()
    assert _name(adapter) == "raw"


def test_a_look_whose_second_statement_fails_too_raises_and_ends_the_unit(adapter):
    """The unreadable row sends the look to its second statement, which SQLite stops (an
    interrupted read is no answer): the error is raised, and the write sent before the look
    is rolled back with it."""
    db, item_id = _holder_and_unreadable_row(adapter, "first")
    adapter.execute("UPDATE raw_items SET data = json_set(data, '$.name', 'raw')")  # not committed
    conn = adapter._conn()
    armed: list[str] = []

    def stop_once() -> int:
        if armed:
            armed.clear()
            return 1
        return 0

    conn.set_trace_callback(
        lambda sql: armed.append(sql) if sql.startswith("SELECT _id, data FROM raw_holders") and "json_" not in sql else None
    )
    conn.set_progress_handler(stop_once, 1)
    try:
        with pytest.raises(OperationalError, match="^interrupted$"):
            integrity.find_referrers(db, "raw_items", item_id)
    finally:
        conn.set_progress_handler(None, 1)
        conn.set_trace_callback(None)
    assert (conn.in_transaction, _name(adapter)) == (False, "a")


@pytest.fixture
def unique(table):
    table.execute("CREATE UNIQUE INDEX t_v ON t (v)")
    table.commit()
    return table


FAILING = {
    "constraint": lambda a: a.execute("INSERT INTO t VALUES ('before')"),
    "refused-when-prepared": lambda a: a.execute("SELECT (1, 2) = (SELECT 1, 2, 3)"),
    "executemany": lambda a: a.executemany("INSERT INTO t VALUES (?)", [["many"], ["before"]]),
}


@pytest.mark.parametrize("failing", list(FAILING))
def test_a_statement_that_fails_after_pending_work_rolls_the_whole_unit_back(unique, failing):
    """Passes on base: the failed statement ends all that was sent since the last commit, so
    no later commit makes part of the unit durable."""
    unique.execute("INSERT INTO t VALUES ('raw')")  # not committed
    with pytest.raises((IntegrityError, OperationalError)):
        FAILING[failing](unique)
    assert _pending(unique) == (False, ["before"])


@pytest.mark.parametrize("failing", list(FAILING))
def test_a_statement_that_fails_with_nothing_pending_leaves_no_transaction_open(unique, failing):
    """Passes on base: the transaction the failed statement opened is rolled back."""
    with pytest.raises((IntegrityError, OperationalError)):
        FAILING[failing](unique)
    assert _pending(unique) == (False, ["before"])


REFUSED_WRITE = "INSERT INTO t SELECT (1, 2) = (SELECT 1, 2, 3)"  # refused when prepared


def test_a_judged_failure_ends_nothing_and_the_second_call_ends_the_unit_and_raises(table):
    table.execute("INSERT INTO t VALUES ('first')")  # the unit's earlier write
    with pytest.raises(OperationalError, match="^row value misused$") as refused:
        table._execute_judged(REFUSED_WRITE)
    assert _pending(table) == (True, ["before", "first"])
    with pytest.raises(OperationalError) as ended:
        table._end_failed_statement(refused.value)
    assert (ended.value is refused.value, _pending(table)) == (True, (False, ["before"]))


def test_the_second_call_with_nothing_pending_raises_and_leaves_no_transaction(table):
    with pytest.raises(OperationalError) as refused:
        table._execute_judged(REFUSED_WRITE)
    with pytest.raises(OperationalError) as ended:
        table._end_failed_statement(refused.value)
    assert (ended.value is refused.value, _pending(table)) == (True, (False, ["before"]))


def test_a_judged_failure_and_the_second_call_inside_a_transaction_end_nothing(table):
    with SQLerDB(table).transaction():
        table.execute("INSERT INTO t VALUES ('a')")
        with pytest.raises(OperationalError) as refused:
            table._execute_judged(REFUSED_WRITE)
        with pytest.raises(OperationalError):
            table._end_failed_statement(refused.value)
        table.execute("INSERT INTO t VALUES ('b')")
    assert _pending(table) == (False, ["before", "a", "b"])


def test_the_second_call_on_the_memory_connection_hands_the_turn_on():
    """The judged failure leaves this thread's unit open, so the turn stays with it and the
    other thread's write waits; the second call rolls the unit back and frees the turn, as
    a failed ``execute`` does."""
    adapter = SQLiteAdapter(":memory:", timeout_ms=5000)
    adapter.connect()
    try:
        adapter.execute("CREATE TABLE t (v TEXT NOT NULL)")
        adapter.commit()
        adapter.execute("INSERT INTO t VALUES ('mine')")  # holds the turn until the unit ends
        done = threading.Event()
        raised: list[BaseException] = []

        def other() -> None:
            try:
                adapter.execute("INSERT INTO t VALUES ('theirs')")
                adapter.commit()
            except BaseException as exc:  # noqa: BLE001 - handed to the test thread
                raised.append(exc)
            done.set()

        thread = threading.Thread(target=other)
        thread.start()
        with pytest.raises(OperationalError) as refused:
            adapter._execute_judged(REFUSED_WRITE)
        assert done.is_set() is False
        with pytest.raises(OperationalError):
            adapter._end_failed_statement(refused.value)
        thread.join(10)
        assert (thread.is_alive(), raised, _pending(adapter)) == (False, [], (False, ["theirs"]))
    finally:
        adapter.close()


@pytest.mark.skipif(not hasattr(ctypes.CDLL(None), "raise"), reason="needs libc's raise()")
def test_an_interrupt_in_a_judged_statement_ends_the_unit(table):
    """An interrupt is never the caller's to judge: it ends the unit as in ``execute``."""
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    table.execute("INSERT INTO t VALUES ('first')")
    conn = table._conn()
    stop_once_and_interrupt(conn)
    try:
        with pytest.raises(KeyboardInterrupt):
            table._execute_judged("INSERT INTO t VALUES ('stopped')")
    finally:
        conn.set_progress_handler(None, 1)
    assert _pending(table) == (False, ["before"])
