"""Each thread that uses a SQLerDB has its own connection: a thread that ends gives it back.

The connection of an ended thread is closed as the thread ends (before ``join()`` returns),
without ``close()``; a live thread, a thread-pool worker and the main thread keep theirs.
"""

import concurrent.futures
import gc
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest

from sqler import SQLerDB
from sqler.adapter.synchronous import SQLiteAdapter
from sqler.exceptions import NotConnectedError
from sqler.logging import query_logger

HAS_PROC_FD = os.path.isdir("/proc/self/fd")


def _open_files() -> int:
    return len(os.listdir("/proc/self/fd"))


def _join(t: threading.Thread, what: str) -> None:
    """Join a thread the test started; a thread that does not end fails the test, naming it."""
    t.join(30)
    assert not t.is_alive(), f"{what} was still running after 30 s"


def _run_in_thread(target) -> None:
    """Run ``target`` on a new thread and join it; re-raise what it raised."""
    raised: list[BaseException] = []

    def body():
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            raised.append(exc)

    t = threading.Thread(target=body)
    t.start()
    t.join()
    if raised:
        raise raised[0]


@pytest.fixture
def db(tmp_path):
    database = SQLerDB.on_disk(str(tmp_path / "t.db"))
    database.insert_document("items", {"n": 1})
    yield database
    database.close()


@pytest.fixture
def no_wait_db(tmp_path):
    """A file database whose writes fail at once on a held lock instead of waiting."""
    path = str(tmp_path / "locks.db")
    database = SQLerDB(
        SQLiteAdapter(path, pragmas=["PRAGMA journal_mode = WAL", "PRAGMA busy_timeout = 0"])
    )
    database.insert_document("items", {"who": "main"})
    yield database
    database.close()


# ---- an ended thread holds no connection ----


@pytest.mark.parametrize("collect", ["gc enabled", "gc disabled"])
def test_200_ended_threads_leave_only_the_main_threads_connection(db, collect):
    if collect == "gc disabled":
        gc.disable()
    try:
        # SQLite may keep one file of a closed connection open while another connection of
        # the process holds a lock on it, so the bar is the count after one ended thread
        _run_in_thread(lambda: db.find_document("items", 1))
        before = _open_files() if HAS_PROC_FD else 0
        for _ in range(199):  # one after the other: never two alive at once
            _run_in_thread(lambda: db.find_document("items", 1))
        held = len(db.adapter._conns)
        after = _open_files() if HAS_PROC_FD else 0
    finally:
        gc.enable()
    assert held == 1  # the main thread's
    if HAS_PROC_FD:
        assert after == before


def test_an_ended_threads_connection_is_closed_when_join_returns(db):
    conns: list[sqlite3.Connection] = []
    _run_in_thread(lambda: conns.append(db.adapter._conn()))
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conns[0].execute("SELECT 1")


def test_every_new_thread_gets_a_fresh_open_connection_never_an_ended_ones(db):
    # Thread ids are reused by the OS, so a thread may meet the slot of an ended thread.
    # Holding every connection keeps them all alive, so identity means "the same connection".
    seen: list[sqlite3.Connection] = []
    docs: list[object] = []

    def later():
        seen.append(db.adapter._conn())
        docs.append(db.find_document("items", 1))

    for _ in range(30):
        _run_in_thread(later)
    assert len({id(conn) for conn in seen}) == 30
    assert docs == [{"_id": 1, "n": 1}] * 30
    for conn in seen:  # each was closed as its thread ended
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")


# ---- a live thread keeps its connection ----


