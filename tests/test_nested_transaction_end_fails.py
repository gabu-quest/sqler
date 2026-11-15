"""A nested transaction of the sync adapter whose end fails in SQLite.

The end of a nested transaction is ``RELEASE`` (commit) or ``ROLLBACK TO`` then ``RELEASE``
(rollback). When SQLite refuses one (a raw statement already ended the savepoint, say), the
transaction can no longer be trusted: no ``RELEASE`` follows a failed ``ROLLBACK TO``, the
nested end raises ``TransactionStateError`` with SQLite's error as ``__cause__``, every
later statement of the thread in that transaction raises it and sends nothing, and the
transaction ends rolled back with the same error. The async adapter says the same sentence
for the same failure.

When the rollback that ends the outermost transaction fails, it is tried once more at once.
If that fails too, a thread's own connection is closed (which rolls back) and the thread
opens a new one at its next statement; the one connection of a plain ``":memory:"``
database keeps the transaction, and every later statement tries the rollback again first.
No statement is sent into a transaction the adapter counts as ended.
"""

import asyncio
import ctypes
import functools
import logging
import signal
import sqlite3
import sys
import threading
import time
import uuid

import pytest
from exception_points import stop_once_and_interrupt
from sqler import AsyncSQLerDB, SQLerDB
from sqler.adapter import SQLiteAdapter
from sqler.exceptions import TransactionStateError
from sqler.logging import query_logger

ADAPTER = "sqler.adapter.synchronous"

KINDS = ["memory", "file"]
TRACED = ("BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT", "INSERT", "SELECT")
SENTENCE = (
    "a nested transaction could not be {}, so the transaction it is part of ends rolled back: "
    "nothing written in it is committed"
)
COMMITTED = SENTENCE.format("committed")
ROLLED_BACK = SENTENCE.format("rolled back")
NO_SP_1 = "no such savepoint: sp_1"


class Undo(Exception):
    """The block's own reason to roll its nested transaction back."""


class Traced:
    """A database with a table ``t (v)`` holding ``before``, and the statements sent on
    the test thread's connection from the start of the test, as SQLite saw them."""

    def __init__(self, kind: str, tmp_path):
        path = ":memory:" if kind == "memory" else str(tmp_path / "nested.db")
        self.db = SQLerDB(SQLiteAdapter(path))
        self.adapter = self.db.adapter
        self.adapter.execute("CREATE TABLE t (v TEXT)")
        self.insert("before")
        self.adapter.auto_commit()
        self.sent: list[str] = []
        self.adapter._conn().set_trace_callback(
            lambda sql: self.sent.append(sql.strip()) if sql.split()[0].upper() in TRACED else None
        )

    def insert(self, value: str) -> None:
        self.adapter.execute("INSERT INTO t VALUES (?)", [value])

    def steps(self) -> list[str]:
        """The transaction statements, each insert shown as ``INSERT <its value>``."""
        return [
            "INSERT " + sql.split("VALUES", 1)[1].strip(" ();'") if sql.startswith("INSERT") else sql
            for sql in self.sent
            if not sql.startswith("SELECT")
        ]

    def rows(self) -> list[str]:
        rows = sorted(r[0] for r in self.adapter.execute("SELECT v FROM t").fetchall())
        self.adapter.auto_commit()
        return rows


@pytest.fixture(params=KINDS)
def t(request, tmp_path):
    traced = Traced(request.param, tmp_path)
    yield traced
    traced.db.close()


def _released_behind_sqlers_back(t: Traced, savepoint: str = "sp_1") -> None:
    t.adapter.execute(f"RELEASE SAVEPOINT {savepoint}")


def _after_the_transaction(t: Traced) -> None:
    """The thread is in no transaction and its next statement is committed on its own."""
    assert (t.adapter._txn_depth, t.adapter.in_transaction) == (0, False)
    t.insert("after")
    t.adapter.auto_commit()


# ---- the nested end that fails ----


def test_a_nested_rollback_whose_rollback_to_fails_sends_no_release_and_rolls_back(t):
    with pytest.raises(TransactionStateError) as ended:
        with t.db.transaction():
            t.insert("outer")
            with pytest.raises(Undo) as nested:
                with t.db.transaction():
                    t.insert("nested, to be undone")
                    _released_behind_sqlers_back(t)
                    raise Undo()
            assert nested.value.__notes__ == [ROLLED_BACK]
    assert (str(ended.value), str(ended.value.__cause__)) == (ROLLED_BACK, NO_SP_1)
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "INSERT outer",
        "SAVEPOINT sp_1",
        "INSERT nested, to be undone",
        "RELEASE SAVEPOINT sp_1",
        "ROLLBACK TO SAVEPOINT sp_1",
        "ROLLBACK",
    ]
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]


