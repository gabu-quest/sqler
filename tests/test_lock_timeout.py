"""How long a statement of the sync adapter waits for a lock: ``timeout_ms``.

One number, however the caller says it (the ``timeout_ms`` argument of the constructor or of
a factory, or a ``busy_timeout`` pragma in ``pragmas``): after ``connect()``,
``adapter.timeout_ms``, SQLite's ``busy_timeout`` on every connection the adapter opens and
the measured wait are the same.
"""

import sqlite3
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Optional

import pytest

from sqler import SQLerDB
from sqler.adapter import SQLiteAdapter
from sqler.exceptions import NotConnectedError


def _locked_by_another_connection(path: str) -> sqlite3.Connection:
    """A second connection on ``path`` that holds the write lock until it is closed."""
    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO t VALUES ('held')")
    return other


def _seconds_until_locked(adapter: SQLiteAdapter) -> float:
    """Insert while another connection holds the write lock; the seconds until it raises."""
    start = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="^database is locked$"):
        adapter.execute("INSERT INTO t VALUES ('mine')")
    return time.monotonic() - start


def _file_adapter(path: str, **kwargs) -> SQLiteAdapter:
    adapter = SQLiteAdapter(path, **kwargs)
    adapter.connect()
    adapter.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    adapter.auto_commit()
    return adapter


def _busy_timeouts(adapter: SQLiteAdapter) -> list[int]:
    """``busy_timeout`` on this thread's connection and on one a new thread opens."""
    seen = [adapter.pragma_report()["busy_timeout"]]
    thread = threading.Thread(target=lambda: seen.append(adapter.pragma_report()["busy_timeout"]))
    thread.start()
    thread.join(10)
    return seen


# ---- the wait on a file database ----


def test_timeout_ms_is_the_wait_for_another_connections_lock(tmp_path, clock_tick):
    path = str(tmp_path / "wait.db")
    adapter = _file_adapter(path, timeout_ms=200)
    other = _locked_by_another_connection(path)
    try:
        elapsed = _seconds_until_locked(adapter)
    finally:
        other.close()
        adapter.close()
    assert 0.2 - clock_tick <= elapsed < 2.5


def test_timeout_ms_zero_raises_at_once(tmp_path):
    path = str(tmp_path / "zero.db")
    adapter = _file_adapter(path, timeout_ms=0)
    other = _locked_by_another_connection(path)
    try:
        elapsed = _seconds_until_locked(adapter)
    finally:
        other.close()
        adapter.close()
    assert elapsed < 2.5  # the wait this replaces was 5 s


def test_a_timeout_python_would_truncate_is_the_exact_number(tmp_path):
    """``sqlite3.connect(timeout=1.001)`` sets 1000 ms; the adapter's number is kept as given."""
    adapter = _file_adapter(str(tmp_path / "exact.db"), timeout_ms=1001)
    try:
        assert (adapter.timeout_ms, _busy_timeouts(adapter)) == (1001, [1001, 1001])
    finally:
        adapter.close()


# ---- every way in passes the number on ----

FACTORIES = {
    "constructor": lambda path, **kw: _connected(SQLiteAdapter(path, **kw)),
    "adapter-on-disk": lambda path, **kw: _connected(SQLiteAdapter.on_disk(path, **kw)),
    "adapter-in-memory-shared": lambda path, **kw: _connected(SQLiteAdapter.in_memory(True, **kw)),
    "adapter-in-memory-plain": lambda path, **kw: _connected(SQLiteAdapter.in_memory(False, **kw)),
    "db-on-disk": lambda path, **kw: SQLerDB.on_disk(path, **kw).adapter,
    "db-in-memory-shared": lambda path, **kw: SQLerDB.in_memory(True, **kw).adapter,
    "db-in-memory-plain": lambda path, **kw: SQLerDB.in_memory(False, **kw).adapter,
}


def _connected(adapter: SQLiteAdapter) -> SQLiteAdapter:
    adapter.connect()
    return adapter


@pytest.mark.parametrize("factory", list(FACTORIES))
def test_every_factory_passes_timeout_ms_to_every_connection(factory, tmp_path):
    adapter = FACTORIES[factory](str(tmp_path / "f.db"), timeout_ms=1234)
    try:
        assert (adapter.timeout_ms, _busy_timeouts(adapter)) == (1234, [1234, 1234])
    finally:
        adapter.close()


