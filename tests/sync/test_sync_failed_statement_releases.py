"""A statement that fails outside a transaction must not keep the write lock.

The sync twin of ``tests/async/test_async_failed_statement_releases.py``.
Python's ``sqlite3`` opens a transaction before a write; when the write (or
the commit after it) fails outside an explicit transaction, the adapter rolls
that transaction back, so the thread's connection holds no lock and another
thread can write at once. Inside ``with db.transaction()`` a failed statement
leaves the transaction open and usable.

Transaction depth is per thread: one thread's ``with db.transaction()`` does
not make another thread "in a transaction".

Every wait on another thread is bounded; file databases use a 300 ms busy
timeout, so a lock kept by mistake shows as "database is locked" quickly.
"""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import threading

import pytest

from sqler import SQLerDB
from sqler.adapter.synchronous import SQLiteAdapter
from sqler.exceptions import QueryError, StaleVersionError

BUSY_MS = 300
BOUND_S = 5.0

# (statement, params, the sqlite3 error it raises); the table holds 'dup' already.
FAILING = {
    "syntax_error": ("INSERT INTO t (v) VALUSE ('x')", None, sqlite3.OperationalError),
    "constraint_violation": ("INSERT INTO t (v) VALUES (?)", ["dup"], sqlite3.IntegrityError),
    "param_count_mismatch": ("INSERT INTO t (v) VALUES (?)", ["a", "b"], sqlite3.ProgrammingError),
}


def _file_adapter(path: str) -> SQLiteAdapter:
    adapter = SQLiteAdapter(
        path,
        pragmas=[
            "PRAGMA foreign_keys = ON",
            f"PRAGMA busy_timeout = {BUSY_MS}",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = OFF",
        ],
    )
    adapter.connect()
    return adapter


@pytest.fixture
def adapter(tmp_path):
    a = _file_adapter(os.path.join(tmp_path, "t.db"))
    a.execute("CREATE TABLE t (v TEXT NOT NULL UNIQUE)")
    a.execute("INSERT INTO t (v) VALUES ('dup')")
    a.auto_commit()
    yield a
    a.close()