@pytest.mark.parametrize("caught", [False, True], ids=["raised-on", "caught-in-the-block"])
def test_a_nested_commit_whose_release_fails_commits_nothing_and_tells_both(t, caught):
    with pytest.raises(TransactionStateError) as ended:
        with t.db.transaction():
            t.insert("outer")
            try:
                with t.db.transaction():
                    t.insert("nested")
                    _released_behind_sqlers_back(t)
            except TransactionStateError as nested:
                assert (str(nested), str(nested.__cause__)) == (COMMITTED, NO_SP_1)
                if not caught:
                    raise
    assert (str(ended.value), str(ended.value.__cause__)) == (COMMITTED, NO_SP_1)
    assert getattr(ended.value, "__notes__", []) == []
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "INSERT outer",
        "SAVEPOINT sp_1",
        "INSERT nested",
        "RELEASE SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
        "ROLLBACK",
    ]
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]


def test_an_outer_block_raising_after_the_failed_end_keeps_its_exception_with_a_note(t):
    class OuterFailed(Exception):
        pass

    with pytest.raises(OuterFailed) as raised:
        with t.db.transaction():
            t.insert("outer")
            with pytest.raises(TransactionStateError):
                with t.db.transaction():
                    _released_behind_sqlers_back(t)
            raise OuterFailed("its own")
    assert (str(raised.value), raised.value.__notes__) == ("its own", [COMMITTED])
    assert t.steps()[-1] == "ROLLBACK"
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]


def test_an_exception_leaving_a_transaction_block_and_the_db_block_has_the_note_once(t):
    with pytest.raises(ValueError) as raised:
        with t.db:
            with t.db.transaction():
                with pytest.raises(Undo):
                    with t.db.transaction():
                        t.insert("nested, to be undone")
                        _released_behind_sqlers_back(t, "sp_2")
                        raise Undo()
                raise ValueError("its own")
    assert (str(raised.value), raised.value.__notes__) == ("its own", [ROLLED_BACK])
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]


# ---- an end error that is not the rolled-back sentence, while an exception leaves ----


def test_an_end_error_while_an_exception_leaves_is_named_on_that_exception_once():
    from sqler.db.sqler_db import _ended_while_leaving

    leaving = ValueError("its own")
    for _ in range(2):  # the same end error met by two blocks it leaves: one note
        _ended_while_leaving(sqlite3.OperationalError("disk I/O error"), leaving)
    assert leaving.__notes__ == ["ending the transaction failed: OperationalError: disk I/O error"]


def test_an_end_error_with_no_exception_leaving_is_raised():
    from sqler.db.sqler_db import _ended_while_leaving

    ended = sqlite3.OperationalError("disk I/O error")
    with pytest.raises(sqlite3.OperationalError) as raised:
        _ended_while_leaving(ended, None)
    assert (raised.value is ended, getattr(ended, "__notes__", [])) == (True, [])


# ---- every later statement in the transaction raises and sends nothing ----


def _send(t: Traced, kind: str) -> None:
    if kind == "execute":
        t.insert("refused")
    elif kind == "executemany":
        t.adapter.executemany("INSERT INTO t VALUES (?)", [["refused"]])
    elif kind == "executescript":
        t.adapter.executescript("INSERT INTO t VALUES ('refused');")
    elif kind == "commit":
        t.adapter.commit()
    elif kind == "nested":
        with t.db.transaction():
            t.insert("refused")
    elif kind == "with-adapter":
        with t.adapter:
            t.insert("refused")
    else:
        t.adapter.execute("SELECT v FROM t").fetchall()


STATEMENTS = ["execute", "executemany", "executescript", "commit", "nested", "with-adapter", "select"]


@pytest.mark.parametrize("kind", STATEMENTS)
def test_a_statement_after_a_failed_nested_end_raises_and_sends_nothing(t, kind):
    refused = []
    with pytest.raises(TransactionStateError) as ended:
        with t.db.transaction():
            t.insert("outer")
            with pytest.raises(TransactionStateError):
                with t.db.transaction():
                    _released_behind_sqlers_back(t)
            sent = len(t.sent)
            with query_logger.capture() as logged:
                with pytest.raises(TransactionStateError) as raised:
                    _send(t, kind)
            refused.append((str(raised.value), str(raised.value.__cause__), t.sent[sent:], len(logged)))
    assert refused == [(COMMITTED, NO_SP_1, [], 0)]
    assert (str(ended.value), str(ended.value.__cause__)) == (COMMITTED, NO_SP_1)
    assert t.steps()[-1] == "ROLLBACK"
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]