@pytest.mark.parametrize("factory", list(FACTORIES))
def test_every_factory_waits_five_seconds_by_default(factory, tmp_path):
    adapter = FACTORIES[factory](str(tmp_path / "f.db"))
    try:
        assert (adapter.timeout_ms, _busy_timeouts(adapter)) == (5000, [5000, 5000])
    finally:
        adapter.close()


def test_the_adapter_made_with_a_path_only_waits_five_seconds(tmp_path):
    """What a caller that passes nothing else (``SQLiteAdapter(path=...)``) has always had."""
    adapter = SQLiteAdapter(path=str(tmp_path / "plain.db"))
    adapter.connect()
    try:
        assert (adapter.timeout_ms, _busy_timeouts(adapter)) == (5000, [5000, 5000])
    finally:
        adapter.close()


@pytest.mark.parametrize("value", [True, False, 1.5, -1, "100", 2**31], ids=repr)
def test_a_timeout_that_is_not_a_whole_number_of_milliseconds_raises_at_construction(value):
    with pytest.raises(ValueError, match="^timeout_ms must be a whole number of milliseconds") as raised:
        SQLiteAdapter(":memory:", timeout_ms=value)
    assert str(raised.value).endswith(f"not {value!r}")


@pytest.mark.parametrize("factory", ["on_disk", "in_memory"])
@pytest.mark.parametrize("owner", [SQLiteAdapter, SQLerDB], ids=["adapter", "db"])
def test_a_factory_given_a_negative_timeout_raises_before_it_opens_anything(owner, factory, tmp_path):
    args = (str(tmp_path / "never.db"),) if factory == "on_disk" else ()
    with pytest.raises(ValueError, match="not -1$"):
        getattr(owner, factory)(*args, timeout_ms=-1)
    assert list(tmp_path.iterdir()) == []


# ---- a busy_timeout pragma says the same number ----

PRAGMA_FORMS = ["PRAGMA busy_timeout=250;", "pragma BUSY_TIMEOUT = 250"]


@pytest.mark.parametrize("pragma", PRAGMA_FORMS)
def test_a_busy_timeout_pragma_is_the_number_when_timeout_ms_is_not_given(pragma, tmp_path, clock_tick):
    path = str(tmp_path / "pragma.db")
    adapter = _file_adapter(path, pragmas=[pragma])
    other = _locked_by_another_connection(path)
    try:
        elapsed = _seconds_until_locked(adapter)
        assert (adapter.timeout_ms, _busy_timeouts(adapter)) == (250, [250, 250])
    finally:
        other.close()
        adapter.close()
    assert 0.25 - clock_tick <= elapsed < 2.5


def test_a_busy_timeout_pragma_is_the_wait_for_another_thread_on_the_memory_connection(clock_tick):
    adapter = SQLiteAdapter(":memory:", pragmas=["PRAGMA busy_timeout = 250"])
    adapter.connect()
    held, done = threading.Event(), threading.Event()

    def holder() -> None:
        adapter.begin_transaction()
        held.set()
        done.wait(10)
        adapter.end_transaction(commit=False)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(10)
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="^database is locked: another thread's transaction"):
            adapter.execute("SELECT 1")
        elapsed = time.monotonic() - start
    finally:
        done.set()
        thread.join(10)
    assert (adapter.timeout_ms, adapter.pragma_report()["busy_timeout"]) == (250, 250)
    assert 0.25 - clock_tick <= elapsed < 2.5
    adapter.close()


def test_timeout_ms_and_a_pragma_that_agree_are_one_number(tmp_path):
    adapter = _file_adapter(str(tmp_path / "agree.db"), timeout_ms=250, pragmas=["PRAGMA busy_timeout = 250"])
    try:
        assert (adapter.timeout_ms, _busy_timeouts(adapter)) == (250, [250, 250])
    finally:
        adapter.close()


@pytest.mark.parametrize("path", ["file", ":memory:"])
def test_timeout_ms_and_a_pragma_that_disagree_raise_at_connect_naming_both(path, tmp_path):
    adapter = SQLiteAdapter(
        str(tmp_path / "disagree.db") if path == "file" else path,
        pragmas=["PRAGMA busy_timeout = 250"],
        timeout_ms=300,
    )
    with pytest.raises(ValueError) as raised:
        adapter.connect()
    assert str(raised.value) == "timeout_ms=300 and the busy_timeout pragma (250) disagree: give one of them"
    with pytest.raises(NotConnectedError):  # the connect that failed left nothing open
        adapter.execute("SELECT 1")
    adapter.close()



