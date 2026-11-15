import logging
import sqlite3
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from itertools import islice
from typing import Any, ContextManager, NoReturn, Optional, Self

from sqler.logging import query_logger

from ..exceptions import NotConnectedError, QueryError, TransactionStateError, unique_violation_from
from .abstract import AdapterABC, CursorLike

logger = logging.getLogger(__name__)


_BROKEN = (
    "a nested transaction could not be {}, so the transaction it is part of ends rolled back: "
    "nothing written in it is committed"
)
# made once: an end that failed records its sentence with no call an interrupt could land in
_COMMIT_FAILED = _BROKEN.format("committed")
_ROLLBACK_FAILED = _BROKEN.format("rolled back")


_INSIDE = (
    "commit() cannot end a transaction opened by begin_transaction: nothing was committed and the "
    "transaction goes on; end it by leaving its db.transaction() block, or with end_transaction()"
)


_ENDED_BEHIND = (
    "a statement ended the transaction behind the adapter (a raw COMMIT, END or ROLLBACK, or a failure "
    "SQLite answers by rolling the transaction back): what was written in it before that statement was "
    "committed or rolled back by it and cannot be put back, and the transaction sends nothing more; end a "
    "transaction by leaving its db.transaction() block, and raise in the block to roll it back"
)


_PENDING = (
    "a transaction cannot begin: SQLite holds a transaction on this connection that the adapter did not "
    "open (a raw BEGIN, or a raw write not committed yet); nothing was sent and that work is still pending: "
    'keep it with db.adapter.commit() or drop it with db.adapter.execute("ROLLBACK"), then begin again'
)


def _broken(failed: tuple[str, Optional[BaseException]]) -> TransactionStateError:
    """The error for a statement or an end in a transaction that can no longer be trusted,
    ``(sentence, cause)``: a nested end failed, or a statement ended the transaction."""
    error = TransactionStateError(failed[0])
    error.__cause__ = failed[1]
    return error


_KEPT = (
    'the transaction could not be rolled back: the one connection of the ":memory:" database keeps it, '
    "and every statement tries the rollback again first"
)


def _kept(error: BaseException) -> TransactionStateError:
    """The error for an end or a statement while the one ``":memory:"`` connection keeps a
    transaction its rollback could not end (``error``: SQLite's, from the last try)."""
    kept = TransactionStateError(_KEPT)
    kept.__cause__ = error
    return kept


def _roll_back(conn: sqlite3.Connection, shared: Optional["_SharedConnection"], local: Optional["_ThreadState"]) -> None:
    """Roll back ``conn``'s transaction; a rollback that fails is tried once more at once.

    Whatever leaves the tries (SQLite's error twice, an interrupt between them), a
    transaction SQLite still counts as open is never joined by a later statement. A thread's
    own connection (``local``'s) is forgotten and closed, which rolls back: the thread opens
    a new one at its next statement. The one ``":memory:"`` connection (``shared``) cannot
    be closed without losing the database: it keeps the transaction and its holder, and
    ``shared.unended`` names what left until a rollback that ``take`` tries succeeds.
    SQLite's error ends here; anything else is raised on.
    """
    left: Optional[BaseException] = None
    try:
        try:
            conn.rollback()
        except sqlite3.Error:
            conn.rollback()  # an interrupt that was a one-off
    except BaseException as exc:  # noqa: BLE001 - settled below, and raised on unless SQLite's
        left = exc
    try:
        kept = left is not None and conn.in_transaction
    except sqlite3.Error:  # closed meanwhile, which rolled back
        kept = False
    if shared is not None:
        shared.unended = left if kept else None
    elif kept and local is not None:
        if local.conn is conn:
            local.conn = local.owner = None
        conn.close()  # SQLite rolls back what a closing connection leaves open
        logger.warning("rollback of a reclaimed connection failed (%s: %s); replacing it", type(left).__name__, left)
    if left is not None and not isinstance(left, sqlite3.Error):
        raise left


def _end_outermost(
    conn: sqlite3.Connection, commit: bool, shared: Optional["_SharedConnection"], local: "_ThreadState"
) -> None:
    """Commit or roll back the thread's outermost transaction on ``conn``."""
    if commit:
        conn.commit()
        return
    _roll_back(conn, shared, local)
    if shared is not None and shared.unended is not None:
        raise _kept(shared.unended)


def _end_savepoint(conn: sqlite3.Connection, depth: int, commit: bool) -> None:
    """Release the savepoint of nesting ``depth``, rolled back to first unless ``commit``."""
    if not commit:
        conn.execute(f"ROLLBACK TO SAVEPOINT sp_{depth}")
    conn.execute(f"RELEASE SAVEPOINT sp_{depth}")