def test_the_adapter_used_directly_follows_the_same_rule(t):
    """Three levels with ``begin_transaction``/``end_transaction`` and no block: a raw
    ``RELEASE`` of ``sp_1`` ends both savepoints; the innermost end fails, the middle one
    raises and sends nothing, and the outermost sends ``ROLLBACK`` although it asks to commit."""
    ad = t.adapter
    ad.begin_transaction()
    t.insert("outer")
    ad.begin_transaction()
    ad.begin_transaction()
    t.insert("deep")
    _released_behind_sqlers_back(t)
    outcomes = []
    for _ in range(3):
        sent = len(t.sent)
        with pytest.raises(TransactionStateError) as raised:
            ad.end_transaction(commit=True)
        outcomes.append((str(raised.value), str(raised.value.__cause__), t.sent[sent:]))
    assert outcomes == [
        (COMMITTED, "no such savepoint: sp_2", ["RELEASE SAVEPOINT sp_2"]),
        (COMMITTED, "no such savepoint: sp_2", []),
        (COMMITTED, "no such savepoint: sp_2", ["ROLLBACK"]),
    ]
    ad.end_transaction(commit=True)  # in no transaction: nothing to end, nothing raised
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]


def test_three_levels_with_the_middle_end_failing(t):
    with pytest.raises(TransactionStateError) as ended:
        with t.db.transaction():
            t.insert("outer")
            with pytest.raises(TransactionStateError) as middle:
                with t.db.transaction():
                    t.insert("middle")
                    with t.db.transaction():
                        t.insert("inner")
                    _released_behind_sqlers_back(t)
    assert (str(middle.value), str(middle.value.__cause__)) == (COMMITTED, NO_SP_1)
    assert (str(ended.value), str(ended.value.__cause__)) == (COMMITTED, NO_SP_1)
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "INSERT outer",
        "SAVEPOINT sp_1",
        "INSERT middle",
        "SAVEPOINT sp_2",
        "INSERT inner",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
        "ROLLBACK",
    ]
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]


# ---- raw statements that end the whole transaction ----


@pytest.mark.parametrize("raw", ["COMMIT", "ROLLBACK"])
def test_a_raw_end_of_the_transaction_inside_a_nested_block_raises_once(t, raw):
    """The raw statement ends the transaction (a raw ``COMMIT`` keeps what was written
    until then; that cannot be undone); the nested ``RELEASE`` then fails, and the error
    leaves the outer block once, as it is, with nothing more sent."""
    with pytest.raises(TransactionStateError) as raised:
        with t.db.transaction():
            t.insert("outer")
            with t.db.transaction():
                t.insert("nested")
                t.adapter.execute(raw)
    assert str(raised.value) == COMMITTED
    assert str(raised.value.__cause__) == NO_SP_1
    assert getattr(raised.value, "__notes__", []) == []
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "INSERT outer",
        "SAVEPOINT sp_1",
        "INSERT nested",
        raw,
        "RELEASE SAVEPOINT sp_1",
    ]
    _after_the_transaction(t)
    assert t.rows() == (["after", "before", "nested", "outer"] if raw == "COMMIT" else ["after", "before"])


# ---- the one ":memory:" connection: the other threads go on after the end ----