def test_a_running_thread_keeps_its_connection_while_others_end(db):
    used = threading.Event()
    go_on = threading.Event()
    ids: list[int] = []
    late: list[str] = []

    def long_lived():
        ids.append(id(db.adapter._conn()))
        used.set()
        if not go_on.wait(30):
            late.append("go_on")
        ids.append(id(db.adapter._conn()))
        assert db.find_document("items", 1) == {"_id": 1, "n": 1}

    t = threading.Thread(target=long_lived)
    t.start()
    try:
        assert used.wait(30), "the long-lived thread never signalled `used` within 30 s"
        main_conn = db.adapter._conn()
        for _ in range(20):
            _run_in_thread(lambda: db.find_document("items", 1))
        held_meanwhile = len(db.adapter._conns)
    finally:
        go_on.set()
        _join(t, "the long-lived thread")
    assert late == [], "the long-lived thread timed out waiting for `go_on`"
    assert held_meanwhile == 2  # main + the running thread
    assert ids[0] == ids[1]
    assert db.adapter._conn() is main_conn
    assert len(db.adapter._conns) == 1


def test_a_pool_of_4_workers_keeps_the_same_4_connections_over_400_jobs(db):
    def job(_):
        assert db.find_document("items", 1) == {"_id": 1, "n": 1}
        return id(db.adapter._conn()), len(db.adapter._conns)

    meet = threading.Barrier(4, timeout=30)

    def first_job(_):
        db.find_document("items", 1)
        meet.wait()  # four jobs held at once: each runs on a different worker
        return job(_)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        # every worker opens its connection first, so the count is 5 for every job after
        list(pool.map(first_job, range(4)))
        results = list(pool.map(job, range(400)))
        assert len(db.adapter._conns) == 5  # main + 4 workers, between jobs
    assert len({conn_id for conn_id, _ in results}) == 4
    assert {count for _, count in results} == {5}
    assert len(db.adapter._conns) == 1  # the workers ended with the pool


def test_reads_on_one_thread_run_one_statement_each(db):
    db.find_document("items", 1)
    with query_logger.capture() as captured:
        for _ in range(10):
            db.find_document("items", 1)
    assert len(captured) == 10


# ---- a thread that ends inside a transaction loses it ----


def _end_inside_db_transaction(database):
    tx = database.transaction()
    tx.__enter__()
    database.insert_document("items", {"who": "ended"})
    try:
        raise RuntimeError("the thread gives up and never leaves the transaction")
    except RuntimeError:
        pass


def _end_inside_raw_transaction(database):
    database.adapter.begin_transaction()
    database.insert_document("items", {"who": "ended"})


@pytest.mark.parametrize(
    "end_thread",
    [_end_inside_db_transaction, _end_inside_raw_transaction],
    ids=["db.transaction", "begin_transaction"],
)
def test_a_thread_that_ends_in_a_transaction_frees_the_write_lock(no_wait_db, end_thread):
    _run_in_thread(lambda: end_thread(no_wait_db))
    _run_in_thread(lambda: no_wait_db.insert_document("items", {"who": "next"}))
    rows = no_wait_db.execute_sql(
        "SELECT json_extract(data, '$.who') AS who FROM items ORDER BY _id"
    )
    assert [row["who"] for row in rows] == ["main", "next"]
    assert no_wait_db.adapter._txn_depth == 0


# ---- close() ----


def test_threads_ending_while_another_thread_closes(tmp_path):
    for round_no in range(30):
        database = SQLerDB.on_disk(str(tmp_path / f"c{round_no}.db"))
        database.insert_document("items", {"n": 1})
        start = threading.Barrier(3, timeout=30)
        errors: list[BaseException] = []

        def reader():
            try:
                database.find_document("items", 1)
                start.wait()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        def closer():
            try:
                start.wait()
                database.close()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=closer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            _join(t, "a reader or the closer")
        assert errors == []
        assert len(database.adapter._conns) == 0


def test_close_from_another_thread_closes_a_live_threads_connection(db):
    used = threading.Event()
    go_on = threading.Event()
    seen: list[str] = []
    late: list[str] = []

    def live():
        db.find_document("items", 1)
        own = db.adapter._conn()
        used.set()
        if not go_on.wait(30):
            late.append("go_on")
        try:
            own.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            seen.append(str(exc))

    t = threading.Thread(target=live)
    t.start()
    try:
        assert used.wait(30), "the live thread never signalled `used` within 30 s"
        _run_in_thread(db.close)
    finally:
        go_on.set()
        _join(t, "the live thread")
    assert late == [], "the live thread timed out waiting for `go_on`"
    assert seen == ["Cannot operate on a closed database."]
    assert len(db.adapter._conns) == 0


