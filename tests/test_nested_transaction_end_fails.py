"""A nested transaction of the sync adapter whose end fails in SQLite.

The end of a nested transaction is ``RELEASE`` (commit) or ``ROLLBACK TO`` then ``RELEASE``
(rollback). When SQLite refuses one (a raw statement already ended the savepoint, say), the
transaction can no longer be trusted: no ``RELEASE`` follows a failed ``ROLLBACK TO``, the
nested end raises ``TransactionStateError`` with SQLite's error as ``__cause__``, every
later statement of the thread in that transaction raises it and sends nothing, and the
transaction ends rolled back with the same error. The async adapter says the same sentence
for the same failure.
"""

import asyncio
import signal
import sys
import threading
import time

import pytest
from sqler import AsyncSQLerDB, SQLerDB
from sqler.adapter import SQLiteAdapter
from sqler.exceptions import TransactionStateError
from sqler.logging import query_logger

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
            while sys._current_frames()[main].f_code.co_name != "take":
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