def _in_thread(fn, *args):
    """Run ``fn`` in a fresh thread (its own connection); return its result or raise its error."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn, *args).result(timeout=BOUND_S)


def _values(adapter: SQLiteAdapter) -> list[str]:
    return [r[0] for r in adapter.execute("SELECT v FROM t ORDER BY v").fetchall()]


# --- outside a transaction ----------------------------------------------------


@pytest.mark.parametrize("name", sorted(FAILING))
def test_failed_statement_outside_a_transaction_leaves_no_transaction(adapter, name):
    sql, params, error = FAILING[name]
    with pytest.raises(error):
        adapter.execute(sql, params)
    assert adapter._conn().in_transaction is False

    def write():
        adapter.execute("INSERT INTO t (v) VALUES ('other')")
        adapter.auto_commit()

    _in_thread(write)
    assert _values(adapter) == ["dup", "other"]


def test_failed_insert_document_then_another_thread_writes_at_once(tmp_path):
    db = SQLerDB(_file_adapter(os.path.join(tmp_path, "u.db")))
    db.insert_document("users", {"email": "a@x"})
    db.create_index("users", "email", unique=True)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_document("users", {"email": "a@x"})
    assert db.adapter._conn().in_transaction is False

    new_id = _in_thread(db.insert_document, "users", {"email": "b@x"})
    assert new_id == 2  # the failed insert's id was rolled back with it
    emails = [
        r[0]
        for r in db.adapter.execute(
            "SELECT json_extract(data, '$.email') FROM users ORDER BY _id"
        ).fetchall()
    ]
    assert emails == ["a@x", "b@x"]
    db.close()


def test_uncommitted_write_before_a_failed_statement_is_rolled_back(adapter):
    adapter.execute("INSERT INTO t (v) VALUES ('pending')")  # no auto_commit yet
    with pytest.raises(sqlite3.IntegrityError):
        adapter.execute("INSERT INTO t (v) VALUES ('dup')")
    assert adapter._conn().in_transaction is False
    assert _values(adapter) == ["dup"]


def test_failed_select_outside_a_transaction_leaves_no_transaction(adapter):
    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        adapter.execute("SELEC v FROM t")
    assert adapter._conn().in_transaction is False
    assert _values(adapter) == ["dup"]


def test_commit_that_fails_outside_a_transaction_leaves_no_transaction(adapter):
    # A deferred foreign key is checked at commit: the statement passes, the commit fails.
    adapter.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    adapter.execute(
        "CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    adapter.auto_commit()
    adapter.execute("INSERT INTO child (pid) VALUES (99)")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        adapter.auto_commit()
    assert adapter._conn().in_transaction is False
    assert adapter.execute("SELECT count(*) FROM child").fetchone()[0] == 0

    def write():
        adapter.execute("INSERT INTO t (v) VALUES ('other')")
        adapter.auto_commit()

    _in_thread(write)
    assert _values(adapter) == ["dup", "other"]


def test_executemany_whose_third_row_fails_stores_nothing_and_leaves_no_transaction(adapter):
    with pytest.raises(sqlite3.IntegrityError):
        adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"], ["dup"], ["c"]])
    assert adapter._conn().in_transaction is False
    assert _values(adapter) == ["dup"]


def test_executescript_that_fails_outside_a_transaction_leaves_no_transaction(adapter):
    with pytest.raises(sqlite3.IntegrityError):
        adapter.executescript(
            "BEGIN; INSERT INTO t (v) VALUES ('a'); INSERT INTO t (v) VALUES ('dup');"
        )
    assert adapter._conn().in_transaction is False
    assert _values(adapter) == ["dup"]


def test_raw_begin_then_failed_statement_is_rolled_back(adapter):
    """Pinned: a caller's own raw BEGIN is not an explicit transaction to the adapter.

    The adapter only knows ``begin_transaction``; a failure after a raw BEGIN
    rolls the raw transaction back like any other implicit one.
    """
    adapter.execute("BEGIN")
    adapter.execute("INSERT INTO t (v) VALUES ('raw')")
    with pytest.raises(sqlite3.IntegrityError):
        adapter.execute("INSERT INTO t (v) VALUES ('dup')")
    assert adapter._conn().in_transaction is False
    assert _values(adapter) == ["dup"]


# --- inside an explicit transaction --------------------------------------------


def test_failed_statement_inside_a_transaction_keeps_it_usable(tmp_path):
    db = SQLerDB(_file_adapter(os.path.join(tmp_path, "u.db")))
    db.insert_document("users", {"email": "a@x"})
    db.create_index("users", "email", unique=True)
    with db.transaction():
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_document("users", {"email": "a@x"})
        assert db.adapter._conn().in_transaction is True
        db.insert_document("users", {"email": "b@x"})
    assert db.adapter._conn().in_transaction is False
    emails = _in_thread(
        lambda: [
            r[0]
            for r in db.adapter.execute(
                "SELECT json_extract(data, '$.email') FROM users ORDER BY _id"
            ).fetchall()
        ]
    )
    assert emails == ["a@x", "b@x"]
    db.close()


def test_failed_statement_inside_a_nested_transaction_keeps_both_levels(adapter):
    adapter.begin_transaction()
    adapter.execute("INSERT INTO t (v) VALUES ('outer')")
    adapter.begin_transaction()  # savepoint
    with pytest.raises(sqlite3.IntegrityError):
        adapter.execute("INSERT INTO t (v) VALUES ('dup')")
    adapter.execute("INSERT INTO t (v) VALUES ('inner')")
    adapter.end_transaction(commit=True)
    assert adapter._conn().in_transaction is True
    adapter.end_transaction(commit=True)
    assert adapter._conn().in_transaction is False
    assert _in_thread(_values, adapter) == ["dup", "inner", "outer"]


def test_transaction_whose_commit_fails_leaves_no_transaction(adapter):
    adapter.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    adapter.execute(
        "CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    adapter.auto_commit()
    adapter.begin_transaction()
    adapter.execute("INSERT INTO child (pid) VALUES (99)")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        adapter.end_transaction(commit=True)
    assert (adapter.in_transaction, adapter._conn().in_transaction) == (False, False)
    assert adapter.execute("SELECT count(*) FROM child").fetchone()[0] == 0


# --- transaction depth is per thread --------------------------------------------


def test_another_threads_transaction_is_not_this_threads(adapter):
    entered = threading.Event()
    done = threading.Event()

    def hold():
        adapter.begin_transaction()
        entered.set()
        done.wait(BOUND_S)
        adapter.end_transaction(commit=False)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert entered.wait(BOUND_S)
        assert adapter.in_transaction is False
    finally:
        done.set()
        t.join(BOUND_S)


def test_thread_b_fails_a_write_while_thread_a_holds_a_transaction(adapter):
    """B's failed write is rolled back, so after A commits B writes at once."""
    entered = threading.Event()
    b_failed = threading.Event()

    def thread_a():
        adapter.begin_transaction()
        adapter.execute("INSERT INTO t (v) VALUES ('a')")
        entered.set()
        b_failed.wait(BOUND_S)
        adapter.end_transaction(commit=True)

    a = threading.Thread(target=thread_a)
    a.start()
    assert entered.wait(BOUND_S)
    try:
        # B: waits its busy timeout for A's write lock, then fails.
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            adapter.execute("INSERT INTO t (v) VALUES ('b')")
        assert adapter._conn().in_transaction is False
    finally:
        b_failed.set()
        a.join(BOUND_S)
    adapter.execute("INSERT INTO t (v) VALUES ('b')")
    adapter.auto_commit()
    assert _values(adapter) == ["a", "b", "dup"]


