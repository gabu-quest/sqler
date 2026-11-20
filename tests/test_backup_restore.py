"""backup and restore, on a file database and on the one connection of a plain ':memory:' database."""

import os
import re
import sqlite3
import tempfile
import threading
import time

import pytest

from sqler import SQLerDB, SQLerModel, backup, restore
from sqler.adapter.synchronous import SQLiteAdapter


class TestBackupRestore:
    """Tests for backup and restore functions."""

    def test_backup_creates_file(self):
        """Backup creates a valid database file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "source.db")
            backup_path = os.path.join(tmpdir, "backup.db")

            db = SQLerDB.on_disk(db_path)

            class User(SQLerModel):
                name: str

            User.set_db(db)
            User(name="Alice").save()
            User(name="Bob").save()

            result = backup(db, backup_path)

            assert result.success is True
            assert os.path.exists(backup_path)
            assert result.size_bytes > 0
            assert result.duration_ms >= 0

            db.close()

    def test_backup_result_to_dict(self):
        """BackupResult can be serialized with correct values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "source.db")
            backup_path = os.path.join(tmpdir, "backup.db")

            db = SQLerDB.on_disk(db_path)
            result = backup(db, backup_path)
            data = result.to_dict()

            assert data["success"] is True
            assert data["size_bytes"] > 0
            assert data["duration_ms"] >= 0
            assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", data["timestamp"])

            db.close()

    def test_restore_replaces_data(self):
        """Restore replaces database with backup contents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "main.db")
            backup_path = os.path.join(tmpdir, "backup.db")

            # Create original db with data
            db = SQLerDB.on_disk(db_path)

            class User(SQLerModel):
                name: str

            User.set_db(db)
            User(name="Alice").save()
            User(name="Bob").save()

            # Create backup
            backup(db, backup_path)

            # Add more data to original
            User(name="Charlie").save()
            assert User.count() == 3

            # Restore from backup
            result = restore(db, backup_path)

            assert result.success is True
            # After restore, should only have 2 users
            assert User.count() == 2

            db.close()

    def test_restore_missing_file_fails(self):
        """Restore fails gracefully for missing file."""
        db = SQLerDB.in_memory(shared=False)
        result = restore(db, "/nonexistent/path/backup.db")

        assert result.success is False
        assert "not found" in result.error.lower()


# ---- backup and restore on the one connection of a plain ":memory:" database ----


def _memory_db(timeout_ms: int = 120_000) -> SQLerDB:
    """A plain ":memory:" database (one connection every thread shares) with three committed rows."""
    database = SQLerDB(SQLiteAdapter(":memory:", timeout_ms=timeout_ms))
    database.adapter.execute("CREATE TABLE t (v TEXT)")
    database.adapter.executemany("INSERT INTO t VALUES (?)", [("c1",), ("c2",), ("c3",)])
    return database


def _file_rows(path: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")]
    finally:
        conn.close()


def _waiting(database: SQLerDB, n: int) -> None:
    """Return once ``n`` threads wait for the connection's turn; fails after 10 s."""
    deadline = time.monotonic() + 10
    while database.adapter._shared._waiting < n:
        assert time.monotonic() < deadline, "no thread came to wait for the turn"
        time.sleep(0.001)


def _other_thread_transaction(database: SQLerDB, commit: bool):
    """A thread that inserts 'dirty' in its own transaction, waits for one waiter, then ends it."""
    a_in, raised = threading.Event(), []

    def a():
        try:
            database.adapter.begin_transaction()
            database.adapter.execute("INSERT INTO t VALUES ('dirty')")
            a_in.set()
            _waiting(database, 1)
            database.adapter.end_transaction(commit=commit)
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            raised.append(exc)
            a_in.set()

    thread = threading.Thread(target=a)
    thread.start()
    assert a_in.wait(10)
    return thread, raised


def test_backup_of_a_memory_database_waits_for_another_threads_transaction(tmp_path):
    database = _memory_db()
    try:
        thread, raised = _other_thread_transaction(database, commit=False)
        result = backup(database, str(tmp_path / "b.db"))
        thread.join(10)
        assert (result.success, result.error, raised) == (True, None, [])
        assert _file_rows(str(tmp_path / "b.db")) == ["c1", "c2", "c3"]
    finally:
        database.close()


@pytest.mark.parametrize("kind", ["memory", "file"])
def test_backup_inside_the_threads_own_transaction_copies_nothing_and_says_why(kind, tmp_path):
    # SQLite answers "locked" to a backup whose source connection writes; it is refused at
    # once, not retried for ever, and the transaction goes on
    if kind == "memory":
        database = _memory_db()
    else:
        database = SQLerDB(SQLiteAdapter(str(tmp_path / "src.db")))
        database.adapter.execute("CREATE TABLE t (v TEXT)")
        database.adapter.executemany("INSERT INTO t VALUES (?)", [("c1",), ("c2",), ("c3",)])
    try:
        with database.transaction():
            database.adapter.execute("INSERT INTO t VALUES ('mine')")
            result = backup(database, str(tmp_path / "b.db"))
        assert (result.success, result.error) == (
            False,
            "backup cannot run inside this thread's open transaction",
        )
        assert sorted(os.listdir(tmp_path)) == ([] if kind == "memory" else ["src.db"])
        rows = database.adapter.execute("SELECT v FROM t ORDER BY v").fetchall()
        assert [r[0] for r in rows] == ["c1", "c2", "c3", "mine"]
    finally:
        database.close()


def _saved_backup(tmp_path) -> str:
    path = str(tmp_path / "saved.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('saved')")
    conn.commit()
    conn.close()
    return path


def test_restore_into_a_memory_database_waits_for_another_threads_transaction(tmp_path):
    source = _saved_backup(tmp_path)
    database = _memory_db()
    try:
        thread, raised = _other_thread_transaction(database, commit=True)
        result = restore(database, source)
        thread.join(10)
        assert (result.success, result.error, raised) == (True, None, [])
        assert [
            r[0] for r in database.adapter.execute("SELECT v FROM t ORDER BY v").fetchall()
        ] == ["saved"]
    finally:
        database.close()


def test_restore_into_a_memory_database_runs_out_and_leaves_the_other_threads_transaction(tmp_path):
    source = _saved_backup(tmp_path)
    database = _memory_db(timeout_ms=50)
    try:
        a_in, a_go, raised = threading.Event(), threading.Event(), []

        def a():
            try:
                with database.transaction():
                    database.adapter.execute("INSERT INTO t VALUES ('dirty')")
                    a_in.set()
                    assert a_go.wait(10)
                    raise RuntimeError("the other thread rolls back")
            except BaseException as exc:  # noqa: BLE001 - handed to the test thread
                raised.append(exc)

        thread = threading.Thread(target=a)
        thread.start()
        assert a_in.wait(10)
        result = restore(database, source)
        a_go.set()
        thread.join(10)
        assert (result.success, result.error) == (
            False,
            "database is locked: another thread's transaction holds the in-memory connection",
        )
        assert [type(e) for e in raised] == [RuntimeError]  # its own rollback, nobody else's commit
        assert [
            r[0] for r in database.adapter.execute("SELECT v FROM t ORDER BY v").fetchall()
        ] == ["c1", "c2", "c3"]
    finally:
        database.close()