def _lock_wait(timeout_ms: Optional[int]) -> tuple[int, bool]:
    """An adapter's ``timeout_ms`` argument checked: ``(milliseconds, given)``; not given, 5000.

    Raises:
        ValueError: ``timeout_ms`` is not a whole number from 0 to 2**31 - 1.
    """
    if timeout_ms is None:
        return 5000, False
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 0 <= timeout_ms < 2**31:
        raise ValueError(f"timeout_ms must be a whole number of milliseconds from 0 to 2**31 - 1, not {timeout_ms!r}")
    return timeout_ms, True


def _agreed_lock_wait(timeout_ms: int, given: bool, busy: int) -> int:
    """The lock wait once SQLite has read the pragmas and reads back ``busy`` as its busy_timeout.

    A pragma decides the number when ``timeout_ms`` was not given; both given, they must agree.
    """
    if busy != timeout_ms and given:
        raise ValueError(f"timeout_ms={timeout_ms} and the busy_timeout pragma ({busy}) disagree: give one of them")
    return busy


def _names_memory_database(path: str) -> bool:
    """True for a ``file:`` URI that names an in-memory database: ``file::memory:``,
    ``mode=memory`` or ``vfs=memdb`` (not the plain ``":memory:"``, which is one connection)."""
    if not path.startswith("file:"):
        return False
    name, _, query = path[5:].partition("?")
    options = query.partition("#")[0].split("&")
    return name == ":memory:" or "mode=memory" in options or "vfs=memdb" in options


class _ThreadConnection:
    """Closes one thread's connection when the thread ends.

    Only the thread's local storage holds it (the adapter's registry holds it
    weakly). CPython drops a thread's local storage as the thread ends, before
    ``join()`` returns, so the connection closes then: SQLite rolls back what the
    thread left uncommitted and its lock is free.
    """

    __slots__ = ("conn", "__weakref__")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass  # Connection may already be closed

    __del__ = close


class _ThreadState(threading.local):
    """One thread's connection (``None`` until it opens one), depth of explicit transactions,
    and why its transaction can no longer be trusted (``None``: it can), as ``_broken`` reads it.

    On a plain ``":memory:"`` adapter (``shared`` given) each thread's state is made with
    the thread's end hook (``turn_end``) the first time the thread touches it, so the hook
    exists before the thread can hold the shared connection; ``me`` names the thread to it.
    """

    conn: Optional[sqlite3.Connection] = None
    owner: Optional[_ThreadConnection] = None
    txn_depth = 0
    broken: Optional[tuple[str, Optional[BaseException]]] = None
    turn_end: Optional["_TurnEnd"] = None
    me: Optional["weakref.ref[_TurnEnd]"] = None

    def __init__(self, shared: Optional["_SharedConnection"] = None):
        if shared is not None:
            self.turn_end = end = _TurnEnd(shared)
            self.me = end.token


class _Block:
    """Marks one block holding the shared connection (``_holding``): it lives as long as the
    block's frame, and a weak reference to it keeps the connection for the block."""

    __slots__ = ("__weakref__",)