# --- versioned writes on the shared in-memory database --------------------------


def _reader_holding(db: SQLerDB, table: str) -> tuple[sqlite3.Connection, sqlite3.Cursor]:
    """A second connection to the same shared-cache database with an open SELECT on ``table``.

    The cursor is returned so it stays alive (and its statement open) until the caller closes it.
    """
    reader = sqlite3.connect(db.adapter.path, uri=True, check_same_thread=False)
    cur = reader.execute(f"SELECT _id FROM {table}")
    assert cur.fetchone() == (
        1,
    )  # row 2 remains: the statement stays open, holding a table read lock
    return reader, cur


def _seed_versioned(db: SQLerDB, table: str, promoted: bool) -> int:
    if promoted:
        db.adapter.execute(
            f"CREATE TABLE {table} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL, "
            f"_version INTEGER NOT NULL DEFAULT 0, name TEXT)"
        )
        db.adapter.auto_commit()
        first, _ = db.upsert_with_version_promoted(
            table, None, {"name": "n0", "k": 0}, None, ["name"]
        )
        db.upsert_with_version_promoted(table, None, {"name": "n1", "k": 0}, None, ["name"])
    else:
        first, _ = db.upsert_with_version(table, None, {"k": 0}, None)
        db.upsert_with_version(table, None, {"k": 0}, None)
    return first


@pytest.mark.parametrize("promoted", [False, True], ids=["plain", "promoted"])
def test_versioned_write_blocked_by_a_table_lock_ends_its_transaction(promoted):
    # the write waits timeout_ms for the reader's table lock, then raises
    db = SQLerDB.in_memory(timeout_ms=100)
    table = "vers"
    _id = _seed_versioned(db, table, promoted)
    reader, held = _reader_holding(db, table)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database table is locked"):
            if promoted:
                db.upsert_with_version_promoted(table, _id, {"name": "x", "k": 1}, 0, ["name"])
            else:
                db.upsert_with_version(table, _id, {"k": 1}, 0)
        assert db.adapter._conn().in_transaction is False
        assert db.adapter.in_transaction is False
    finally:
        held.close()
        reader.close()

    # A third thread writes at once once the reader is gone.
    new_id = _in_thread(db.insert_document, "other", {"v": 1})
    assert new_id == 1
    row = db.find_document_with_version(table, _id)
    assert (row["k"], row["_version"]) == (0, 0)
    db.close()


@pytest.mark.parametrize("promoted", [False, True], ids=["plain", "promoted"])
def test_versioned_write_stale_ends_its_transaction(promoted):
    db = SQLerDB.in_memory()
    table = "vers"
    _id = _seed_versioned(db, table, promoted)
    with pytest.raises(StaleVersionError):
        if promoted:
            db.upsert_with_version_promoted(table, _id, {"name": "x", "k": 1}, 7, ["name"])
        else:
            db.upsert_with_version(table, _id, {"k": 1}, 7)
    assert db.adapter._conn().in_transaction is False
    assert _in_thread(db.insert_document, "other", {"v": 1}) == 1
    db.close()