def test_a_thread_waiting_for_the_memory_connection_goes_on_after_the_rolled_back_end(tmp_path):
    t = Traced("memory", tmp_path)
    seen: list[list[str]] = []
    raised: list[BaseException] = []

    def reader() -> None:
        try:
            seen.append(sorted(r[0] for r in t.adapter.execute("SELECT v FROM t").fetchall()))
            t.adapter.auto_commit()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            raised.append(exc)

    thread = threading.Thread(target=reader)
    with pytest.raises(TransactionStateError):
        with t.db.transaction():
            t.insert("outer")
            thread.start()
            deadline = time.monotonic() + 10
            while t.adapter._shared._waiting == 0:  # the reader waits for this transaction's end
                assert time.monotonic() < deadline, "the reader never came to wait"
                time.sleep(0.001)
            with pytest.raises(TransactionStateError):
                with t.db.transaction():
                    t.insert("nested")
                    _released_behind_sqlers_back(t)
    thread.join(10)
    assert (thread.is_alive(), raised, seen) == (False, [], [["before"]])
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]
    t.db.close()


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="needs a real signal to the main thread")
def test_an_interrupt_while_the_nested_end_waits_leaves_as_it_is_and_the_transaction_rolls_back(tmp_path):
    """Ctrl-C while the nested end waits for the connection: the interrupt leaves the
    nested block; the savepoint is still open in SQLite, so its end can only roll back."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("the interrupt goes to the main thread")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    t = Traced("memory", tmp_path)
    turn = t.adapter._shared._turn
    holding, release = threading.Event(), threading.Event()
    main = threading.main_thread().ident

    def interrupter() -> None:
        with turn:  # the nested end now waits to take the connection
            holding.set()
            deadline = time.monotonic() + 10
            while sys._current_frames()[main].f_code.co_name != "end_transaction":  # blocked on the turn's lock
                assert time.monotonic() < deadline
                time.sleep(0.001)
            signal.pthread_kill(main, signal.SIGINT)  # what Ctrl-C does
            release.wait(10)

    thread = threading.Thread(target=interrupter)
    with pytest.raises(TransactionStateError) as ended:
        with t.db.transaction():
            t.insert("outer")
            with pytest.raises(KeyboardInterrupt):
                with t.db.transaction():
                    t.insert("nested")
                    thread.start()
                    assert holding.wait(10)
            release.set()
            thread.join(10)
    assert (str(ended.value), type(ended.value.__cause__)) == (COMMITTED, KeyboardInterrupt)
    assert t.steps() == ["BEGIN IMMEDIATE", "INSERT outer", "SAVEPOINT sp_1", "INSERT nested", "ROLLBACK"]
    _after_the_transaction(t)
    assert t.rows() == ["after", "before"]
    t.db.close()


# ---- the same sentence as the async adapter, and no cost on the path that works ----


def test_sync_and_async_raise_the_same_error_for_the_same_failure(tmp_path):
    def sync() -> tuple[str, str]:
        db = SQLerDB.on_disk(str(tmp_path / "sync.db"))
        try:
            with pytest.raises(TransactionStateError) as raised:
                with db.transaction():
                    with db.transaction():
                        db.adapter.execute("RELEASE SAVEPOINT sp_1")
            return str(raised.value), str(raised.value.__cause__)
        finally:
            db.close()

    async def async_() -> tuple[str, str]:
        db = AsyncSQLerDB.on_disk(str(tmp_path / "async.db"))
        await db.connect()
        try:
            with pytest.raises(TransactionStateError) as raised:
                async with db.transaction():
                    async with db.transaction():
                        await db.adapter.execute("RELEASE SAVEPOINT sp_1")
            return str(raised.value), str(raised.value.__cause__)
        finally:
            await db.close()

    assert sync() == asyncio.run(async_()) == (COMMITTED, NO_SP_1)


def test_a_transaction_of_one_insert_sends_what_it_always_sent(t):
    with query_logger.capture() as logged:
        with t.db.transaction():
            t.insert("one")
    assert ([q.sql for q in logged], t.steps()) == (
        ["INSERT INTO t VALUES (?)"],
        ["BEGIN IMMEDIATE", "INSERT one", "COMMIT"],
    )
    assert t.rows() == ["before", "one"]


def test_a_transaction_that_rolls_back_sends_what_it_always_sent(t):
    with query_logger.capture() as logged:
        with pytest.raises(Undo):
            with t.db.transaction():
                t.insert("one")
                raise Undo()
    assert ([q.sql for q in logged], t.steps()) == (
        ["INSERT INTO t VALUES (?)"],
        ["BEGIN IMMEDIATE", "INSERT one", "ROLLBACK"],
    )
    assert t.rows() == ["before"]


# ---- the rollback that ends the outermost transaction fails ----
#
# A progress handler on the thread's connection stops every statement from the moment it is
# set, the rollback that ends the transaction too. A thread's own connection (a file, a named
# shared-cache memory database) is then closed, which rolls back, and the thread opens a new
# one at its next statement. The one connection of a plain ":memory:" database cannot be
# closed: it keeps the transaction, its thread stays the holder, and every later statement
# tries the rollback again first and raises until it succeeds.

PLACES = ["file", "named-memory", "memory"]
ENDS = ["with-db", "transaction", "nested", "adapter"]
INTERRUPTED = "interrupted"
NOT_ROLLED_BACK = (
    'the transaction could not be rolled back: the one connection of the ":memory:" database keeps it, '
    "and every statement tries the rollback again first"
)
REPLACED = "rollback of a reclaimed connection failed (OperationalError: interrupted); replacing it"
LOCKED = "database is locked: another thread's transaction holds the in-memory connection"


class Scene:
    """A database at ``place`` with a table ``t (v)`` holding ``before``, committed; ``conn``
    is the test thread's connection."""

    def __init__(self, place: str, tmp_path):
        self.place = place
        if place == "file":
            self.path = str(tmp_path / "failed_rollback.db")
        elif place == "named-memory":
            self.path = f"file:failed-rollback-{uuid.uuid4().hex}?mode=memory&cache=shared"
        else:
            self.path = ":memory:"
        self.db = SQLerDB(SQLiteAdapter(self.path, timeout_ms=50))
        self.adapter = self.db.adapter
        self.adapter.execute("CREATE TABLE t (v TEXT)")
        self.insert("before")
        self.adapter.auto_commit()
        self.conn = self.adapter._conn()
        self.stops = lambda: 1  # the progress handler that stops a statement

    def insert(self, value: str) -> None:
        self.adapter.execute("INSERT INTO t VALUES (?)", [value])

    def stop_every_statement(self) -> None:
        self.conn.set_progress_handler(self.stops, 1)

    def let_statements_run(self) -> None:
        self.conn.set_progress_handler(None, 1)

    def committed(self) -> list[str]:
        """``t`` as another connection sees it (on plain ``":memory:"``, there is no other)."""
        if self.place == "memory":
            rows = sorted(r[0] for r in self.adapter.execute("SELECT v FROM t").fetchall())
            self.adapter.auto_commit()
            return rows
        raw = sqlite3.connect(self.path, uri=True, timeout=0)
        try:
            return sorted(r[0] for r in raw.execute("SELECT v FROM t"))
        finally:
            raw.close()