# ---- a shared-cache in-memory database: the same wait ----
#
# SQLite calls no busy handler for a shared-cache table lock (SQLITE_LOCKED_SHAREDCACHE): the
# adapter waits for it itself, up to ``timeout_ms``. A holder keeps its lock until its
# ``release()``; ``pauses.first`` is set when the waiter starts to wait.


@pytest.fixture
def pauses(monkeypatch):
    """The pauses of the adapter's lock wait: ``count``, and ``first``, set at the first."""
    real = time.sleep
    seen = SimpleNamespace(count=0, first=threading.Event())

    def pause(seconds: float) -> None:
        seen.count += 1
        seen.first.set()
        real(seconds)

    monkeypatch.setattr(time, "sleep", pause)
    return seen


def _shared(timeout_ms: int = 5000, name: Optional[str] = None, seed: bool = True) -> SQLiteAdapter:
    """A shared-cache in-memory adapter; ``seed``: ``t`` made holding 'a' and 'b', ``u`` holding 'x' and 'y'."""
    adapter = SQLiteAdapter.in_memory(shared=True, name=name, timeout_ms=timeout_ms)
    adapter.connect()
    if seed:
        adapter.executescript(
            "CREATE TABLE t (v TEXT); CREATE TABLE u (v TEXT);"
            "INSERT INTO t VALUES ('a'), ('b'); INSERT INTO u VALUES ('x'), ('y');"
        )
    return adapter


class _Holder:
    """A thread holding a lock on ``adapter``'s database until ``release()``.

    ``write``: a transaction that has written 'held' into ``t`` (committed at the release).
    ``read``: a SELECT over ``table`` with one of its rows read, its statement open.
    ``create``: a transaction that has made a table ``made`` (committed at the release); the
    schema is locked, which the first statement of another thread's new connection meets.
    """

    def __init__(self, adapter: SQLiteAdapter, kind: str, table: str = "t"):
        self.held, self.done = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, args=(adapter, kind, table))
        self.thread.start()
        assert self.held.wait(10)

    def _run(self, adapter: SQLiteAdapter, kind: str, table: str) -> None:
        if kind == "write":
            adapter.begin_transaction()
            adapter.execute("INSERT INTO t VALUES ('held')")
            self.held.set()
            self.done.wait(30)
            adapter.end_transaction(commit=True)
        elif kind == "create":
            adapter.begin_transaction()
            adapter.execute("CREATE TABLE made (v TEXT)")
            self.held.set()
            self.done.wait(30)
            adapter.end_transaction(commit=True)
        else:
            cursor = adapter.execute(f"SELECT v FROM {table}")
            cursor.fetchone()
            self.held.set()
            self.done.wait(30)
            cursor.close()

    def release(self) -> None:
        self.done.set()
        self.thread.join(10)
        assert not self.thread.is_alive()


class _Waiter:
    """``work()`` in a thread of its own: ``result`` or ``error``, and ``finished``."""

    def __init__(self, work):
        self.result: object = None
        self.error: Optional[BaseException] = None
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(work,))
        self.thread.start()

    def _run(self, work) -> None:
        try:
            self.result = work()
        except BaseException as exc:
            self.error = exc
        finally:
            self.finished.set()

    def join(self) -> None:
        self.thread.join(30)
        assert not self.thread.is_alive()


def _values(adapter: SQLiteAdapter, table: str = "t") -> list[str]:
    return [row[0] for row in adapter.execute(f"SELECT v FROM {table} ORDER BY rowid").fetchall()]


def _insert_mine(adapter: SQLiteAdapter) -> None:
    adapter.execute("INSERT INTO t VALUES ('mine')")
    adapter.auto_commit()


def _select(adapter: SQLiteAdapter) -> list[str]:
    return [row[0] for row in adapter.execute("SELECT v FROM t ORDER BY rowid").fetchall()]


def _many(adapter: SQLiteAdapter) -> None:
    adapter.executemany("INSERT INTO t VALUES (?)", [["m1"], ["m2"], ["m3"]])