def test_the_same_thread_after_close_is_not_connected_until_connect(db):
    db.close()
    with pytest.raises(NotConnectedError):
        db.find_document("items", 1)
    db.adapter.connect()
    assert db.find_document("items", 1) == {"_id": 1, "n": 1}


def test_a_live_thread_after_close_and_connect_works_again(db):
    used = threading.Event()
    closed_and_reconnected = threading.Event()
    result: list[object] = []
    raised: list[BaseException] = []
    late: list[str] = []

    def live():
        db.find_document("items", 1)
        used.set()
        if not closed_and_reconnected.wait(30):
            late.append("closed_and_reconnected")
        try:
            result.append(db.find_document("items", 1))
            result.append(db.adapter._txn_depth)
        except BaseException as exc:  # noqa: BLE001 - checked below
            raised.append(exc)

    t = threading.Thread(target=live)
    t.start()
    try:
        assert used.wait(30), "the live thread never signalled `used` within 30 s"
        db.close()
        db.adapter.connect()
    finally:
        closed_and_reconnected.set()
        _join(t, "the live thread")
    assert late == [], "the live thread timed out waiting for `closed_and_reconnected`"
    assert raised == []
    assert result == [{"_id": 1, "n": 1}, 0]


def test_a_transaction_depth_does_not_survive_close_and_connect(db):
    db.adapter.begin_transaction()
    assert db.adapter._txn_depth == 1
    db.close()
    db.adapter.connect()
    assert db.adapter._txn_depth == 0


# ---- a shared in-memory database lives from connect() to close() ----


def _shared_memory_db() -> SQLerDB:
    return SQLerDB.in_memory()


def test_a_shared_memory_database_made_on_a_thread_that_ends_is_read_from_main():
    box: dict = {}

    def make():
        box["db"] = _shared_memory_db()
        box["db"].insert_document("things", {"name": "made on a thread"})

    _run_in_thread(make)
    try:
        assert box["db"].find_document("things", 1) == {"_id": 1, "name": "made on a thread"}
    finally:
        box["db"].close()


def test_a_shared_memory_database_written_by_three_threads_that_each_end_first():
    database = _shared_memory_db()
    try:
        for n in range(3):
            _run_in_thread(lambda n=n: database.insert_document("things", {"n": n}))
        assert database.adapter.execute("SELECT count(*) FROM things").fetchone()[0] == 3
    finally:
        database.close()


def test_a_shared_memory_database_handed_on_with_no_thread_alive():
    box: dict = {}

    def make():
        box["db"] = _shared_memory_db()
        box["db"].insert_document("things", {"name": "a"})

    _run_in_thread(make)
    _run_in_thread(lambda: box["db"].insert_document("things", {"name": "b"}))
    box["rows"] = []
    _run_in_thread(
        lambda: box["rows"].append(
            box["db"].adapter.execute("SELECT count(*) FROM things").fetchone()[0]
        )
    )
    try:
        assert box["rows"] == [2]
    finally:
        box["db"].close()