class _SharedConnection:
    """The one connection of a plain ``":memory:"`` adapter, which every thread shares.

    A transaction on it belongs to the thread that opened it, by ``BEGIN`` or by the
    write Python's ``sqlite3`` opens one for: until it ends, every statement of another
    thread waits, at most ``timeout`` seconds, as a second connection waits for a locked
    file database. A ``with`` on ``lock`` holds it for one statement; ``take`` makes the
    thread the holder before the statement and ``give`` keeps it the holder while its
    transaction is open.

    The lock is only ever held by a ``with`` on ``lock`` itself, so an exception leaves no
    lock held. Any part of the bookkeeping around a statement can be skipped by an exception
    (a signal, a trace function, one set from another thread), in any thread; each value
    the next statement of any thread reads is right whatever was skipped:

    - ``_holder``: the thread (its ``_ThreadState.me``, a weak reference to its end hook)
      whose statement runs, whose transaction is open, or whose block holds the connection.
      ``take`` sets it before the statement; a ``give`` that is skipped leaves it set. A
      holder whose thread ended, or that keeps nothing (no transaction open on the
      connection, no live ``_kept``), is left behind: the next ``take`` of any thread rolls
      back what a dead holder left open and goes on at once.
    - ``_kept``: what keeps the holder's turn beyond its statement when SQLite counts no
      transaction (an explicit transaction SQLite ended itself, a ``_holding`` block): the
      holder's ``me`` set by ``give``, or a weak reference to the block's ``_Block``. Cleared
      whenever ``_holder`` is, and by every ``take``: a skipped ``give`` leaves it ``None``.
    - The end hook (``_TurnEnd``): made with the thread's state, before ``take`` reads it,
      so a thread that ends as the holder has its transaction rolled back and the turn
      freed; a thread whose hook raised is a dead holder, found by the next ``take``. A
      thread is named by its hook, never by its ident: a new thread with a dead one's
      ident is another holder.
    - ``_waiting``: counts the threads in ``wait_for``; one that is skipped leaves it too
      high, which costs a needless wake-up, never a missed one. A thread already waiting
      when a holder is left behind is not woken by it: when its wait runs out it finds
      the holder left behind and goes on.

    A transaction whose rollback failed stays open on the connection with its holder
    (``unended``: SQLite's error). Every statement, of any thread, first tries the rollback
    again; until one succeeds the holder's statements raise, and the other threads wait.

    Every change that can let a waiter go on (the holder gives the connection up, ends, or
    the connection closes) wakes every waiter: each checks again, and those that still
    cannot go on wait again. A waiter that leaves ``take`` by an exception holds nothing.
    """

    __slots__ = (
        "conn", "timeout", "lock", "_turn", "_holder", "_kept", "_waiting", "_closed", "unended", "__weakref__",
    )

    def __init__(self, conn: sqlite3.Connection, timeout: float):
        self.conn = conn
        self.timeout = timeout
        self.lock = threading.Lock()
        self._turn = threading.Condition(self.lock)  # waits and wakes; never entered with ``with``
        self._holder: Optional[weakref.ref[_TurnEnd]] = None  # None: no one has the connection
        self._kept: Optional[weakref.ref[Any]] = None
        self._waiting = 0  # threads that wait for the holder's transaction to end
        self._closed = False
        self.unended: Optional[BaseException] = None

    def _free(self) -> bool:
        return self._holder is None or self._closed

    def take(self, local: _ThreadState) -> sqlite3.Connection:
        """Wait until no other thread's transaction or block has the connection and make the
        calling thread (``local``'s) its holder. Called holding ``lock``; the caller then
        calls ``give``.

        Raises ``TransactionStateError`` in a transaction whose nested end failed: nothing is taken.
        """
        holder = self._holder
        # No one has it (``_kept`` is None with it). A thread whose nested end failed raises
        # below even then: it can have been left behind (SQLite ended its transaction, its
        # ``give`` was skipped) and freed by another thread.
        if holder is None and not self._closed and local.broken is None:
            self._holder = local.me
            return self.conn
        if local.broken is not None:
            raise _broken(local.broken)
        me = local.me
        if holder is not me and holder is not None and not self._closed:
            holder = self._behind(holder)
        if self.unended is not None and not self._closed:
            _roll_back(self.conn, self, None)
            if self.unended is None:
                holder = self._holder = self._kept = None
                self._turn.notify_all()
            elif holder is me:
                raise _kept(self.unended)
        if holder is not None and holder is not me:
            self._waiting += 1
            try:
                free = self._turn.wait_for(self._free, self.timeout)
            finally:
                self._waiting -= 1
            if not free:
                # No one wakes a waiter for a holder left behind while it waited (a
                # skipped ``give``, an end hook that raised): it looks once more itself.
                left = self._holder
                if left is not None and self._behind(left) is not None:
                    raise sqlite3.OperationalError(
                        "database is locked: another thread's transaction holds the in-memory connection"
                    )
        if self._closed:
            raise NotConnectedError("Database not connected, call connect() first")
        self._holder = me
        self._kept = None
        return self.conn

    def _behind(self, holder: "weakref.ref[_TurnEnd]") -> Optional["weakref.ref[_TurnEnd]"]:
        """Another thread's ``holder``, or ``None`` once it is found left behind and the turn
        freed: its thread ended (what it left open is rolled back first), or it keeps nothing."""
        if self.unended is not None:
            return holder  # its rollback is tried by the caller
        if holder() is None:
            if self.conn.in_transaction:
                _roll_back(self.conn, self, None)
                if self.unended is not None:
                    return holder
        else:
            kept = self._kept
            if self.conn.in_transaction or (kept is not None and kept() is not None):
                return holder
        self._holder = self._kept = None
        return None

    def take_for(self, local: _ThreadState, block: _Block) -> sqlite3.Connection:
        """``take`` for a block of work that holds the connection while ``block`` lives."""
        conn = self.take(local)
        self._kept = weakref.ref(block)
        return conn

    def retake(self, local: _ThreadState) -> Optional[sqlite3.Connection]:
        """The connection, taken again by ``local``'s thread if it is the holder; ``None``
        otherwise: nothing of the thread's is open on it, and nothing is waited for."""
        if self._holder is not local.me or self._closed:
            return None
        self._kept = None
        return self.conn

    def give(self, local: _ThreadState) -> None:
        """After the statement of ``local``'s thread: it stays the holder while its
        transaction is open."""
        try:
            open_ = local.txn_depth > 0 or self.conn.in_transaction
        except sqlite3.Error:  # the connection was closed under the statement
            open_ = False
        if open_:
            self._kept = self._holder
            return
        self._holder = self._kept = None
        if self._waiting:
            self._turn.notify_all()

    def end_of(self, token: "weakref.ref[_TurnEnd]") -> None:
        """The thread named ``token`` ended: if it held the turn, roll back what it left open and free it.

        Only the thread itself makes ``token`` the holder, so a thread that is not the
        holder now stays not the holder: it ends without the lock, whoever holds it.
        """
        if self._holder is not token:
            return
        with self.lock:
            if self._holder is not token:
                return
            _roll_back(self.conn, self, None)
            if self.unended is None:
                self._holder = self._kept = None
                self._turn.notify_all()

    def close(self) -> None:
        """Close the connection; a thread that waits for the turn raises ``NotConnectedError``."""
        with self.lock:
            self._closed = True
            self._holder = self._kept = None
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            finally:
                self._turn.notify_all()


