"""How long a statement of the sync adapter waits for a lock: ``timeout_ms``.

One number, however the caller says it (the ``timeout_ms`` argument of the constructor or of
a factory, or a ``busy_timeout`` pragma in ``pragmas``): after ``connect()``,
``adapter.timeout_ms``, SQLite's ``busy_timeout`` on every connection the adapter opens and
the measured wait are the same.
"""

import sqlite3
import threading
import time

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


# ---- a shared-cache in-memory database: no busy handler ----


@pytest.mark.parametrize("statement", ["INSERT INTO t VALUES ('mine')", "SELECT v FROM t"])
def test_a_shared_cache_memory_database_raises_at_once_on_another_connections_table_lock(statement):
    """SQLite calls no busy handler for a shared-cache table lock: ``timeout_ms`` does not reach it."""
    adapter = SQLiteAdapter.in_memory(shared=True, timeout_ms=2000)
    adapter.connect()
    adapter.execute("CREATE TABLE t (v TEXT)")
    adapter.auto_commit()
    held, done = threading.Event(), threading.Event()
    outcome: list[object] = []

    def holder() -> None:
        adapter.begin_transaction()
        adapter.execute("INSERT INTO t VALUES ('held')")
        held.set()
        done.wait(10)
        adapter.end_transaction(commit=False)

    def other() -> None:
        start = time.monotonic()
        try:
            adapter.execute(statement)
        except sqlite3.OperationalError as exc:
            outcome.append((str(exc).split(":")[0], time.monotonic() - start < 1.0))

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(10)
        reader = threading.Thread(target=other)
        reader.start()
        reader.join(10)
    finally:
        done.set()
        thread.join(10)
        adapter.close()
    assert outcome == [("database table is locked", True)]