def test_close_from_another_thread_drops_the_shared_memory_database():
    uri_name = "sqler-anchor-close-other-thread"
    database = SQLerDB.in_memory(name=uri_name)
    database.insert_document("things", {"n": 1})
    _run_in_thread(database.close)
    assert database.adapter._anchor is None
    probe = sqlite3.connect(f"file:{uri_name}?mode=memory&cache=shared", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            probe.execute("SELECT count(*) FROM things")
    finally:
        probe.close()


def test_close_twice_and_connect_twice_hold_one_anchor():
    database = _shared_memory_db()
    anchor = database.adapter._anchor
    assert anchor is not None
    database.adapter.connect()
    assert database.adapter._anchor is anchor
    database.close()
    database.close()
    assert database.adapter._anchor is None
    database.adapter.connect()  # a new, empty database
    assert database.adapter._anchor is not None
    assert database.adapter._anchor is not anchor
    database.close()


def test_two_adapters_on_one_named_shared_memory_database_each_hold_their_own_anchor():
    first = SQLerDB.in_memory(name="sqler-two-anchors")
    second = SQLerDB.in_memory(name="sqler-two-anchors")
    try:
        assert first.adapter._anchor is not None
        assert second.adapter._anchor is not None
        assert first.adapter._anchor is not second.adapter._anchor
        first.insert_document("things", {"n": 1})
        first.close()
        assert second.adapter.execute("SELECT count(*) FROM things").fetchone()[0] == 1
    finally:
        second.close()


def test_a_file_database_and_a_private_memory_database_open_no_anchor(db):
    assert db.adapter._anchor is None
    private = SQLerDB.in_memory(shared=False)
    assert private.adapter._anchor is None
    private.close()


@pytest.mark.parametrize(
    "uri",
    [
        "file::memory:?cache=shared",
        "file:tc_by_hand?cache=shared&mode=memory",
        "file:/tc_by_hand?vfs=memdb",
    ],
    ids=["memory-path", "mode-memory", "vfs-memdb"],
)
def test_a_shared_memory_database_named_by_hand_lives_until_close(uri):
    """However the URI names it: made on a thread that ended, it is read from main."""
    box: dict = {}

    def make():
        box["db"] = SQLerDB(SQLiteAdapter(uri))
        box["db"].insert_document("things", {"name": "made on a thread"})

    _run_in_thread(make)
    try:
        assert box["db"].find_document("things", 1) == {"_id": 1, "name": "made on a thread"}
    finally:
        box["db"].close()


def test_a_file_whose_path_holds_the_text_of_a_memory_uri_opens_no_anchor(tmp_path):
    folder = tmp_path / "mode=memory"
    folder.mkdir()
    database = SQLerDB.on_disk(str(folder / "vfs=memdb.db"))
    try:
        database.insert_document("items", {"n": 1})
        assert (database.adapter._anchor, (folder / "vfs=memdb.db").is_file()) == (None, True)
    finally:
        database.close()


def test_memory_database_shares_one_connection_across_threads():
    database = SQLerDB.in_memory(shared=False)
    database.insert_document("items", {"n": 1})
    conns: list[sqlite3.Connection] = []

    def use():
        conns.append(database.adapter._conn())
        assert database.find_document("items", 1) == {"_id": 1, "n": 1}

    for _ in range(3):
        _run_in_thread(use)
    assert conns[0] is conns[1] is conns[2] is database.adapter._conn()
    assert database.find_document("items", 1) == {"_id": 1, "n": 1}
    database.close()


# ---- a result handed over by a thread that ended ----


def test_a_cursor_from_an_ended_thread_cannot_be_read(db):
    handed: list[sqlite3.Cursor] = []
    _run_in_thread(lambda: handed.append(db.adapter.execute("SELECT _id FROM items")))
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        handed[0].fetchall()


# ---- the interpreter exits ----


def test_the_interpreter_exits_with_live_daemon_threads_and_an_unclosed_database(tmp_path):
    script = textwrap.dedent(
        """
        import sys
        import threading
        from sqler import SQLerDB

        db = SQLerDB.on_disk(sys.argv[1])
        db.insert_document("items", {"n": 1})
        ready = threading.Barrier(3, timeout=30)
        never = threading.Event()

        def reader():
            db.find_document("items", 1)
            ready.wait()
            never.wait()

        def writer():
            db.adapter.begin_transaction()
            db.insert_document("items", {"n": 2})
            ready.wait()
            never.wait()

        for target in (reader, writer):
            threading.Thread(target=target, daemon=True).start()
        ready.wait()
        for _ in range(5):
            t = threading.Thread(target=lambda: db.find_document("items", 1))
            t.start()
            t.join()
        print("done")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "exit.db")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "done\n", "")