class _TurnEnd:
    """Held by one thread's local storage only: dropped as the thread ends, it hands the
    turn of the shared connection back (see ``_ThreadConnection`` for when that runs).
    ``token``, a weak reference to it, names the thread as the holder."""

    __slots__ = ("shared", "token", "__weakref__")

    def __init__(self, shared: _SharedConnection):
        self.shared = weakref.ref(shared)  # weak: a thread does not keep a closed connection alive
        self.token = weakref.ref(self)

    def __del__(self) -> None:
        shared = self.shared()
        if shared is not None:
            shared.end_of(self.token)


class _ReadRows:
    """The rows of one statement on the shared ``":memory:"`` connection, all read inside
    the statement's turn, served as its ``sqlite3.Cursor`` would serve them.

    SQLite reads a statement's rows as they are fetched; on a connection another thread
    writes on once the turn is given back, a later fetch would see that thread's
    uncommitted rows.
    """

    __slots__ = ("description", "rowcount", "lastrowid", "arraysize", "_rows")

    def __init__(self, cursor: sqlite3.Cursor):
        self._rows: Optional[Iterator[Any]] = iter(cursor.fetchall())
        self.description = cursor.description
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid
        self.arraysize = cursor.arraysize
        cursor.close()

    def _left(self) -> Iterator[Any]:
        rows = self._rows
        if rows is None:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        return rows

    def fetchone(self) -> Any:
        return next(self._left(), None)

    def fetchmany(self, size: Optional[int] = None) -> list[Any]:
        return list(islice(self._left(), self.arraysize if size is None else size))

    def fetchall(self) -> list[Any]:
        return list(self._left())

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> Any:
        return next(self._left())

    def close(self) -> None:
        self._rows = None