@pytest.fixture(params=PLACES)
def scene(request, tmp_path):
    s = Scene(request.param, tmp_path)
    yield s
    s.db.close()


def _a_block_whose_rollback_fails(s: Scene, end: str) -> BaseException | None:
    """The block writes ``in the block``, every statement is stopped, and the block ends
    rolling back; returns what left it."""
    try:
        if end == "adapter":
            s.adapter.begin_transaction()
            s.insert("in the block")
            s.stop_every_statement()
            s.adapter.end_transaction(commit=False)
            return None
        with s.db if end == "with-db" else s.db.transaction():
            if end == "nested":
                with s.db.transaction():
                    s.insert("in the block")
                    s.stop_every_statement()
                    raise ValueError("the block's own error")
            s.insert("in the block")
            s.stop_every_statement()
            raise ValueError("the block's own error")
    except BaseException as exc:  # noqa: BLE001 - what left is the result
        return exc


def _left(exc: BaseException | None) -> tuple:
    if exc is None:
        return (None,)
    return type(exc).__name__, str(exc), getattr(exc, "__notes__", []), str(exc.__cause__ or "")


OWN_ERROR = "the block's own error"
LEFT = {
    ("own", "with-db"): ("ValueError", OWN_ERROR, [], ""),
    ("own", "transaction"): ("ValueError", OWN_ERROR, [], ""),
    ("own", "nested"): ("ValueError", OWN_ERROR, [ROLLED_BACK], ""),
    ("own", "adapter"): (None,),
    ("memory", "with-db"): ("ValueError", OWN_ERROR, [NOT_ROLLED_BACK], ""),
    ("memory", "transaction"): ("ValueError", OWN_ERROR, [NOT_ROLLED_BACK], ""),
    ("memory", "nested"): ("ValueError", OWN_ERROR, [ROLLED_BACK, NOT_ROLLED_BACK], ""),
    ("memory", "adapter"): ("TransactionStateError", NOT_ROLLED_BACK, [], INTERRUPTED),
}


@pytest.mark.parametrize("end", ENDS)
@pytest.mark.parametrize("place", ["file", "named-memory"])
def test_a_rollback_that_fails_twice_on_a_threads_own_connection_closes_it(tmp_path, caplog, place, end):
    s = Scene(place, tmp_path)
    try:
        caplog.set_level(logging.WARNING, logger="sqler.adapter.synchronous")
        left = _left(_a_block_whose_rollback_fails(s, end))
        seen = s.committed()
        assert (left, seen, [r.getMessage() for r in caplog.records]) == (LEFT["own", end], ["before"], [REPLACED])
        assert (s.adapter._txn_depth, s.adapter._local.conn) == (0, None)
        with pytest.raises(sqlite3.ProgrammingError):  # closed, never used again
            s.conn.execute("SELECT 1")
        s.insert("after the block")
        s.adapter.auto_commit()
        assert s.committed() == ["after the block", "before"]
    finally:
        s.db.close()


@pytest.mark.parametrize("end", ENDS)
def test_a_rollback_that_fails_twice_on_the_memory_connection_keeps_it_until_one_succeeds(tmp_path, caplog, end):
    s = Scene("memory", tmp_path)
    try:
        caplog.set_level(logging.WARNING, logger="sqler.adapter.synchronous")
        assert _left(_a_block_whose_rollback_fails(s, end)) == LEFT["memory", end]
        with query_logger.capture() as logged:
            with pytest.raises(TransactionStateError) as raised:
                s.insert("refused")
        assert (str(raised.value), str(raised.value.__cause__), len(logged)) == (NOT_ROLLED_BACK, INTERRUPTED, 0)
        assert (s.adapter._shared._holder, s.conn.in_transaction) == (s.adapter._local.me, True)
        s.let_statements_run()
        s.insert("after the block")
        s.adapter.auto_commit()
        assert (s.committed(), s.adapter._conn() is s.conn, caplog.records) == (["after the block", "before"], True, [])
    finally:
        s.db.close()