def _many_inside(adapter: SQLiteAdapter) -> None:
    adapter.begin_transaction()
    adapter.executemany("INSERT INTO t VALUES (?)", [["m1"], ["m2"], ["m3"]])
    adapter.end_transaction(commit=True)


def _begin(adapter: SQLiteAdapter) -> None:
    adapter.begin_transaction()
    adapter.execute("INSERT INTO t VALUES ('mine')")
    adapter.end_transaction(commit=True)


def _inside(adapter: SQLiteAdapter) -> None:
    adapter.begin_transaction()
    adapter.execute("INSERT INTO u VALUES ('mine')")
    adapter.execute("INSERT INTO t VALUES ('mine')")  # meets the reader's lock on t
    adapter.end_transaction(commit=True)


# (the lock held, what the waiter does, what it returns, t at the end, u at the end)
WAITS = {
    "insert": ("write", _insert_mine, None, ["a", "b", "held", "mine"], ["x", "y"]),
    "select": ("write", _select, ["a", "b", "held"], ["a", "b", "held"], ["x", "y"]),
    "executemany": ("read", _many, None, ["a", "b", "m1", "m2", "m3"], ["x", "y"]),
    "executemany-inside-a-transaction": ("read", _many_inside, None, ["a", "b", "m1", "m2", "m3"], ["x", "y"]),
    "begin_transaction": ("write", _begin, None, ["a", "b", "held", "mine"], ["x", "y"]),
    "inside-a-transaction": ("read", _inside, None, ["a", "b", "mine"], ["x", "y", "mine"]),
    "a-threads-first-statement": ("create", _insert_mine, None, ["a", "b", "mine"], ["x", "y"]),
}


@pytest.mark.parametrize("case", list(WAITS))
def test_a_statement_that_meets_another_connections_table_lock_waits_for_it_and_runs_once(case, pauses):
    kind, work, returned, t_rows, u_rows = WAITS[case]
    adapter = _shared()
    try:
        holder = _Holder(adapter, kind)
        waiter = _Waiter(lambda: work(adapter))
        try:
            assert pauses.first.wait(10)  # it waits ...
            assert not waiter.finished.is_set()  # ... and has not finished while the lock is held
        finally:
            holder.release()
        waiter.join()
        assert (waiter.error, waiter.result) == (None, returned)
        assert (_values(adapter, "t"), _values(adapter, "u")) == (t_rows, u_rows)
    finally:
        adapter.close()


@pytest.mark.parametrize("kind", ["write", "read"])
def test_a_lock_that_is_not_released_raises_sqlites_error_once_timeout_ms_has_passed(kind, clock_tick):
    adapter = _shared(timeout_ms=200)
    try:
        holder = _Holder(adapter, kind)
        try:
            start = time.monotonic()
            with pytest.raises(sqlite3.OperationalError) as raised:
                adapter.execute("INSERT INTO t VALUES ('mine')")
            elapsed = time.monotonic() - start
            in_transaction = adapter._conn().in_transaction  # the failed write left nothing open
        finally:
            holder.release()
        assert (str(raised.value).split(":")[0], raised.value.sqlite_errorcode) == (
            "database table is locked",
            sqlite3.SQLITE_LOCKED_SHAREDCACHE,
        )
        assert elapsed >= 0.2 - clock_tick
        assert in_transaction is False
        assert _values(adapter) == (["a", "b", "held"] if kind == "write" else ["a", "b"])
    finally:
        adapter.close()


def test_timeout_ms_zero_raises_the_shared_cache_lock_at_once(pauses):
    adapter = _shared(timeout_ms=0)
    try:
        holder = _Holder(adapter, "write")
        try:
            with pytest.raises(sqlite3.OperationalError) as raised:
                adapter.execute("INSERT INTO t VALUES ('mine')")
        finally:
            holder.release()
        assert (raised.value.sqlite_errorcode, pauses.count) == (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 0)
        assert _values(adapter) == ["a", "b", "held"]
    finally:
        adapter.close()


def test_a_lock_of_the_same_connection_is_raised_at_once(pauses):
    """SQLite's plain SQLITE_LOCKED (a table the connection itself is reading) is no other
    connection's lock: no wait ends it."""
    adapter = _shared()
    try:
        reading = adapter._conn().execute("SELECT v FROM t")  # a statement of the thread's own connection
        assert reading.fetchone()[0] == "a"
        with pytest.raises(sqlite3.OperationalError) as raised:
            adapter.execute("DROP TABLE t")
        reading.close()
        assert (raised.value.sqlite_errorcode, pauses.count) == (sqlite3.SQLITE_LOCKED, 0)
        assert _values(adapter) == ["a", "b"]
    finally:
        adapter.close()


