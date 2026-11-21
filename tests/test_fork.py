"""A process that forks while its threads use a database: the child never touches a
connection the parent opened, and a child that uses the database opens its own.

Every scenario runs in a Python process of its own, which forks there and waits for its
child with a deadline (a child stuck inside ``fork()`` is killed and reported as stuck), so
a failing scenario cannot hang the test run. Each scenario prints what it saw; the test
compares the whole output.

Connections are opened through a ``sqlite3.Connection`` subclass that records every
``close()`` and every finalization with the process it ran in and the process that opened
the connection: a child prints what happened in it to connections its parent opened, and
the expected record is empty.
"""

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="os.fork() does not exist on Windows"
)

_PRELUDE = textwrap.dedent(
    """
    import functools
    import os
    import signal
    import sqlite3
    import sys
    import threading
    import time

    EVENTS = []  # (what happened to a connection, the process it happened in, the opener)


    class Recorded(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.opener = os.getpid()

        def close(self):
            EVENTS.append(("close", os.getpid(), self.opener))
            super().close()

        def __del__(self):
            EVENTS.append(("finalize", os.getpid(), self.opener))


    sqlite3.connect = functools.partial(sqlite3.connect, factory=Recorded)

    from sqler import SQLerDB
    from sqler.adapter import SQLiteAdapter
    from sqler.exceptions import NotConnectedError

    PATH = sys.argv[1]
    SLOW = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 60000000) "
        "SELECT count(*) FROM c"
    )


    def say(*parts):
        print(*parts, flush=True)


    def in_child():  # what happened in this process to connections another process opened
        return [what for what, pid, opener in EVENTS if pid == os.getpid() != opener]


    def wait_child(pid, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return f"child exited {os.waitstatus_to_exitcode(status)}"
            time.sleep(0.02)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        return "child stuck"


    def count(sql):
        conn = sqlite3.connect(PATH)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


    def slow_worker(adapter):
        inside = threading.Event()

        def run():
            adapter.execute("SELECT 1").fetchall()
            inside.set()
            adapter.execute(SLOW).fetchall()

        threading.Thread(target=run, daemon=True).start()
        inside.wait()
        time.sleep(0.3)  # well inside the slow statement
    """
)