def test_a_rollback_interrupted_once_succeeds_at_the_second_try(scene, caplog):
    stops = []

    def once() -> int:
        stops.append(1)
        return 1 if len(stops) == 1 else 0

    caplog.set_level(logging.WARNING, logger="sqler.adapter.synchronous")
    with pytest.raises(ValueError) as raised:
        with scene.db.transaction():
            scene.insert("in the block")
            scene.conn.set_progress_handler(once, 1)
            raise ValueError(OWN_ERROR)
    scene.let_statements_run()
    assert getattr(raised.value, "__notes__", []) == []
    assert (scene.adapter._conn() is scene.conn, scene.conn.in_transaction, caplog.records) == (True, False, [])
    assert scene.committed() == ["before"]


REPLACED_AFTER_AN_INTERRUPT = "rollback of a reclaimed connection failed (KeyboardInterrupt: ); replacing it"


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="needs a real signal to the main thread")
@pytest.mark.parametrize("end", ["with-db", "transaction", "adapter"])
def test_an_interrupt_between_the_two_tries_of_a_rollback_leaves_no_transaction_to_join(scene, caplog, end):
    """The handler that stops the rollback also sends this process SIGINT, through libc and
    with no Python frame of its own, so Python raises ``KeyboardInterrupt`` in the adapter
    as the first try returns, before the second. (Not the nested end: its own statement
    would be the one stopped.)"""
    scene.stops = functools.partial(max, [int(signal.SIGINT)], key=getattr(ctypes.CDLL(None), "raise"))
    caplog.set_level(logging.WARNING, logger="sqler.adapter.synchronous")
    left = _a_block_whose_rollback_fails(scene, end)
    messages = [r.getMessage() for r in caplog.records]
    assert type(left) is KeyboardInterrupt
    if scene.place == "memory":
        assert (messages, scene.adapter._shared._holder, scene.conn.in_transaction) == ([], scene.adapter._local.me, True)
        scene.let_statements_run()
    else:
        assert (messages, scene.adapter._local.conn) == ([REPLACED_AFTER_AN_INTERRUPT], None)
    assert scene.committed() == ["before"]
    scene.insert("after")
    scene.adapter.auto_commit()
    assert scene.committed() == ["after", "before"]


@pytest.mark.parametrize("how", ["transaction", "adapter-commit"])
def test_a_commit_that_fails_with_the_rollback_after_it_failing_raises_the_commits_error(scene, caplog, how):
    caplog.set_level(logging.WARNING, logger="sqler.adapter.synchronous")
    with pytest.raises(sqlite3.OperationalError) as raised:
        if how == "transaction":
            with scene.db.transaction():
                scene.insert("in the block")
                scene.stop_every_statement()
        else:
            scene.insert("in the block")  # Python's sqlite3 opens a transaction for it
            scene.stop_every_statement()
            scene.adapter.commit()
    assert (str(raised.value), getattr(raised.value, "__notes__", [])) == (INTERRUPTED, [])
    _the_rollback_failed_twice(scene, caplog)


def test_a_statement_outside_a_transaction_that_fails_with_the_rollback_failing_too(scene, caplog):
    """An interrupted write makes SQLite roll the whole transaction back itself, so the
    statement fails by an error of its own (an integer overflow, which leaves the transaction
    open) and only the rollback after it is stopped: the trace callback arms the handler as
    the ``ROLLBACK`` starts."""
    caplog.set_level(logging.WARNING, logger="sqler.adapter.synchronous")
    scene.insert("in the block")  # Python's sqlite3 opens a transaction for it
    rollbacks: list[str] = []
    scene.conn.set_trace_callback(lambda sql: rollbacks.append(sql) if sql == "ROLLBACK" else None)
    scene.conn.set_progress_handler(lambda: 1 if rollbacks else 0, 1)
    with pytest.raises(sqlite3.OperationalError) as raised:
        scene.adapter.execute("INSERT INTO t VALUES (abs(-9223372036854775808))")
    assert (str(raised.value), len(rollbacks)) == ("integer overflow", 2)
    _the_rollback_failed_twice(scene, caplog)


def _the_rollback_failed_twice(s: Scene, caplog) -> None:
    """After a commit or a statement whose rollback failed twice: what the connection kind
    does with it, then the next statement finds nothing of the transaction."""
    messages = [r.getMessage() for r in caplog.records]
    if s.place == "memory":
        assert (messages, s.adapter._shared._holder, s.conn.in_transaction) == ([], s.adapter._local.me, True)
        with pytest.raises(TransactionStateError) as raised:
            s.insert("refused")
        assert (str(raised.value), str(raised.value.__cause__)) == (NOT_ROLLED_BACK, INTERRUPTED)
        s.let_statements_run()
    else:
        assert (messages, s.committed(), s.adapter._local.conn) == ([REPLACED], ["before"], None)
    s.insert("after")
    s.adapter.auto_commit()
    assert s.committed() == ["after", "before"]