def test_executemany_with_rows_from_an_iterator_raises_at_once(pauses):
    """An iterator has given up the rows it was asked for: they cannot be sent a second time."""
    adapter = _shared()
    try:
        holder = _Holder(adapter, "read")
        try:
            with pytest.raises(sqlite3.OperationalError) as raised:
                adapter.executemany("INSERT INTO t VALUES (?)", iter([["m1"], ["m2"]]))
        finally:
            holder.release()
        assert (raised.value.sqlite_errorcode, pauses.count) == (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 0)
        assert _values(adapter) == ["a", "b"]
    finally:
        adapter.close()


def test_executescript_that_meets_the_lock_raises_at_once_with_its_earlier_statements_run(pauses):
    """The statements before the one that met the lock have run: the script is not sent again."""
    adapter = _shared()
    try:
        holder = _Holder(adapter, "read")
        try:
            with pytest.raises(sqlite3.OperationalError) as raised:
                adapter.executescript("INSERT INTO u VALUES ('s1'); INSERT INTO t VALUES ('s2');")
        finally:
            holder.release()
        assert (raised.value.sqlite_errorcode, pauses.count) == (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 0)
        assert (_values(adapter, "t"), _values(adapter, "u")) == (["a", "b"], ["x", "y", "s1"])
    finally:
        adapter.close()


def test_two_threads_that_each_hold_what_the_other_wants_both_end_after_timeout_ms(clock_tick):
    """A holds the write transaction (it wrote t) and wants u; B reads u through a second adapter
    on the same database and wants t. Neither can go on: each raises once its wait has passed."""
    name = f"lock-cycle-{uuid.uuid4().hex}"
    first, second = _shared(timeout_ms=300, name=name), _shared(timeout_ms=300, name=name, seed=False)
    wrote, reading = threading.Event(), threading.Event()
    a_done, b_done = threading.Event(), threading.Event()  # each keeps its lock until both have ended

    def timed(work) -> tuple[Optional[int], float]:
        start = time.monotonic()
        try:
            work()
        except sqlite3.OperationalError as exc:
            return exc.sqlite_errorcode, time.monotonic() - start
        return None, time.monotonic() - start

    def a() -> tuple[Optional[int], float]:
        first.begin_transaction()
        first.execute("INSERT INTO t VALUES ('a-wrote')")
        wrote.set()
        assert reading.wait(10)
        try:
            return timed(lambda: first.execute("INSERT INTO u VALUES ('a-wants')"))
        finally:
            a_done.set()
            b_done.wait(30)
            first.end_transaction(commit=False)

    def b() -> tuple[Optional[int], float]:
        assert wrote.wait(10)
        cursor = second.execute("SELECT v FROM u")
        cursor.fetchone()
        reading.set()
        try:
            return timed(lambda: first.execute("INSERT INTO t VALUES ('b-wants')"))
        finally:
            b_done.set()
            a_done.wait(30)
            cursor.close()

    try:
        threads = [_Waiter(a), _Waiter(b)]
        for thread in threads:
            thread.join()
        assert [(w.error, w.result[0]) for w in threads] == [(None, sqlite3.SQLITE_LOCKED_SHAREDCACHE)] * 2
        assert [w.result[1] >= 0.3 - clock_tick for w in threads] == [True, True]
        assert (_values(first, "t"), _values(first, "u")) == (["a", "b"], ["x", "y"])
    finally:
        second.close()
        first.close()


def test_a_waiter_whose_adapter_is_closed_while_it_waits_ends_with_an_error(pauses):
    name = f"lock-close-{uuid.uuid4().hex}"
    holding, waiting = _shared(name=name), _shared(timeout_ms=60_000, name=name, seed=False)
    try:
        holder = _Holder(holding, "write")
        try:
            waiter = _Waiter(lambda: waiting.execute("INSERT INTO t VALUES ('mine')"))
            assert pauses.first.wait(10)
            waiting.close()
            waiter.join()  # it does not wait on for the 60 s it was given
        finally:
            holder.release()
        assert type(waiter.error) is sqlite3.ProgrammingError  # Python's own, for a closed connection
        assert _values(holding) == ["a", "b", "held"]
    finally:
        holding.close()