@pytest.mark.parametrize("promoted", [False, True], ids=["plain", "promoted"])
def test_versioned_write_interrupted_after_begin_ends_its_transaction(promoted, monkeypatch):
    """Whatever ends the call after BEGIN IMMEDIATE (here a KeyboardInterrupt), no transaction stays."""
    db = SQLerDB.in_memory()
    table = "vers"
    _id = _seed_versioned(db, table, promoted)
    real = db.adapter.execute

    def execute(query, params=None):
        if query.lstrip().startswith("UPDATE"):
            raise KeyboardInterrupt
        return real(query, params)

    monkeypatch.setattr(db.adapter, "execute", execute)
    with pytest.raises(KeyboardInterrupt):
        if promoted:
            db.upsert_with_version_promoted(table, _id, {"name": "x", "k": 1}, 0, ["name"])
        else:
            db.upsert_with_version(table, _id, {"k": 1}, 0)
    monkeypatch.undo()
    assert db.adapter._conn().in_transaction is False
    assert _in_thread(db.insert_document, "other", {"v": 1}) == 1
    db.close()


# --- executemany and executescript inside an explicit transaction --------------


def test_executemany_inside_a_transaction_is_rolled_back_with_it(adapter):
    adapter.begin_transaction()
    adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"]])
    assert adapter._conn().in_transaction is True
    adapter.execute("INSERT INTO t (v) VALUES ('after')")
    adapter.end_transaction(commit=False)
    assert adapter._conn().in_transaction is False
    assert _in_thread(_values, adapter) == ["dup"]


def test_executemany_inside_a_transaction_is_committed_with_it(adapter):
    adapter.begin_transaction()
    adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"]])
    adapter.end_transaction(commit=True)
    assert _in_thread(_values, adapter) == ["a", "b", "dup"]


def test_executemany_in_a_nested_transaction_whose_inner_part_rolls_back(adapter):
    adapter.begin_transaction()
    adapter.execute("INSERT INTO t (v) VALUES ('outer')")
    adapter.begin_transaction()  # savepoint
    adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"]])
    adapter.end_transaction(commit=False)
    assert adapter._conn().in_transaction is True
    adapter.end_transaction(commit=True)
    assert _in_thread(_values, adapter) == ["dup", "outer"]


def test_executemany_whose_third_row_fails_inside_a_transaction_keeps_it_usable(adapter):
    """Pinned: the rows before the failing one stay in the open transaction."""
    adapter.begin_transaction()
    adapter.execute("INSERT INTO t (v) VALUES ('pre')")
    with pytest.raises(sqlite3.IntegrityError):
        adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"], ["dup"], ["c"]])
    assert adapter._conn().in_transaction is True
    adapter.execute("INSERT INTO t (v) VALUES ('post')")
    adapter.end_transaction(commit=False)
    assert _in_thread(_values, adapter) == ["dup"]


def test_executemany_outside_a_transaction_commits(adapter):
    adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"]])
    assert adapter._conn().in_transaction is False
    assert _in_thread(_values, adapter) == ["a", "b", "dup"]


def test_executescript_inside_a_transaction_is_refused_and_keeps_it(adapter):
    adapter.begin_transaction()
    adapter.execute("INSERT INTO t (v) VALUES ('pre')")
    with pytest.raises(QueryError, match="executescript cannot run inside a transaction"):
        adapter.executescript("INSERT INTO t (v) VALUES ('script');")
    assert adapter._conn().in_transaction is True
    adapter.execute("INSERT INTO t (v) VALUES ('post')")
    adapter.end_transaction(commit=False)
    assert _in_thread(_values, adapter) == ["dup"]


def test_executescript_outside_a_transaction_runs_and_commits(adapter):
    adapter.executescript("INSERT INTO t (v) VALUES ('a'); INSERT INTO t (v) VALUES ('b');")
    assert adapter._conn().in_transaction is False
    assert _in_thread(_values, adapter) == ["a", "b", "dup"]