@pytest.mark.parametrize("next_by", ["the-same-thread", "the-waiting-thread"])
def test_another_thread_never_writes_into_the_memory_transaction_that_could_not_be_rolled_back(tmp_path, next_by):
    s = Scene("memory", tmp_path)
    outcomes: list[str] = []

    def other(value: str) -> None:
        try:
            s.insert(value)
            s.adapter.auto_commit()
            outcomes.append("written")
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            outcomes.append(f"{type(exc).__name__}: {exc}")

    try:
        assert _left(_a_block_whose_rollback_fails(s, "with-db")) == LEFT["memory", "with-db"]
        waiting = threading.Thread(target=other, args=("by the other thread",))
        waiting.start()
        waiting.join(10)
        assert (waiting.is_alive(), outcomes) == (False, [f"OperationalError: {LOCKED}"])
        s.let_statements_run()
        if next_by == "the-same-thread":
            s.insert("after")
            s.adapter.auto_commit()
        else:
            again = threading.Thread(target=other, args=("after",))
            again.start()
            again.join(10)
            assert (again.is_alive(), outcomes[1:]) == (False, ["written"])
        assert s.committed() == ["after", "before"]
    finally:
        s.db.close()


def test_a_thread_that_ends_holding_the_memory_connection_whose_rollback_fails(tmp_path):
    s = Scene("memory", tmp_path)

    def holder() -> None:
        s.adapter.begin_transaction()
        s.insert("of the thread that ended")
        s.stop_every_statement()

    try:
        ended = threading.Thread(target=holder)
        ended.start()
        ended.join(10)
        assert ended.is_alive() is False
        with pytest.raises(sqlite3.OperationalError) as raised:
            s.insert("joins nothing")
        assert str(raised.value) == LOCKED
        s.let_statements_run()
        s.insert("after")
        s.adapter.auto_commit()
        assert s.committed() == ["after", "before"]
    finally:
        s.db.close()


def test_close_after_a_rollback_that_failed_raises_nothing(scene):
    _a_block_whose_rollback_fails(scene, "transaction")
    scene.db.close()
    if scene.place == "file":
        assert scene.committed() == ["before"]


def test_the_write_lock_of_a_file_is_free_right_after_the_block(tmp_path):
    s = Scene("file", tmp_path)
    try:
        _a_block_whose_rollback_fails(s, "with-db")
        raw = sqlite3.connect(s.path, timeout=0)
        try:
            raw.execute("INSERT INTO t VALUES ('by another connection')")
            raw.commit()
        finally:
            raw.close()
        assert s.committed() == ["before", "by another connection"]
    finally:
        s.db.close()


def test_a_half_read_cursor_of_the_closed_connection_holds_the_lock_and_commits_nothing(tmp_path):
    """SQLite keeps a closed connection, and its transaction, while one of its statements
    is not finished: writers are refused until the cursor goes, then the block's row is
    rolled back."""
    s = Scene("file", tmp_path)
    try:
        with pytest.raises(ValueError, match=OWN_ERROR):
            with s.db.transaction():
                for n in range(3):
                    s.insert(f"row {n}")
                cursor = s.adapter.execute("SELECT v FROM t")
                assert tuple(cursor.fetchone()) == ("before",)  # three rows not read
                s.stop_every_statement()
                raise ValueError(OWN_ERROR)
        assert s.adapter._local.conn is None
        with pytest.raises(sqlite3.OperationalError, match="^database is locked$"):
            s.insert("refused")
        assert s.committed() == ["before"]
        del cursor
        s.insert("after the cursor went")
        s.adapter.auto_commit()
        assert s.committed() == ["after the cursor went", "before"]
    finally:
        s.db.close()


# ---- an outermost end left by an interrupt: the count and SQLite agree ----


@pytest.mark.skipif(not hasattr(ctypes.CDLL(None), "raise"), reason="needs libc's raise()")
@pytest.mark.parametrize("end", ["transaction", "adapter"])
def test_a_commit_that_fails_with_an_interrupt_pending_leaves_no_transaction_to_join(scene, end):
    """The interrupt is raised by the first call after the failed ``COMMIT``: the count is
    0 and the rollback still runs, so no later statement of the thread joins the block's
    rows and commits them."""
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    with pytest.raises(KeyboardInterrupt):
        if end == "transaction":
            with scene.db.transaction():
                scene.insert("in the block")
                stop_once_and_interrupt(scene.conn)
        else:
            scene.adapter.begin_transaction()
            scene.insert("in the block")
            stop_once_and_interrupt(scene.conn)
            scene.adapter.end_transaction(commit=True)
    scene.let_statements_run()
    assert (scene.adapter._txn_depth, scene.conn.in_transaction) == (0, False)
    scene.insert("after")
    scene.adapter.auto_commit()
    assert scene.committed() == ["after", "before"]


# ---- a versioned update ends its transaction through the adapter ----