# ---- a write outside a transaction takes the write lock first, where SQLite waits ----
#
# Python's implicit transaction before a write was a plain BEGIN, which takes no lock. When the
# write itself read first (a statement prepared again after the connection's own schema reset
# connects the FTS5 table of a trigger, or of the index kept by hand, and reads its config), the
# read transaction had to become a write one, and SQLite answers that with "database is locked"
# at once, without the wait. The adapter's implicit transaction is BEGIN IMMEDIATE.


class _RolledBack(Exception):
    pass


def _search_classes(path: str, content_sync: bool):
    from sqler import FTSIndex, SearchableMixin, SQLerModel

    tag = uuid.uuid4().hex[:8]

    class Note(SearchableMixin, SQLerModel):
        __tablename__ = f"notes_{tag}"
        body: str = ""

    class Fresh(SQLerModel):
        __tablename__ = f"fresh_{tag}"
        n: int = 0

    db = SQLerDB.on_disk(path)  # waits 5 s for a lock
    Note._fts_index = FTSIndex(Note, ["body"], content_sync=content_sync, db=db)
    Note._fts_index.create(db)
    return db, Note, Fresh


def _reset_own_schema(db: SQLerDB, Fresh) -> None:
    """The connection's own rolled-back CREATE TABLE: the first save of ``Fresh`` makes its
    table, and the rollback takes it away again."""
    try:
        with db.transaction():
            Fresh(n=1).save(db=db)
            raise _RolledBack()
    except _RolledBack:
        pass


def _held_for(path: str, seconds: float) -> tuple[threading.Event, threading.Thread]:
    """Another connection takes the write lock now; once the returned event is set, it holds it
    ``seconds`` more and commits."""
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    started = threading.Event()

    def release():
        started.wait(10)
        time.sleep(seconds)
        holder.execute("COMMIT")
        holder.close()

    thread = threading.Thread(target=release)
    thread.start()
    return started, thread


@pytest.mark.parametrize("between", ["nothing", "a read"])
@pytest.mark.parametrize("content_sync", [True, False], ids=["content_sync", "kept-by-hand"])
def test_a_search_save_after_the_connections_own_schema_reset_waits_for_the_lock(
    content_sync, between, tmp_path, clock_tick
):
    path = str(tmp_path / "search-wait.db")
    db, Note, Fresh = _search_classes(path, content_sync)
    try:
        Note(body="first").save(db=db)  # its statements are prepared and cached
        _reset_own_schema(db, Fresh)
        if between == "a read":
            assert [n.body for n in Note.search("first", db=db)] == ["first"]
        started, releaser = _held_for(path, 0.3)
        start = time.monotonic()
        started.set()
        Note(body="second").save(db=db)
        elapsed = time.monotonic() - start
        releaser.join(10)
        found = sorted(n.body for n in Note.search("first OR second", db=db))
    finally:
        db.close()
    assert (0.3 - clock_tick <= elapsed < 5, found) == (True, ["first", "second"])


def test_eight_threads_saving_search_rows_beside_first_saves_of_a_new_class_never_fail(tmp_path):
    path = str(tmp_path / "search-threads.db")
    db, Note, Fresh = _search_classes(path, True)
    from sqler import FTSIndex, SearchableMixin, SQLerModel

    class Kept(SearchableMixin, SQLerModel):
        __tablename__ = f"kept_{uuid.uuid4().hex[:8]}"
        body: str = ""

    Kept._fts_index = FTSIndex(Kept, ["body"], content_sync=False, db=db)
    Kept._fts_index.create(db)
    stop = time.monotonic() + 1.0
    errors: list[str] = []
    saved = [0] * 8

    def worker(i: int, cls) -> None:
        while time.monotonic() < stop:
            try:
                _reset_own_schema(db, Fresh)
                cls(body="hello").save(db=db)
                saved[i] += 1
            except sqlite3.OperationalError as exc:
                errors.append(f"{cls.__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i, (Note, Kept)[i % 2])) for i in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        counts = [Note.search_count("hello", db=db), Kept.search_count("hello", db=db)]
    finally:
        db.close()
    assert errors == []
    assert counts == [sum(saved[0::2]), sum(saved[1::2])]