class SQLiteAdapter(AdapterABC):
    """Synchronous SQLite adapter with WAL and thread-local connections.

    Features:
        - Thread-local connections for safe concurrent access; a thread's
          connection closes when the thread ends
        - A plain ``":memory:"`` adapter has one connection for every thread: a
          transaction on it belongs to the thread that opened it, and the other
          threads' statements wait for it to end (at most ``timeout_ms``)
        - WAL mode for improved concurrency
        - One lock wait, ``timeout_ms``, on every connection
        - Optional slow query logging
        - Transaction-aware auto-commit (respects explicit transactions)
        - A statement or commit that fails outside an explicit transaction
          rolls back what was sent since the last commit (one unit: the
          transaction Python's ``sqlite3`` opened for it), so the thread's
          connection keeps no lock
        - A nested transaction whose end fails in SQLite, or a statement that
          ends the transaction behind the adapter (a raw ``COMMIT``), leaves the
          transaction broken: that end or statement, every later statement of the
          thread in the transaction and the transaction's own end raise
          ``TransactionStateError``, and it ends rolled back
        - ``begin_transaction`` on a connection SQLite holds in a transaction the
          adapter did not open raises ``TransactionStateError`` and sends nothing

    Args:
        path: Path to the database file, or ":memory:" for in-memory.
        pragmas: Optional list of PRAGMA statements to execute on connect.
        timeout_ms: How long a statement waits for a lock, in milliseconds.
    """

    def __init__(
        self,
        path: str = "sqler.db",
        pragmas: Optional[list[str]] = None,
        timeout_ms: Optional[int] = None,
    ):
        """Initialize the adapter.

        Args:
            path: Path to the SQLite database file.
            pragmas: Optional list of SQL PRAGMA statements to apply on connection.
            timeout_ms: How long a statement waits for another connection's lock, in
                milliseconds (SQLite's ``busy_timeout`` on every connection the adapter
                opens; on a plain ``":memory:"`` adapter, also the wait for another
                thread's transaction). It does not reach a shared-cache in-memory
                database: SQLite calls no busy handler for a table lock there, so a
                statement that meets one raises ``sqlite3.OperationalError`` ("database
                table is locked") at once.
                Not given: a ``busy_timeout`` pragma in ``pragmas``, else 5000. Given
                beside such a pragma, the two must agree (``connect()`` raises
                ``ValueError`` otherwise).

        Raises:
            ValueError: ``timeout_ms`` is not a whole number from 0 to 2**31 - 1.
        """
        self.timeout_ms, self._timeout_given = _lock_wait(timeout_ms)
        self.path = path
        self.pragmas = pragmas or []
        self._local = _ThreadState()
        self._conns_lock = threading.RLock()
        self._conns: weakref.WeakSet[_ThreadConnection] = weakref.WeakSet()
        self._connected: bool = False
        # The one connection of a plain ":memory:" adapter (see ``_SharedConnection``).
        self._shared: Optional[_SharedConnection] = None
        # SQLite drops a shared-cache in-memory database when its last connection closes,
        # and a thread's connection closes when the thread ends. This connection runs no
        # statement; it keeps the database alive from connect() to close().
        self._anchor: Optional[sqlite3.Connection] = None
        self._memory_singleton = path == ":memory:"

    @property
    def _txn_depth(self) -> int:
        """Depth of this thread's explicit transactions (0 = none).

        Per thread on every database: on the single ``":memory:"`` connection a
        transaction belongs to the thread that opened it.
        """
        return self._local.txn_depth

    @_txn_depth.setter
    def _txn_depth(self, value: int) -> None:
        self._local.txn_depth = value

    def _still_open(self, conn: sqlite3.Connection, exc: Optional[BaseException] = None) -> None:
        """After a statement in this thread's transaction (``exc``: the statement failed with it):
        if SQLite no longer counts the transaction open, the statement ended it behind the
        adapter, and from here the transaction is broken. Raises ``TransactionStateError``
        then (from ``exc``), unless ``exc`` is an interrupt, which is raised on by the caller."""
        if conn.in_transaction:
            return
        local = self._local
        local.broken = (_ENDED_BEHIND, exc)
        if exc is None or isinstance(exc, Exception):
            raise _broken(local.broken)

    def _end_failed_implicit_transaction(
        self, conn: sqlite3.Connection, exc: Optional[BaseException] = None, judged: bool = False
    ) -> None:
        """A statement or commit on ``conn`` failed (with ``exc``): outside an explicit
        transaction, roll back what was sent since the last commit.

        Python's ``sqlite3`` opens a transaction before a write and keeps it open
        when the write fails; left open it holds the write lock (or a shared-cache
        table lock) until this thread's next commit, and the next commit would make
        part of the unit durable. Inside an explicit transaction nothing changes: the
        caller ends the transaction. ``judged``: the statement was sent with
        ``_execute_judged``, whose caller reads an ``Exception`` as an answer: it ends
        nothing (an interrupt ends the unit as any failure does).
        """
        if self._txn_depth > 0 or not conn.in_transaction or (judged and isinstance(exc, Exception)):
            return
        _roll_back(conn, self._shared, self._local)  # the caller sees the original error

    def _roll_back_left_open(self, local: _ThreadState) -> None:
        """Roll back what SQLite still counts open on the connection of ``local``'s thread, out
        of every explicit transaction. A connection a failed rollback closed, or the
        ``":memory:"`` one keeping its transaction, is already settled by ``_roll_back``."""
        shared = self._shared
        if shared is None:
            if local.conn is not None:
                self._end_failed_implicit_transaction(local.conn)
        elif shared.unended is None:
            with shared.lock:
                conn = shared.retake(local)  # waits for no one: not the holder, nothing is open
                if conn is None:
                    return
                try:
                    self._end_failed_implicit_transaction(conn)
                finally:
                    shared.give(local)

    def connect(self) -> None:
        if self._memory_singleton:
            with self._conns_lock:
                if self._shared is None:
                    shared = _SharedConnection(self._open(), self.timeout_ms / 1000)
                    self._local = _ThreadState(shared)  # before any statement can reach ``shared``
                    self._shared = shared
        elif self._anchor is None and _names_memory_database(self.path):
            with self._conns_lock:
                if self._anchor is None:
                    self._anchor = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        was_connected = self._connected
        self._connected = True
        try:
            self._conn()  # a connection for this thread
        except BaseException:
            if not was_connected:
                self.close()  # the connect that failed leaves nothing open
            raise

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            # not connect(timeout=): Python truncates the seconds to milliseconds (1.001 s waits 1000 ms)
            cur.execute(f"PRAGMA busy_timeout = {self.timeout_ms}")
            for pragma in self.pragmas:
                cur.execute(pragma)
            conn.commit()
            if self.pragmas:
                # SQLite has read the pragmas: its busy_timeout is the one number
                busy = cur.execute("PRAGMA busy_timeout").fetchone()[0]
                self.timeout_ms = _agreed_lock_wait(self.timeout_ms, self._timeout_given, busy)
        except BaseException:
            conn.close()  # a connection that could not be set up is not left open
            raise
        return conn

    def _holding(self) -> ContextManager[sqlite3.Connection]:
        """This thread's connection, held for a block of work outside the statement methods
        (a backup, a restore): on a plain ``":memory:"`` adapter the block waits for the turn
        as a statement does and gives it back by the same rule; elsewhere it is ``_conn()``."""
        shared = self._shared
        return nullcontext(self._conn()) if shared is None else self._turn_of(shared)

    @contextmanager
    def _turn_of(self, shared: _SharedConnection) -> Iterator[sqlite3.Connection]:
        # The thread holds the turn as a holder, not by the lock: the lock held across the
        # ``yield`` would stay held when an exception leaves before the caller's block begins.
        # ``block`` keeps the turn while this frame lives; an exit that is skipped drops it.
        local = self._local
        block = _Block()
        with shared.lock:
            conn = shared.take_for(local, block)
        try:
            yield conn
        finally:
            with shared.lock:
                del block
                shared.give(local)

    def _conn(self) -> sqlite3.Connection:
        """This thread's connection; on a plain ``":memory:"`` adapter the shared one,
        whose statements hold its turn (``_SharedConnection``). Code outside the adapter
        that works on the connection itself holds it with ``_holding()``.

        Raises ``TransactionStateError`` in a transaction whose nested end failed: the ends
        of a transaction read ``_local.conn`` themselves."""
        if not self._connected:
            raise NotConnectedError("Database not connected, call connect() first")
        local = self._local  # read once: close() on another thread replaces it
        if local.broken is not None:
            raise _broken(local.broken)
        conn = local.conn
        if conn is not None:
            return conn
        if self._memory_singleton:
            shared = self._shared
            if shared is None:  # close() on another thread
                raise NotConnectedError("Database not connected, call connect() first")
            return shared.conn
        conn = self._open()
        local.conn = conn  # what every statement reads
        local.owner = owner = _ThreadConnection(conn)
        with self._conns_lock:
            self._conns.add(owner)
        return conn

    def close(self) -> None:
        self._connected = False
        with self._conns_lock:
            for owner in list(self._conns):
                owner.close()
            self._conns.clear()
        shared, self._shared = self._shared, None
        if shared is not None:
            shared.close()
        with self._conns_lock:
            anchor, self._anchor = self._anchor, None
        if anchor is not None:
            try:
                anchor.close()
            except sqlite3.Error:
                pass
        # A fresh thread-local object: a thread that outlives close() holds nothing of the
        # old one (connection, transaction depth) and opens a new connection on connect().
        self._local = _ThreadState()

    def execute(self, query: str, params: Optional[list[Any]] = None) -> CursorLike:
        """Execute a SQL query with optional parameters and return cursor.

        On the one connection of a plain ``":memory:"`` adapter, a statement that returns
        rows has every row read before its turn is given back, and returns them in a
        ``_ReadRows``; everywhere else the ``sqlite3.Cursor`` is returned unread.

        Automatically logs the query if query_logger is enabled.
        """
        shared = self._shared  # not None: the one ":memory:" connection, which waits for its turn
        if shared is None:
            return self._execute(self._conn(), query, params, False, False)
        local = self._local
        with shared.lock:
            conn = shared.take(local)
            try:
                return self._execute(conn, query, params, True, False)
            finally:
                shared.give(local)

    def _execute_judged(self, query: str, params: Optional[list[Any]] = None) -> CursorLike:
        """``execute`` for a library caller that reads the statement's failure as an answer
        (the look for referrers in ``sqler.models.integrity``): outside a transaction an
        ``Exception`` of the statement ends nothing and is raised as it is. A failure the
        caller does not read as an answer goes to ``_end_failed_statement``. Inside a
        transaction, and for an interrupt, it is ``execute``. Not for code outside sqler."""
        shared = self._shared
        if shared is None:
            return self._execute(self._conn(), query, params, False, True)
        local = self._local
        with shared.lock:
            conn = shared.take(local)
            try:
                return self._execute(conn, query, params, True, True)
            finally:
                shared.give(local)

    def _end_failed_statement(self, exc: Exception) -> NoReturn:
        """After ``_execute_judged`` raised ``exc`` and its caller found it no answer: end what
        ``execute`` ends for a failed statement (outside a transaction, all that was sent
        since the last commit; inside one, nothing) and raise ``exc``. Not for code outside sqler."""
        self._roll_back_left_open(self._local)
        raise exc

    def _execute(
        self, conn: sqlite3.Connection, query: str, params: Optional[list[Any]], read_all: bool, judged: bool
    ) -> CursorLike:
        inside = self._local.txn_depth > 0
        cursor: Any = conn.cursor()
        _logging = query_logger.enabled
        if _logging:
            start = time.perf_counter()
        error_msg = None
        try:
            if params is not None:
                if isinstance(params, list):
                    params = tuple(params)
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            if read_all and cursor.description is not None:
                cursor = _ReadRows(cursor)  # a read that fails is a failed statement
        except BaseException as exc:  # an interrupt counts as a failure
            error_msg = str(exc) or type(exc).__name__
            if inside:
                self._still_open(conn, exc)
            else:
                self._end_failed_implicit_transaction(conn, exc, judged)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        finally:
            if _logging:
                duration_ms = (time.perf_counter() - start) * 1000
                query_logger.log(
                    sql=query,
                    params=list(params) if params else [],
                    duration_ms=duration_ms,
                    rows_affected=cursor.rowcount if cursor.rowcount >= 0 else None,
                    error=error_msg,
                )
        if inside and not conn.in_transaction:
            self._still_open(conn)
        return cursor

    def executemany(self, query: str, param_list: Optional[list[Any]]) -> sqlite3.Cursor:
        """Execute a param query multiple times with different parameter sets.

        Inside an explicit transaction the rows are part of it (a rollback
        removes them); outside one they are committed, and a failure stores
        none of the rows.
        """
        shared = self._shared
        if shared is None:
            return self._executemany(self._conn(), query, param_list)
        local = self._local
        with shared.lock:
            conn = shared.take(local)
            try:
                return self._executemany(conn, query, param_list)
            finally:
                shared.give(local)

    def _executemany(self, conn: sqlite3.Connection, query: str, param_list: Optional[list[Any]]) -> sqlite3.Cursor:
        inside = self._local.txn_depth > 0
        try:
            cursor = conn.cursor()
            cursor.executemany(query, param_list or [])
            if not inside:
                conn.commit()
        except BaseException as exc:
            if inside:
                self._still_open(conn, exc)
            else:
                self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        if inside and not conn.in_transaction:
            self._still_open(conn)
        return cursor

    def executescript(self, script: str) -> sqlite3.Cursor:
        """Run a script of statements and commit.

        Raises ``QueryError`` inside an explicit transaction: SQLite's driver commits
        the open transaction before it runs a script, which would end it unseen.
        """
        if self._txn_depth > 0:
            if self._local.broken is not None:
                raise _broken(self._local.broken)
            raise QueryError(
                "executescript cannot run inside a transaction: "
                "SQLite commits the open transaction before it runs a script",
                sql=script,
            )
        shared = self._shared
        if shared is None:
            return self._executescript(self._conn(), script)
        local = self._local
        with shared.lock:
            conn = shared.take(local)
            try:
                return self._executescript(conn, script)
            finally:
                shared.give(local)

    def _executescript(self, conn: sqlite3.Connection, script: str) -> sqlite3.Cursor:
        try:
            cursor = conn.cursor()
            cursor.executescript(script)
            conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise
        return cursor

    def commit(self) -> None:
        """Commit what this thread wrote outside an explicit transaction.

        A commit that fails (a deferred constraint, a lock) rolls the transaction back
        before the error is raised. Inside this thread's explicit transaction it raises
        ``TransactionStateError`` and commits nothing: the transaction goes on, and
        ``end_transaction`` ends it (``auto_commit`` is the call that skips the commit there).
        """
        if self._txn_depth > 0:
            raise _broken(self._local.broken) if self._local.broken is not None else TransactionStateError(_INSIDE)
        shared = self._shared
        if shared is None:
            self._commit(self._conn())
        else:
            local = self._local
            with shared.lock:
                conn = shared.take(local)
                try:
                    self._commit(conn)
                finally:
                    shared.give(local)

    def _commit(self, conn: sqlite3.Connection) -> None:
        try:
            conn.commit()
        except BaseException as exc:
            self._end_failed_implicit_transaction(conn)
            converted = unique_violation_from(exc)
            if converted is not None:
                raise converted from exc
            raise

    def auto_commit(self) -> None:
        """Commit only if NOT inside an explicit transaction.

        Use this instead of commit() in operations like insert/update/delete
        so that explicit transactions work correctly with model.save().
        """
        if self._txn_depth == 0:
            self.commit()

    @property
    def in_transaction(self) -> bool:
        """Return True if currently inside an explicit transaction."""
        return self._txn_depth > 0

    def begin_transaction(self) -> None:
        """Begin an explicit transaction (increments depth counter).

        Uses SAVEPOINTs for nested transactions to allow proper inner rollback.
        """
        shared = self._shared
        if shared is None:
            self._begin(self._conn())
        else:
            local = self._local
            with shared.lock:
                conn = shared.take(local)
                try:
                    self._begin(conn)
                finally:
                    shared.give(local)

    def _begin(self, conn: sqlite3.Connection) -> None:
        depth = self._txn_depth  # nested: a savepoint
        if depth == 0 and conn.in_transaction:
            raise TransactionStateError(_PENDING)
        conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT sp_{depth}")
        self._txn_depth = depth + 1

    def end_transaction(self, *, commit: bool = True) -> None:
        """End this thread's innermost explicit transaction (decrements depth counter).

        Args:
            commit: If True, commit on outermost transaction. If False, rollback.

        Raises:
            TransactionStateError: the end of a nested transaction failed in SQLite, here
                or earlier in this transaction (``__cause__`` is SQLite's error). The
                transaction ends rolled back whatever ``commit`` says; a nested end after
                the failure sends nothing. An interrupt during a nested end leaves as it
                is, and the transaction ends rolled back the same way.
        """
        local = self._local
        depth = local.txn_depth - 1
        if depth < 0:
            return  # No active transaction
        failed = local.broken
        shared = self._shared
        # The count changes inside each ``try``: whatever leaves the end (an exception at
        # any point of it, a failed commit), the count and SQLite agree. Outermost, no
        # transaction is left that a later statement of the thread joins; nested, the
        # transaction is marked to end rolled back. An exception pending as SQLite failed
        # is raised by the first call after it, the rollback's own: it is tried once more.
        if depth == 0:
            try:
                local.txn_depth = 0
                local.broken = None
                if shared is None:
                    _end_outermost(local.conn, commit and failed is None, None, local)
                else:
                    with shared.lock:
                        conn = shared.take(local)
                        try:
                            _end_outermost(conn, commit and failed is None, shared, local)
                        finally:
                            shared.give(local)
            except BaseException:
                local.txn_depth = 0
                local.broken = None
                try:
                    self._roll_back_left_open(local)
                except BaseException:
                    self._roll_back_left_open(local)
                    raise
                raise
        elif failed is None:
            try:
                local.txn_depth = depth
                if shared is None:
                    _end_savepoint(local.conn, depth, commit)
                else:
                    with shared.lock:
                        conn = shared.take(local)
                        try:
                            _end_savepoint(conn, depth, commit)
                        finally:
                            shared.give(local)
            except BaseException as exc:
                # a RELEASE after a failed ROLLBACK TO would keep the rows it was to undo
                local.txn_depth = depth
                local.broken = failed = (_COMMIT_FAILED if commit else _ROLLBACK_FAILED, exc)
                if not isinstance(exc, Exception):
                    raise
        else:
            local.txn_depth = depth
        if failed is not None:
            raise _broken(failed)

    def __enter__(self):
        """Enter context manager; connect if not connected"""
        if self._local.conn is None:
            self.connect()
        else:
            self._conn()  # in a transaction whose nested end failed: raises
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        """Exit context manager: commit the block's writes, or roll them back while an
        exception leaves; inside this thread's explicit transaction, the transaction goes on."""
        if not self._connected or self._txn_depth > 0:
            return
        if exception_type is None:
            self.commit()
            return
        self._roll_back_left_open(self._local)  # an error of its own is not raised

    ### factories

    @classmethod
    def in_memory(cls, shared: bool = True, name: Optional[str] = None, *, timeout_ms: int = 5000) -> Self:
        """Connects to an in memory db with some pragmas applied; ``timeout_ms`` as for the constructor."""
        pragmas = [
            "PRAGMA foreign_keys = ON",
            "PRAGMA synchronous = OFF",
            "PRAGMA journal_mode = MEMORY",
            "PRAGMA temp_store = MEMORY",
            "PRAGMA cache_size = -32000",
            "PRAGMA locking_mode = EXCLUSIVE",
        ]
        if shared:
            ident = name or f"sqler-{uuid.uuid4().hex}"
            uri = f"file:{ident}?mode=memory&cache=shared"
        else:
            uri = ":memory:"
        return cls(uri, pragmas=pragmas, timeout_ms=timeout_ms)

    _PRAGMA_KEYS = [
        "foreign_keys", "busy_timeout", "journal_mode", "synchronous",
        "cache_size", "wal_autocheckpoint", "mmap_size", "temp_store",
    ]

    def pragma_report(self) -> dict[str, Any]:
        """Return current PRAGMA values for key settings.

        Queries the active connection for each tracked PRAGMA and returns
        a dict of ``{pragma_name: value}``. Useful for downstream libraries
        to verify that expected PRAGMAs are in effect.

        Returns:
            dict[str, Any]: Mapping of PRAGMA names to their current values.
        """
        report: dict[str, Any] = {}
        for key in self._PRAGMA_KEYS:
            cursor = self.execute(f"PRAGMA {key};")
            row = cursor.fetchone()
            report[key] = row[0] if row else None
        return report

    @classmethod
    def on_disk(cls, path: str = "sqler.db", *, timeout_ms: int = 5000) -> Self:
        """Connects (creates if not exist) a db on disk with some pragmas applied; ``timeout_ms`` as for the constructor."""
        pragmas = [
            "PRAGMA foreign_keys = ON",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA cache_size = -64000",
            "PRAGMA wal_autocheckpoint = 1000",
            "PRAGMA mmap_size = 268435456",
            "PRAGMA temp_store = MEMORY",
        ]
        return cls(path, pragmas=pragmas, timeout_ms=timeout_ms)