def _refused_update_whose_rollback_fails(s: Scene) -> BaseException:
    """A versioned update that a trigger refuses (the transaction stays open), with every
    rollback after it stopped; returns what left it."""
    _id, _ = s.db.upsert_with_version("docs", None, {"k": 0}, None)
    s.adapter.execute("CREATE TRIGGER refuse BEFORE UPDATE ON docs BEGIN SELECT RAISE(ABORT, 'refused'); END")
    s.adapter.auto_commit()
    rollbacks: list[str] = []
    s.conn.set_trace_callback(lambda sql: rollbacks.append(sql) if sql.startswith("ROLLBACK") else None)
    s.conn.set_progress_handler(lambda: 1 if rollbacks else 0, 1)
    try:
        s.db.upsert_with_version("docs", _id, {"k": 1}, 0)
    except BaseException as exc:  # noqa: BLE001 - what left is the result
        return exc
    raise AssertionError("the refused update raised nothing")


def test_a_versioned_update_whose_rollback_fails_twice_on_the_memory_connection_raises_its_own_error(tmp_path):
    s = Scene("memory", tmp_path)
    try:
        left = _refused_update_whose_rollback_fails(s)
        assert (type(left).__name__, str(left), getattr(left, "__notes__", [])) == ("IntegrityError", "refused", [])
        assert (s.adapter._shared._holder, s.conn.in_transaction) == (s.adapter._local.me, True)
        s.let_statements_run()
        s.adapter.execute("DROP TRIGGER refuse")
        s.adapter.auto_commit()
        assert s.db.find_document_with_version("docs", 1) == {"_id": 1, "_version": 0, "k": 0}
    finally:
        s.db.close()


@pytest.mark.parametrize("place", ["file", "named-memory"])
def test_a_versioned_update_whose_rollback_fails_twice_on_a_threads_own_connection_closes_it(tmp_path, caplog, place):
    s = Scene(place, tmp_path)
    try:
        caplog.set_level(logging.WARNING, logger="sqler.adapter.synchronous")
        left = _refused_update_whose_rollback_fails(s)
        messages = [r.getMessage() for r in caplog.records]
        assert (type(left).__name__, str(left), messages, s.adapter._local.conn) == ("IntegrityError", "refused", [REPLACED], None)
        s.adapter.execute("DROP TRIGGER refuse")
        s.adapter.auto_commit()
        assert s.db.find_document_with_version("docs", 1) == {"_id": 1, "_version": 0, "k": 0}
    finally:
        s.db.close()


def test_a_refused_versioned_update_leaves_its_own_error_at_once_while_another_thread_holds_a_transaction():
    """The update is refused at depth 0, its failed statement rolled back and gave the turn
    back; another thread opens a transaction before the update's end runs. The end has
    nothing of its thread's to roll back: it waits for no one, and the other thread's
    transaction commits untouched."""
    db = SQLerDB(SQLiteAdapter(":memory:", timeout_ms=5000))
    _id, _ = db.upsert_with_version("docs", None, {"k": 0}, None)
    db.adapter.execute("CREATE TABLE t (v TEXT)")
    db.adapter.execute("CREATE TRIGGER refuse BEFORE UPDATE ON docs BEGIN SELECT RAISE(ABORT, 'refused'); END")
    db.adapter.auto_commit()
    at_the_end, other_in, other_go = threading.Event(), threading.Event(), threading.Event()

    def other() -> None:
        assert at_the_end.wait(10)
        db.adapter.begin_transaction()
        db.adapter.execute("INSERT INTO t VALUES ('of the other thread')")
        other_in.set()
        assert other_go.wait(10)
        db.adapter.end_transaction(commit=True)

    def holds_the_end_until_the_other_is_in(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "__exit__" and frame.f_globals["__name__"] == ADAPTER:
            at_the_end.set()
            assert other_in.wait(10)
        return None

    raised: list[BaseException] = []
    thread = threading.Thread(target=lambda: raised.extend(_left_by(other)))
    thread.start()
    try:
        sys.settrace(holds_the_end_until_the_other_is_in)
        start = time.monotonic()
        try:
            with pytest.raises(sqlite3.IntegrityError) as left:
                db.upsert_with_version("docs", _id, {"k": 1}, 0)
        finally:
            sys.settrace(None)
        took = time.monotonic() - start
        assert (str(left.value), took < 2.5) == ("refused", True)  # half the 5 s lock wait
        other_go.set()
        thread.join(10)
        assert (thread.is_alive(), raised) == (False, [])
        rows = [r[0] for r in db.adapter.execute("SELECT v FROM t").fetchall()]
        assert (rows, db.find_document_with_version("docs", _id)) == (["of the other thread"], {"_id": 1, "_version": 0, "k": 0})
    finally:
        other_go.set()
        db.close()


def _left_by(work) -> list[BaseException]:
    try:
        work()
    except BaseException as exc:  # noqa: BLE001 - handed to the test thread
        return [exc]
    return []