def _run(tmp_path, body: str) -> tuple[int, str, str]:
    script = _PRELUDE + textwrap.dedent(body)
    done = subprocess.run(
        [sys.executable, "-W", "ignore::DeprecationWarning", "-c", script, str(tmp_path / "f.db")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode, done.stdout, done.stderr


def test_a_child_forked_while_a_thread_is_inside_a_statement_exits(tmp_path):
    out = _run(
        tmp_path,
        """
        db = SQLerDB.on_disk(PATH)
        slow_worker(db.adapter)
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        say(wait_child(pid))
        os._exit(0)
        """,
    )
    assert out == (0, "child exited 0\n", "")


def test_a_child_forked_while_a_thread_holds_the_memory_connection_exits(tmp_path):
    out = _run(
        tmp_path,
        """
        db = SQLerDB.in_memory(shared=False)
        db.insert_document("items", {"n": 1})
        slow_worker(db.adapter)
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        say(wait_child(pid))
        os._exit(0)
        """,
    )
    assert out == (0, "child exited 0\n", "")


@pytest.mark.parametrize(
    "make, journal",
    [("SQLerDB.on_disk(PATH)", "wal"), ("SQLerDB(SQLiteAdapter(PATH))", "delete")],
    ids=["wal", "default"],
)
def test_idle_threads_keep_their_connections_after_a_child_exits(tmp_path, make, journal):
    out = _run(
        tmp_path,
        f"""
        db = {make}
        say(db.adapter.execute("PRAGMA journal_mode").fetchone()[0])
        db.insert_document("items", {{"by": "main"}})
        ready = threading.Barrier(4, timeout=30)
        go = threading.Event()
        rows = []

        def worker(name):
            own = db.insert_document("items", {{"by": name}})
            ready.wait()
            go.wait()
            rows.append((name, db.find_document("items", own)["by"]))
            db.insert_document("items", {{"by": name + " again"}})

        threads = [threading.Thread(target=worker, args=(f"t{{i}}",)) for i in range(3)]
        for t in threads:
            t.start()
        ready.wait()
        pid = os.fork()
        if pid == 0:
            say("child recorded", in_child())
            os._exit(0)
        say(wait_child(pid))
        go.set()
        for t in threads:
            t.join(30)
        say(sorted(rows))
        say(count("SELECT count(*) FROM items"))
        os._exit(0)
        """,
    )
    assert out == (
        0,
        f"{journal}\n"
        "child recorded []\n"
        "child exited 0\n"
        "[('t0', 't0'), ('t1', 't1'), ('t2', 't2')]\n"
        "[(7,)]\n",
        "",
    )


def test_a_child_reads_on_its_own_connection_outside_the_parents_transaction(tmp_path):
    # The child's write of C fails with SQLite's lock error: the parent holds the write lock
    # at the fork, and SQLite's record of the locks this process holds is copied into the
    # child with the rest of its memory, so the child's own connection finds that lock
    # still held whatever the parent does after the fork. Nothing is written, and it says so.
    out = _run(
        tmp_path,
        """
        db = SQLerDB.on_disk(PATH, timeout_ms=300)
        db.insert_document("items", {"name": "A"})
        db.adapter.begin_transaction()
        db.insert_document("items", {"name": "B"})
        pid = os.fork()
        if pid == 0:
            names = [row[0] for row in db.adapter.execute(
                "SELECT json_extract(data, '$.name') FROM items ORDER BY _id").fetchall()]
            say("child sees", names, "in a transaction:", db.adapter.in_transaction)
            try:
                db.insert_document("items", {"name": "C"})
            except sqlite3.OperationalError as exc:
                say("child write:", exc, "in a transaction:", db.adapter.in_transaction)
            say("child recorded", in_child())
            os._exit(0)
        say(wait_child(pid))
        say("committed before the parent commits", count(
            "SELECT json_extract(data, '$.name') FROM items ORDER BY _id"))
        say("parent in a transaction:", db.adapter.in_transaction)
        db.adapter.end_transaction(commit=True)
        say(count("SELECT json_extract(data, '$.name') FROM items ORDER BY _id"))
        os._exit(0)
        """,
    )
    assert out == (
        0,
        "child sees ['A'] in a transaction: False\n"
        "child write: database is locked in a transaction: False\n"
        "child recorded []\n"
        "child exited 0\n"
        "committed before the parent commits [('A',)]\n"
        "parent in a transaction: True\n"
        "[('A',), ('B',)]\n",
        "",
    )


@pytest.mark.parametrize(
    "make", ["SQLerDB.in_memory(shared=False)", "SQLerDB.in_memory()"], ids=["plain", "shared"]
)
def test_an_in_memory_database_cannot_be_used_in_a_child(tmp_path, make):
    out = _run(
        tmp_path,
        f"""
        db = {make}
        db.insert_document("items", {{"n": 1}})
        pid = os.fork()
        if pid == 0:
            try:
                db.find_document("items", 1)
            except NotConnectedError as exc:
                say("child:", exc)
            try:
                db.insert_document("items", {{"n": 2}})
            except NotConnectedError as exc:
                say("child again:", exc)
            say("child recorded", in_child())
            os._exit(0)
        say(wait_child(pid))
        say(db.adapter.execute("SELECT count(*) FROM items").fetchone()[0])
        os._exit(0)
        """,
    )
    message = (
        "this in-memory database was opened in another process: its rows live in that "
        "process and cannot be reached after fork(); open a database in this process"
    )
    assert out == (
        0,
        f"child: {message}\nchild again: {message}\nchild recorded []\nchild exited 0\n1\n",
        "",
    )


def test_close_in_a_child_leaves_the_parents_connections_open(tmp_path):
    out = _run(
        tmp_path,
        """
        db = SQLerDB.on_disk(PATH)
        db.insert_document("items", {"by": "main"})
        ready = threading.Barrier(3, timeout=30)
        go = threading.Event()
        rows = []

        def worker(name):
            own = db.insert_document("items", {"by": name})
            ready.wait()
            go.wait()
            rows.append((name, db.find_document("items", own)["by"]))
            db.insert_document("items", {"by": name + " again"})

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        ready.wait()
        pid = os.fork()
        if pid == 0:
            db.find_document("items", 1)  # the child's own connection
            db.adapter.close()
            db.close()
            say("child recorded", in_child())
            os._exit(0)
        say(wait_child(pid))
        go.set()
        for t in threads:
            t.join(30)
        rows.append(("main", db.find_document("items", 1)["by"]))
        say(sorted(rows))
        say(count("SELECT count(*) FROM items"))
        os._exit(0)
        """,
    )
    assert out == (
        0,
        "child recorded []\n"
        "child exited 0\n"
        "[('main', 'main'), ('t0', 't0'), ('t1', 't1')]\n"
        "[(5,)]\n",
        "",
    )


def test_a_multiprocessing_child_writes_through_the_module_database(tmp_path):
    out = _run(
        tmp_path,
        """
        import multiprocessing

        db = SQLerDB.on_disk(PATH)
        db.insert_document("items", {"by": "parent"})


        def write_one():
            db.insert_document("items", {"by": "child"})


        slow_worker(db.adapter)
        child = multiprocessing.get_context("fork").Process(target=write_one)
        child.start()
        child.join(10)
        if child.exitcode is None:
            child.kill()
            child.join()
            say("child stuck")
        else:
            say("child exit code", child.exitcode)
        say(count("SELECT json_extract(data, '$.by') FROM items ORDER BY _id"))
        os._exit(0)
        """,
    )
    assert out == (0, "child exit code 0\n[('parent',), ('child',)]\n", "")


def test_a_child_forked_while_an_async_statement_runs_exits(tmp_path):
    out = _run(
        tmp_path,
        """
        import asyncio
        from sqler import AsyncSQLerDB


        async def main():
            db = AsyncSQLerDB.on_disk(PATH)
            await db.connect()
            await db.insert_document("items", {"n": 1})
            slow = asyncio.ensure_future(db.adapter.execute(SLOW))
            await asyncio.sleep(0.5)
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            say(wait_child(pid))
            os._exit(0)


        asyncio.run(main())
        """,
    )
    assert out == (0, "child exited 0\n", "")


def test_a_child_forked_inside_a_transaction_block_is_in_no_transaction(tmp_path):
    out = _run(
        tmp_path,
        """
        db = SQLerDB.on_disk(PATH)
        with db:
            db.insert_document("items", {"n": 1})
            pid = os.fork()
            if pid == 0:
                say("child levels", db._open.levels, "depth", db.adapter._txn_depth)
                os._exit(0)
            say(wait_child(pid))
        say(count("SELECT count(*) FROM items"))
        os._exit(0)
        """,
    )
    assert out == (0, "child levels [] depth 0\nchild exited 0\n[(1,)]\n", "")


def _held_by_a_thread(lock: str) -> str:
    """A scenario body: a thread of the parent takes ``lock`` and holds it across the fork."""
    return f"""
        taken = threading.Event()
        release = threading.Event()

        def holder():
            with {lock}:
                taken.set()
                release.wait(30)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        taken.wait(30)
        """


def test_a_child_forked_while_a_thread_holds_the_ddl_lock_changes_a_table(tmp_path):
    out = _run(
        tmp_path,
        """
        db = SQLerDB.on_disk(PATH)
        db.insert_document("items", {"n": 1})
        """
        + _held_by_a_thread("db._ddl_lock")
        + """
        pid = os.fork()
        if pid == 0:
            db.ensure_table("fresh", versioned=True)
            say("child ensure_table returned")
            os._exit(0)
        say(wait_child(pid))
        release.set()
        thread.join(30)
        say(count("SELECT name FROM pragma_table_info('fresh') ORDER BY cid"))
        os._exit(0)
        """,
    )
    assert out == (
        0,
        "child ensure_table returned\nchild exited 0\n[('_id',), ('data',), ('_version',)]\n",
        "",
    )


def test_a_child_forked_while_a_thread_holds_the_cache_lock_makes_a_cache(tmp_path):
    out = _run(
        tmp_path,
        """
        from sqler import cache
        from sqler.cache import QueryCache
        """
        + _held_by_a_thread("cache._held_lock")
        + """
        pid = os.fork()
        if pid == 0:
            QueryCache(max_size=3)
            say("child made a cache")
            os._exit(0)
        say(wait_child(pid))
        release.set()
        thread.join(30)
        os._exit(0)
        """,
    )
    assert out == (0, "child made a cache\nchild exited 0\n", "")


def test_a_child_forked_while_a_transaction_wrote_a_cached_table_holds_no_table(tmp_path):
    out = _run(
        tmp_path,
        """
        from sqler import cache
        from sqler.cache import CacheAwareModel
        from sqler.models import SQLerModel


        class T(CacheAwareModel, SQLerModel):
            __tablename__ = "t"
            name: str = ""


        db = SQLerDB.on_disk(PATH)
        with db:
            T(name="x").save(db=db)
            say("parent held", cache._held)
            pid = os.fork()
            if pid == 0:
                say("child held", cache._held)
                os._exit(0)
            say(wait_child(pid))
        say("parent held", cache._held)
        os._exit(0)
        """,
    )
    assert out == (
        0,
        "parent held {'t': 1}\nchild held {}\nchild exited 0\nparent held {}\n",
        "",
    )
