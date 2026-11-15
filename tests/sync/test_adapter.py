import contextlib
import os
import threading
from sqlite3 import IntegrityError, OperationalError, ProgrammingError

import pytest
from sqler import SQLerDB
from sqler.adapter import AdapterABC, NotConnectedError, SQLiteAdapter
from sqler.exceptions import TransactionStateError
from sqler.migrations import Migration, MigrationRunner
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
