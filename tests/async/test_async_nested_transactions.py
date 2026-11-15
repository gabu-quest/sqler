"""Nested async transactions of sibling tasks that share their parent's connection.

Tasks started inside ``async with db.transaction():`` share the parent's pinned
connection, and a connection has one stack of savepoints. A nested transaction one
task opens while a sibling's nested transaction is open waits until that one has
committed or rolled back; the savepoints on the connection are then opened and closed
in stack order, so each child's commit or rollback touches only its own rows.

Nesting in one task, and in a task started inside a nested transaction and awaited in
it, does not wait. Tasks are ordered with ``asyncio.Event``s; every test is bounded.

A plain statement of a sibling also waits while another sibling's nested transaction is
open. A transaction ends for every task that shares it when its ``COMMIT`` or ``ROLLBACK``
is sent: a task that outlives it is in no transaction. A transaction that ends while a
nested transaction of another task is open is rolled back, and both sides are told.
"""

import asyncio
import os
import sqlite3
import tempfile

import pytest
import pytest_asyncio
from sqler import TransactionStateError
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import LockTimeoutError, NotConnectedError, QueryError
from sqler.logging import query_logger

BOUND_S = 5.0
PRAGMAS = ["PRAGMA busy_timeout = 2000", "PRAGMA journal_mode = WAL", "PRAGMA synchronous = OFF"]
TXN_WORDS = {"BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT", "INSERT"}
# about a second of work in SQLite: a statement that is still running when the next is queued
SLOW = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT count(*) FROM c"
ENDED = "the transaction this nested transaction was part of has ended; nothing written in it was committed"


class Undo(Exception):
    pass


class Traced:
    """A database on a temporary file with two pooled connections and a statement trace."""

    def __init__(self, db: AsyncSQLerDB):
        self.db = db
        self.adapter: AsyncSQLiteAdapter = db.adapter
        self.sent: list[str] = []
        self.by_connection: list[list[str]] = []

    async def trace(self) -> None:
        for conn in self.adapter._all_conns:
            mine: list[str] = []
            self.by_connection.append(mine)
            await conn.set_trace_callback(lambda sql, mine=mine: (self.sent.append(sql), mine.append(sql)))

    def statements(self) -> list[str]:
        """The transaction statements and inserts sent, in order, without their values."""
        out = []
        for sql in self.sent:
            words = sql.split()
            if words and words[0].upper() in TXN_WORDS:
                out.append(sql.split(" VALUES")[0].strip())
        return out

    def steps(self, sent: list[str] | None = None) -> list[str]:
        """Like ``statements``, with each insert shown as ``INSERT <its value>``."""
        out = []
        for sql in self.sent if sent is None else sent:
            words = sql.split()
            if not words or words[0].upper() not in TXN_WORDS:
                continue
            out.append("INSERT " + sql.split("VALUES", 1)[1].strip(" ();'") if words[0] == "INSERT" else sql.strip())
        return out

    def steps_by_connection(self) -> list[list[str]]:
        """``steps`` of each connection that sent any, in a fixed order (sorted)."""
        return sorted(steps for steps in map(self.steps, self.by_connection) if steps)

    async def rows_in_order_written(self) -> list[str]:
        cur = await self.adapter.execute("SELECT v FROM t ORDER BY rowid")
        rows = [r[0] for r in await cur.fetchall()]
        await self.adapter.auto_commit()
        return rows

    async def rows(self) -> list[str]:
        cur = await self.adapter.execute("SELECT v FROM t ORDER BY v")
        rows = [r[0] for r in await cur.fetchall()]
        await self.adapter.auto_commit()
        return rows

    async def insert(self, value: str) -> None:
        await self.adapter.execute("INSERT INTO t (v) VALUES (?)", [value])
        await self.adapter.auto_commit()


@pytest_asyncio.fixture
async def make_db():
    made: list[AsyncSQLerDB] = []
    with tempfile.TemporaryDirectory() as tmpdir:

        async def make(acquire_timeout: float = BOUND_S) -> Traced:
            path = os.path.join(tmpdir, f"t{len(made)}.db")
            adapter = AsyncSQLiteAdapter(
                path, pragmas=list(PRAGMAS), pool_size=2, acquire_timeout=acquire_timeout
            )
            db = AsyncSQLerDB(adapter)
            made.append(db)
            await db.connect()
            await adapter.execute("CREATE TABLE t (v TEXT NOT NULL)")
            await adapter.auto_commit()
            traced = Traced(db)
            await traced.trace()
            return traced

        try:
            yield make
        finally:
            for db in made:
                await db.close()


def max_open_savepoints(statements: list[str]) -> int:
    """Replay the savepoint statements; fail on a release or rollback that is not of the top one."""
    stack: list[str] = []
    most = 0
    for sql in statements:
        words = sql.split()
        if words[0] == "SAVEPOINT":
            stack.append(words[1])
            most = max(most, len(stack))
        elif words[0] == "ROLLBACK" and len(words) > 1:
            assert stack and stack[-1] == words[-1], f"{sql} with {stack} open"
        elif words[0] == "RELEASE":
            assert stack and stack[-1] == words[-1], f"{sql} with {stack} open"
            stack.pop()
        elif words[0] in {"COMMIT", "ROLLBACK"}:
            stack.clear()
    return most


def open_at_each_end(statements: list[str]) -> list[int]:
    """Replay the statements as SQLite does: how many savepoints are open at each COMMIT or ROLLBACK."""
    stack: list[str] = []
    out = []
    for sql in statements:
        words = sql.split()
        name = words[-1]
        newest = len(stack) - 1 - stack[::-1].index(name) if name in stack else None
        if words[0] == "SAVEPOINT":
            stack.append(name)
        elif words[0] == "RELEASE" and newest is not None:
            del stack[newest:]
        elif words[0] == "ROLLBACK" and len(words) > 1 and newest is not None:
            del stack[newest + 1 :]
        elif words[0] in {"COMMIT", "ROLLBACK"}:
            out.append(len(stack))
            stack.clear()
    return out


async def turns_until(ready) -> None:
    """Give the loop one turn at a time until ``ready()`` holds; bounded."""

    async def spin() -> None:
        while not ready():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), BOUND_S)


def ending_on_top(lease) -> bool:
    """The top of the connection's stack is a nested transaction whose end is being sent."""
    return bool(lease.savepoints) and lease.savepoints[-1].task is None


async def until_a_nested_end_is_sent(lease) -> None:
    """Return in the first loop turn after a nested transaction's end was sent and the turn handed on.

    The turn is handed on in the same step that takes the ending one off the stack, and
    a waiter given its turn runs two loop turns after that at the earliest: the caller
    runs before it.
    """
    await turns_until(lambda: ending_on_top(lease))
    await turns_until(lambda: not ending_on_top(lease))


async def settle(t: Traced) -> None:
    """Wait for the background work of ended tasks (rollbacks, closing savepoints) to finish."""
    while t.adapter._reclaims:
        await asyncio.wait_for(asyncio.gather(*t.adapter._reclaims), BOUND_S)


async def assert_pool_clean(t: Traced) -> None:
    await settle(t)
    stats = t.adapter.pool_stats()
    assert (stats.total_connections, stats.available_connections) == (2, 2)
    assert [conn.in_transaction for conn in t.adapter._all_conns] == [False, False]


async def run_siblings(t: Traced, commits: list[bool]) -> None:
    """Children of one transaction each open a nested one and insert ``c<i>``; some raise.

    Child 0 opens its nested transaction first and ends it only after every later child
    has asked for one; child i asks after child i - 1 has asked.
    """
    asked = [asyncio.Event() for _ in commits]

    async def child(i: int) -> None:
        if i > 0:
            await asked[i - 1].wait()
            asked[i].set()
        try:
            async with t.db.transaction():
                await t.insert(f"c{i}")
                if i == 0:
                    asked[0].set()
                    await asked[-1].wait()
                if not commits[i]:
                    raise Undo()
        except Undo:
            pass

    async with t.db.transaction():
        await t.insert("parent")
        await asyncio.gather(*(child(i) for i in range(len(commits))))


# --- siblings that each open a nested transaction --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commits, kept",
    [
        ([True, False], ["c0", "parent"]),
        ([False, True], ["c1", "parent"]),
        ([True, True], ["c0", "c1", "parent"]),
        ([False, False], ["parent"]),
        ([True, False, True], ["c0", "c2", "parent"]),
    ],
    ids=["commit-then-raise", "raise-then-commit", "both-commit", "both-raise", "three-middle-raises"],
)
async def test_each_sibling_keeps_or_loses_only_its_own_rows(make_db, commits, kept):
    t = await make_db()
    await asyncio.wait_for(run_siblings(t, commits), BOUND_S)
    assert await t.rows() == kept
    assert max_open_savepoints(t.statements()) == 1


@pytest.mark.asyncio
async def test_siblings_open_their_savepoints_in_the_order_they_asked(make_db):
    t = await make_db()
    await asyncio.wait_for(run_siblings(t, [True, False, True]), BOUND_S)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t (v)",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "ROLLBACK TO SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]


@pytest.mark.asyncio
async def test_twenty_siblings_keep_exactly_the_rows_of_those_that_did_not_raise(make_db):
    t = await make_db()

    async def child(i: int) -> None:
        try:
            async with t.db.transaction():
                await t.insert(f"c{i:02d}")
                if i % 3 == 2:
                    raise Undo()
        except Undo:
            pass

    async def run() -> None:
        async with t.db.transaction():
            await asyncio.gather(*(child(i) for i in range(20)))

    await asyncio.wait_for(run(), BOUND_S)
    expected = [f"c{i:02d}" for i in range(20) if i % 3 != 2]
    assert len(expected) == 14
    assert await t.rows() == expected
    statements = t.statements()
    assert statements.count("SAVEPOINT sp_1") == 20
    assert max_open_savepoints(statements) == 1


# --- nesting that is not between siblings does not wait ----------------------------------


@pytest.mark.asyncio
async def test_one_task_nesting_three_deep_sends_the_statements_it_always_sent(make_db):
    t = await make_db()
    async with t.db.transaction():
        async with t.db.transaction():
            async with t.db.transaction():
                await t.insert("deep")
            try:
                async with t.db.transaction():
                    await t.insert("undone")
                    raise Undo()
            except Undo:
                pass
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "SAVEPOINT sp_2",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_2",
        "SAVEPOINT sp_2",
        "INSERT INTO t (v)",
        "ROLLBACK TO SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["deep"]


@pytest.mark.asyncio
async def test_a_task_started_inside_a_nested_transaction_nests_inside_it(make_db):
    t = await make_db()

    async def grandchild() -> None:
        async with t.db.transaction():
            await t.insert("grandchild")

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("child")
            await asyncio.ensure_future(grandchild())

    async def run() -> None:
        async with t.db.transaction():
            await asyncio.ensure_future(child())

    await asyncio.wait_for(run(), BOUND_S)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "SAVEPOINT sp_2",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["child", "grandchild"]


@pytest.mark.asyncio
async def test_one_child_sends_the_statements_it_always_sent(make_db):
    t = await make_db()

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("kept")

    with query_logger.capture() as captured:
        async with t.db.transaction():
            await asyncio.gather(child())
    assert [q.sql for q in captured] == ["INSERT INTO t (v) VALUES (?)"]
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]


@pytest.mark.asyncio
async def test_two_top_level_transactions_in_two_tasks_use_two_connections(make_db):
    t = await make_db()
    a_in = asyncio.Event()
    conns: dict[str, object] = {}

    async def a() -> None:
        async with t.db.transaction():
            conns["a"] = t.adapter.connection
            await t.insert("a")
            a_in.set()

    async def b() -> None:
        await a_in.wait()
        async with t.db.transaction():
            conns["b"] = t.adapter.connection
            await t.insert("b")

    await asyncio.wait_for(asyncio.gather(a(), b()), BOUND_S)
    assert conns["a"] is not conns["b"]
    # b's BEGIN may start while a's commit is on its way: the order between them is SQLite's
    assert sorted(t.statements()) == ["BEGIN IMMEDIATE"] * 2 + ["COMMIT"] * 2 + ["INSERT INTO t (v)"] * 2
    assert await t.rows() == ["a", "b"]


# --- the wait ends ----------------------------------------------------------------------


async def assert_pool_whole_and_usable(t: Traced) -> None:
    stats = t.adapter.pool_stats()
    assert (stats.total_connections, stats.available_connections) == (2, 2)
    async with t.db.transaction():
        await t.insert("after")


@pytest.mark.asyncio
async def test_a_sibling_cancelled_while_it_waits_leaves_the_next_waiter_its_turn(make_db):
    t = await make_db()
    a_in, b_asked, c_asked, a_go = (asyncio.Event() for _ in range(4))

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await a_go.wait()

    async def b() -> None:
        await a_in.wait()
        b_asked.set()
        async with t.db.transaction():
            await t.insert("b")

    async def c() -> None:
        await b_asked.wait()
        c_asked.set()
        async with t.db.transaction():
            await t.insert("c")

    async def run() -> None:
        async with t.db.transaction():
            ta, tb, tc = (asyncio.ensure_future(f()) for f in (a, b, c))
            await c_asked.wait()  # b and c are waiting for a
            tb.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tb
            a_go.set()
            await asyncio.gather(ta, tc)

    await asyncio.wait_for(run(), BOUND_S)
    assert await t.rows() == ["a", "c"]
    assert max_open_savepoints(t.statements()) == 1
    await assert_pool_whole_and_usable(t)


@pytest.mark.asyncio
async def test_a_sibling_cancelled_while_it_holds_lets_the_waiter_in(make_db):
    t = await make_db()
    a_in, b_asked, never = (asyncio.Event() for _ in range(3))

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await never.wait()

    async def b() -> None:
        await a_in.wait()
        b_asked.set()
        async with t.db.transaction():
            await t.insert("b")

    async def run() -> None:
        async with t.db.transaction():
            ta, tb = asyncio.ensure_future(a()), asyncio.ensure_future(b())
            await b_asked.wait()
            ta.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ta
            await tb

    await asyncio.wait_for(run(), BOUND_S)
    assert await t.rows() == ["b"]
    assert max_open_savepoints(t.statements()) == 1
    await assert_pool_whole_and_usable(t)


@pytest.mark.asyncio
async def test_the_parent_cancelled_while_one_holds_and_one_waits_leaves_the_pool_whole(make_db):
    t = await make_db()
    a_in, b_asked, never = (asyncio.Event() for _ in range(3))

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await never.wait()

    async def b() -> None:
        await a_in.wait()
        b_asked.set()
        async with t.db.transaction():
            await t.insert("b")

    async def parent() -> None:
        await t.insert("before")
        async with t.db.transaction():
            await t.insert("parent")
            await asyncio.gather(a(), b())

    task = asyncio.ensure_future(parent())
    await asyncio.wait_for(b_asked.wait(), BOUND_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, BOUND_S)
    if t.adapter._reclaims:  # a connection left by an ended task is rolled back in the background
        await asyncio.wait_for(asyncio.gather(*t.adapter._reclaims), BOUND_S)
    assert await t.rows() == ["before"]
    await assert_pool_whole_and_usable(t)
    assert await t.rows() == ["after", "before"]


@pytest.mark.asyncio
async def test_a_task_inside_a_nested_transaction_awaiting_a_sibling_that_wants_one_raises(make_db):
    t = await make_db(acquire_timeout=0.3)
    h_in = asyncio.Event()

    async def sibling() -> None:
        await h_in.wait()
        async with t.db.transaction():
            await t.insert("sibling")

    async def holder(s: asyncio.Future) -> None:
        async with t.db.transaction():
            await t.insert("holder")
            h_in.set()
            await s

    async def run() -> None:
        async with t.db.transaction():
            s = asyncio.ensure_future(sibling())
            await asyncio.ensure_future(holder(s))

    with pytest.raises(LockTimeoutError, match="can never end"):
        await asyncio.wait_for(run(), BOUND_S)
    assert await t.rows() == []
    await assert_pool_whole_and_usable(t)


# --- a task that outlives the transaction it was started in is outside it ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_raises", [False, True], ids=["after-commit", "after-rollback"])
async def test_a_plain_write_of_a_task_that_outlived_its_transaction_is_committed(make_db, parent_raises):
    t = await make_db()
    go = asyncio.Event()

    async def late() -> bool:
        await go.wait()
        await t.insert("late")
        return t.adapter.in_transaction

    try:
        async with t.db.transaction():
            await t.insert("parent")
            task = asyncio.ensure_future(late())
            if parent_raises:
                raise Undo()
    except Undo:
        pass
    go.set()
    assert await asyncio.wait_for(task, BOUND_S) is False
    await assert_pool_clean(t)
    end = "ROLLBACK" if parent_raises else "COMMIT"
    assert t.statements() == ["BEGIN IMMEDIATE", "INSERT INTO t (v)", end, "BEGIN", "INSERT INTO t (v)", "COMMIT"]
    assert await t.rows() == (["late"] if parent_raises else ["late", "parent"])


@pytest.mark.asyncio
async def test_a_task_that_outlived_its_transaction_begins_its_own(make_db):
    t = await make_db()
    go = asyncio.Event()

    async def late() -> bool:
        await go.wait()
        try:
            async with t.db.transaction():
                await t.insert("own, rolled back")
                raise Undo()
        except Undo:
            pass
        async with t.db.transaction():
            await t.insert("own, committed")
        return t.adapter.in_transaction

    async with t.db.transaction():
        task = asyncio.ensure_future(late())
    go.set()
    assert await asyncio.wait_for(task, BOUND_S) is False
    await assert_pool_clean(t)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "COMMIT",
        "BEGIN IMMEDIATE",
        "INSERT INTO t (v)",
        "ROLLBACK",
        "BEGIN IMMEDIATE",
        "INSERT INTO t (v)",
        "COMMIT",
    ]
    assert await t.rows() == ["own, committed"]


@pytest.mark.asyncio
async def test_a_task_started_in_a_nested_transaction_that_outlived_the_transaction_commits_alone(make_db):
    t = await make_db()
    go = asyncio.Event()

    async def late() -> bool:
        await go.wait()
        await t.insert("late")
        return t.adapter.in_transaction

    async with t.db.transaction():
        async with t.db.transaction():
            task = asyncio.ensure_future(late())
    go.set()
    assert await asyncio.wait_for(task, BOUND_S) is False
    await assert_pool_clean(t)
    assert await t.rows() == ["late"]


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [0, 1, 2, 3, 4])
async def test_a_sibling_writing_in_the_loop_turn_the_parent_exits_is_never_left_uncommitted(make_db, turns):
    """The parent's block ends in the step that lets the sibling go: the transaction has
    ended before the sibling runs, whatever the turn count, so the sibling's row is
    committed alone on the other pooled connection.

    The two connections run in their own threads, so only the order of each one's
    statements is fixed, not how the two interleave.
    """
    t = await make_db()
    go = asyncio.Event()

    async def sibling() -> bool:
        await go.wait()
        for _ in range(turns):
            await asyncio.sleep(0)
        await t.insert("sibling")
        return t.adapter.in_transaction

    async with t.db.transaction():
        await t.insert("parent")
        task = asyncio.ensure_future(sibling())
        await asyncio.sleep(0)
        go.set()
    assert await asyncio.wait_for(task, BOUND_S) is False
    await assert_pool_clean(t)
    assert t.steps_by_connection() == [
        ["BEGIN", "INSERT sibling", "COMMIT"],
        ["BEGIN IMMEDIATE", "INSERT parent", "COMMIT"],
    ]
    assert await t.rows() == ["parent", "sibling"]


# --- a sibling's plain statement waits while another sibling's nested transaction is open --


@pytest.mark.asyncio
async def test_a_sibling_plain_write_waits_for_a_nested_transaction_that_rolls_back(make_db):
    t = await make_db()
    a_in, b_started = asyncio.Event(), asyncio.Event()

    async def a() -> None:
        try:
            async with t.db.transaction():
                await t.insert("a")
                a_in.set()
                await b_started.wait()
                for _ in range(20):
                    await asyncio.sleep(0)
                raise Undo()
        except Undo:
            pass

    async def b() -> None:
        await a_in.wait()
        b_started.set()
        await t.insert("b-plain")

    async with t.db.transaction():
        await asyncio.wait_for(asyncio.gather(a(), b()), BOUND_S)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "ROLLBACK TO SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "COMMIT",
    ]
    assert await t.rows() == ["b-plain"]


@pytest.mark.asyncio
async def test_twenty_plain_siblings_keep_their_rows_while_one_nested_sibling_rolls_back(make_db):
    t = await make_db()
    held = asyncio.Event()
    started = [asyncio.Event() for _ in range(20)]

    async def holder() -> None:
        try:
            async with t.db.transaction():
                await t.insert("nested")
                held.set()
                for event in started:
                    await event.wait()
                for _ in range(20):
                    await asyncio.sleep(0)
                raise Undo()
        except Undo:
            pass

    async def plain(i: int) -> None:
        await held.wait()
        started[i].set()
        await t.insert(f"p{i:02d}")

    async with t.db.transaction():
        await asyncio.wait_for(asyncio.gather(holder(), *(plain(i) for i in range(20))), BOUND_S)
    assert await t.rows() == [f"p{i:02d}" for i in range(20)]
    statements = t.statements()
    assert statements[:5] == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "ROLLBACK TO SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
    ]
    assert statements[5:] == ["INSERT INTO t (v)"] * 20 + ["COMMIT"]


@pytest.mark.asyncio
async def test_a_sibling_executemany_waits_for_a_nested_transaction_that_rolls_back(make_db):
    t = await make_db()
    a_in, b_started = asyncio.Event(), asyncio.Event()

    async def a() -> None:
        try:
            async with t.db.transaction():
                await t.insert("a")
                a_in.set()
                await b_started.wait()
                for _ in range(20):
                    await asyncio.sleep(0)
                raise Undo()
        except Undo:
            pass

    async def b() -> None:
        await a_in.wait()
        b_started.set()
        await t.adapter.executemany("INSERT INTO t (v) VALUES (?)", [["m1"], ["m2"]])

    async with t.db.transaction():
        await asyncio.wait_for(asyncio.gather(a(), b()), BOUND_S)
    assert await t.rows() == ["m1", "m2"]


@pytest.mark.asyncio
async def test_a_sibling_executescript_during_a_nested_transaction_raises_query_error(make_db):
    t = await make_db()
    a_in = asyncio.Event()
    raised: list[BaseException] = []

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            for _ in range(5):
                await asyncio.sleep(0)

    async def b() -> None:
        await a_in.wait()
        try:
            await t.adapter.executescript("INSERT INTO t (v) VALUES ('script');")
        except QueryError as exc:
            raised.append(exc)

    async with t.db.transaction():
        await asyncio.wait_for(asyncio.gather(a(), b()), BOUND_S)
    assert [str(e).split(":")[0] for e in raised] == ["executescript cannot run inside a transaction"]
    assert await t.rows() == ["a"]


@pytest.mark.asyncio
async def test_a_task_whose_savepoint_ended_waits_for_a_sibling_nested_transaction(make_db):
    """A task started inside a nested transaction that has since ended is directly in the
    transaction: it waits while a sibling's nested transaction is open."""
    t = await make_db()
    s_ended, x_in, late_asked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def late() -> None:
        await x_in.wait()
        late_asked.set()
        async with t.db.transaction():
            await t.insert("late")

    async def x() -> None:
        await s_ended.wait()
        try:
            async with t.db.transaction():
                await t.insert("x")
                x_in.set()
                await late_asked.wait()
                for _ in range(20):
                    await asyncio.sleep(0)
                raise Undo()
        except Undo:
            pass

    async with t.db.transaction():
        tx = asyncio.ensure_future(x())
        async with t.db.transaction():
            tl = asyncio.ensure_future(late())
        s_ended.set()
        await asyncio.wait_for(asyncio.gather(tx, tl), BOUND_S)
    assert await t.rows() == ["late"]
    assert max_open_savepoints(t.statements()) == 1


@pytest.mark.asyncio
async def test_a_task_in_a_nested_transaction_awaiting_a_sibling_plain_write_raises(make_db):
    t = await make_db(acquire_timeout=0.3)
    h_in = asyncio.Event()

    async def sibling() -> None:
        await h_in.wait()
        await t.insert("sibling")

    async def run() -> None:
        async with t.db.transaction():
            s = asyncio.ensure_future(sibling())
            async with t.db.transaction():
                await t.insert("holder")
                h_in.set()
                await s

    with pytest.raises(LockTimeoutError) as raised:
        await asyncio.wait_for(run(), BOUND_S)
    assert "is still running" in str(raised.value)
    assert "can never end" in str(raised.value)
    assert await t.rows() == []
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_plain_waiter_with_no_timeout_goes_when_the_holder_ends(make_db):
    t = await make_db(acquire_timeout=None)
    a_in, b_started = asyncio.Event(), asyncio.Event()

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await b_started.wait()
            for _ in range(5):
                await asyncio.sleep(0)

    async def b() -> None:
        await a_in.wait()
        b_started.set()
        await t.insert("b")

    async with t.db.transaction():
        await asyncio.wait_for(asyncio.gather(a(), b()), BOUND_S)
    assert await t.rows() == ["a", "b"]
    assert t.statements()[-3:] == ["RELEASE SAVEPOINT sp_1", "INSERT INTO t (v)", "COMMIT"]


@pytest.mark.asyncio
async def test_a_plain_waiter_cancelled_leaves_the_next_waiter_its_turn(make_db):
    t = await make_db()
    a_in, b_asked, c_asked, a_go = (asyncio.Event() for _ in range(4))

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await a_go.wait()

    async def b() -> None:
        await a_in.wait()
        b_asked.set()
        await t.insert("b")

    async def c() -> None:
        await b_asked.wait()
        c_asked.set()
        await t.insert("c")

    async def run() -> None:
        async with t.db.transaction():
            ta, tb, tc = (asyncio.ensure_future(f()) for f in (a, b, c))
            await c_asked.wait()
            tb.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tb
            a_go.set()
            await asyncio.gather(ta, tc)

    await asyncio.wait_for(run(), BOUND_S)
    assert await t.rows() == ["a", "c"]
    await assert_pool_clean(t)


# --- a transaction that ends under an open nested transaction of another task -------------


@pytest.mark.asyncio
@pytest.mark.parametrize("child_then", ["writes", "ends", "raises"])
async def test_the_parent_committing_under_a_child_nested_transaction_rolls_back_and_tells_both(make_db, child_then):
    t = await make_db()
    await t.insert("before")
    opened, finish = asyncio.Event(), asyncio.Event()

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("child, open at the commit")
            opened.set()
            await finish.wait()
            if child_then == "writes":
                await t.insert("child, after the commit")
            elif child_then == "raises":
                raise Undo()

    with pytest.raises(TransactionStateError) as parent_raised:
        async with t.db.transaction():
            await t.insert("parent")
            task = asyncio.ensure_future(child())
            await opened.wait()
    assert str(parent_raised.value) == (
        "the transaction was rolled back: 1 nested transaction of another task was still open when it "
        "ended; end them before the block ends (await the tasks inside the block, or use a TaskGroup)"
    )
    finish.set()
    if child_then == "raises":
        with pytest.raises(Undo) as child_raised:
            await asyncio.wait_for(task, BOUND_S)
        assert child_raised.value.__notes__ == [ENDED]
    else:
        with pytest.raises(TransactionStateError, match=ENDED):
            await asyncio.wait_for(task, BOUND_S)
    await assert_pool_clean(t)
    assert await t.rows() == ["before"]
    assert t.statements()[-1] == "ROLLBACK"


@pytest.mark.asyncio
async def test_the_parent_raising_under_a_child_nested_transaction_keeps_its_own_exception(make_db):
    t = await make_db()
    opened, finish = asyncio.Event(), asyncio.Event()

    class ParentFailed(Exception):
        pass

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("child")
            opened.set()
            await finish.wait()
            raise Undo()

    with pytest.raises(ParentFailed) as parent_raised:
        async with t.db.transaction():
            await t.insert("parent")
            task = asyncio.ensure_future(child())
            await opened.wait()
            raise ParentFailed("its own")
    assert str(parent_raised.value) == "its own"
    assert getattr(parent_raised.value, "__notes__", None) is None
    finish.set()
    with pytest.raises(Undo) as child_raised:
        await asyncio.wait_for(task, BOUND_S)
    assert child_raised.value.__notes__ == [ENDED]
    await assert_pool_clean(t)
    assert await t.rows() == []


@pytest.mark.asyncio
async def test_a_nested_transaction_waiting_when_the_transaction_ends_opens_nothing(make_db):
    t = await make_db()
    a_in, b_asked, never = (asyncio.Event() for _ in range(3))

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await never.wait()

    async def b() -> None:
        await a_in.wait()
        b_asked.set()
        async with t.db.transaction():
            await t.insert("b")

    with pytest.raises(TransactionStateError, match="1 nested transaction of another task"):
        async with t.db.transaction():
            ta, tb = asyncio.ensure_future(a()), asyncio.ensure_future(b())
            await b_asked.wait()
    with pytest.raises(TransactionStateError, match=ENDED):
        await asyncio.wait_for(tb, BOUND_S)
    ta.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(ta, BOUND_S)
    await assert_pool_clean(t)
    assert t.statements() == ["BEGIN IMMEDIATE", "SAVEPOINT sp_1", "INSERT INTO t (v)", "ROLLBACK"]
    assert await t.rows() == []


@pytest.mark.asyncio
async def test_a_child_nested_transaction_open_at_close_raises_at_its_next_statement(make_db):
    t = await make_db()
    opened, finish = asyncio.Event(), asyncio.Event()

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("child")
            opened.set()
            await finish.wait()
            await t.insert("after close")

    async def parent() -> None:
        async with t.db.transaction():
            task = asyncio.ensure_future(child())
            await opened.wait()
            await t.db.close()
            finish.set()
            with pytest.raises(TransactionStateError, match=ENDED):
                await asyncio.wait_for(task, BOUND_S)

    await asyncio.wait_for(parent(), BOUND_S)
    assert t.adapter.pool_stats().total_connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("waiter", ["plain", "nested"])
async def test_a_waiter_woken_by_close_raises_not_connected(make_db, waiter):
    t = await make_db()
    a_in, b_asked, never = (asyncio.Event() for _ in range(3))

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await never.wait()

    async def b() -> None:
        await a_in.wait()
        b_asked.set()
        if waiter == "plain":
            await t.insert("b")
        else:
            async with t.db.transaction():
                await t.insert("b")

    async def parent() -> None:
        async with t.db.transaction():
            ta, tb = asyncio.ensure_future(a()), asyncio.ensure_future(b())
            await b_asked.wait()
            await t.db.close()
            with pytest.raises(NotConnectedError, match="closed"):
                await asyncio.wait_for(tb, BOUND_S)
            ta.cancel()
            await asyncio.gather(ta, return_exceptions=True)

    await asyncio.wait_for(parent(), BOUND_S)


# --- a task that ends with its nested transaction open -----------------------------------


@pytest.mark.asyncio
async def test_a_task_that_ends_with_its_nested_transaction_open_rolls_it_back(make_db):
    t = await make_db(acquire_timeout=1.0)
    a_in, b_asked = asyncio.Event(), asyncio.Event()

    async def a() -> None:  # begins a nested transaction and never ends it
        await t.adapter.begin_transaction()
        await t.insert("a")
        a_in.set()
        await b_asked.wait()

    async def b() -> None:
        await a_in.wait()
        b_asked.set()
        async with t.db.transaction():
            await t.insert("b")

    async with t.db.transaction():
        await t.insert("parent")
        await asyncio.wait_for(asyncio.gather(a(), b()), BOUND_S)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t (v)",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "ROLLBACK TO SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["b", "parent"]
    await assert_pool_clean(t)


# --- cancellation keeps the adapter's savepoint stack equal to SQLite's -------------------


async def nested_rollback_then_plain_write(t: Traced) -> None:
    try:
        async with t.db.transaction():
            await t.insert("undone")
            raise Undo()
    except Undo:
        pass
    await t.insert("kept")


@pytest.mark.asyncio
async def test_a_task_cancelled_while_its_savepoint_is_in_flight(make_db):
    t = await make_db()

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("never")

    async def run() -> None:
        async with t.db.transaction():
            slow = asyncio.ensure_future(t.adapter.execute(SLOW))
            await asyncio.sleep(0)  # the slow statement is on the connection
            task = asyncio.ensure_future(child())
            await asyncio.sleep(0)  # its SAVEPOINT is queued behind the slow statement
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await (await slow).close()
            await nested_rollback_then_plain_write(t)

    await asyncio.wait_for(run(), BOUND_S * 2)
    assert await t.rows() == ["kept"]
    assert open_at_each_end(t.statements()) == [0]
    await assert_pool_clean(t)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_raises", [False, True], ids=["release", "rollback-to"])
async def test_a_task_cancelled_while_its_savepoint_end_is_in_flight(make_db, child_raises):
    t = await make_db()
    ending = asyncio.Event()
    slow: list[asyncio.Future] = []

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("child")
            slow.append(asyncio.ensure_future(t.adapter.execute(SLOW)))
            await asyncio.sleep(0)  # the slow statement is on the connection
            ending.set()
            if child_raises:
                raise Undo()
        # the block's end is queued behind the slow statement

    async def run() -> None:
        async with t.db.transaction():
            task = asyncio.ensure_future(child())
            await ending.wait()
            for _ in range(3):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await (await slow[0]).close()
            await nested_rollback_then_plain_write(t)

    await asyncio.wait_for(run(), BOUND_S * 2)
    assert await t.rows() == (["kept"] if child_raises else ["child", "kept"])
    assert open_at_each_end(t.statements()) == [0]
    await assert_pool_clean(t)


# --- the wait ends: timeouts and grandchildren -------------------------------------------


@pytest.mark.asyncio
async def test_a_nested_waiter_that_timed_out_leaves_the_queue_clean(make_db):
    t = await make_db(acquire_timeout=0.3)
    a_in, b_out, a_go = (asyncio.Event() for _ in range(3))

    async def a() -> None:
        async with t.db.transaction():
            await t.insert("a")
            a_in.set()
            await a_go.wait()

    async def b() -> None:
        await a_in.wait()
        try:
            async with t.db.transaction():
                await t.insert("b")
        finally:
            b_out.set()

    async def c() -> None:
        await b_out.wait()
        async with t.db.transaction():
            await t.insert("c")

    async def run() -> None:
        async with t.db.transaction():
            ta, tb, tc = (asyncio.ensure_future(f()) for f in (a, b, c))
            with pytest.raises(LockTimeoutError):
                await tb
            for _ in range(3):
                await asyncio.sleep(0)  # c has asked
            a_go.set()
            await asyncio.gather(ta, tc)

    await asyncio.wait_for(run(), BOUND_S)
    assert await t.rows() == ["a", "c"]
    assert max_open_savepoints(t.statements()) == 1
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_grandchild_holding_with_a_sibling_waiting_behind_it(make_db):
    t = await make_db()
    g_in, b_asked = asyncio.Event(), asyncio.Event()

    async def grandchild() -> None:
        async with t.db.transaction():
            await t.insert("grandchild")
            g_in.set()
            await b_asked.wait()
            for _ in range(5):
                await asyncio.sleep(0)

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("child")
            await asyncio.ensure_future(grandchild())

    async def b() -> None:
        await g_in.wait()
        b_asked.set()
        async with t.db.transaction():
            await t.insert("sibling")

    async with t.db.transaction():
        await asyncio.wait_for(asyncio.gather(child(), b()), BOUND_S)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "SAVEPOINT sp_2",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["child", "grandchild", "sibling"]


# --- a statement given its turn is sent before another task's nested transaction opens ----

BROKEN = (
    "a nested transaction could not be {}, so the transaction it is part of ends rolled back: "
    "nothing written in it is committed"
)


async def plain_waiter_then_ready_opener(t: Traced, turns: int, xs: list[str], *, many: bool, y_commits: bool):
    """The parent's nested transaction is open while X sends a plain statement for each of
    ``xs`` (each from its own task, in that order, or all in one ``executemany``), so they
    wait. Y is ready on every loop turn and opens a nested transaction of its own ``turns``
    loop turns after the parent's has ended (the step that hands X its turn), writes ``y``
    and commits or rolls back."""
    nested_open = asyncio.Event()
    asked = [asyncio.Event() for _ in xs]

    async def x(i: int) -> None:
        await (asked[i - 1] if i else nested_open).wait()
        asked[i].set()
        if many:
            await t.adapter.executemany("INSERT INTO t (v) VALUES (?)", [[v] for v in xs])
        else:
            await t.adapter.execute("INSERT INTO t (v) VALUES (?)", [xs[i]])
        await t.adapter.auto_commit()

    async def y() -> None:
        await nested_open.wait()
        await until_a_nested_end_is_sent(t.adapter._task_lease.get())
        for _ in range(turns):
            await asyncio.sleep(0)
        try:
            async with t.db.transaction():
                await t.insert("y")
                if not y_commits:
                    raise Undo()
        except Undo:
            pass

    async with t.db.transaction():
        tasks = [asyncio.ensure_future(x(i)) for i in range(1 if many else len(xs))]
        ty = asyncio.ensure_future(y())
        async with t.db.transaction():
            await t.insert("parent nested")
            nested_open.set()
            await asked[len(tasks) - 1].wait()  # every X is waiting for this block to end
        await asyncio.wait_for(asyncio.gather(*tasks, ty), BOUND_S)


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [0, 1, 2, 3])
@pytest.mark.parametrize(
    "many, y_commits", [(False, False), (True, False), (False, True)], ids=["execute", "executemany", "y-commits"]
)
async def test_a_plain_statement_given_its_turn_is_sent_before_a_ready_sibling_opens_a_nested_transaction(
    make_db, turns, many, y_commits
):
    t = await make_db()
    xs = ["x1", "x2"] if many else ["x"]
    await plain_waiter_then_ready_opener(t, turns, xs, many=many, y_commits=y_commits)
    assert await t.rows() == ["parent nested", *xs, *(["y"] if y_commits else [])]
    y_end = ["RELEASE SAVEPOINT sp_1"] if y_commits else ["ROLLBACK TO SAVEPOINT sp_1", "RELEASE SAVEPOINT sp_1"]
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT parent nested",
        "RELEASE SAVEPOINT sp_1",
        *(f"INSERT {v}" for v in xs),
        "SAVEPOINT sp_1",
        "INSERT y",
        *y_end,
        "COMMIT",
    ]
    await assert_pool_clean(t)


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [0, 1, 2, 3])
async def test_two_plain_statements_given_their_turn_together_are_both_sent_before_a_sibling_opens(make_db, turns):
    t = await make_db()
    await plain_waiter_then_ready_opener(t, turns, ["x1", "x2"], many=False, y_commits=False)
    assert await t.rows() == ["parent nested", "x1", "x2"]
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT parent nested",
        "RELEASE SAVEPOINT sp_1",
        "INSERT x1",
        "INSERT x2",
        "SAVEPOINT sp_1",
        "INSERT y",
        "ROLLBACK TO SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]


class HeldNested:
    """A child task holding a nested transaction (it writes ``h``) until ``end`` is set; it then commits it."""

    def __init__(self, t: Traced):
        self.t = t
        self.inside, self.end = asyncio.Event(), asyncio.Event()

    async def run(self) -> None:
        async with self.t.db.transaction():
            await self.t.insert("h")
            self.inside.set()
            await self.end.wait()


async def nested_child(t: Traced, value: str, start: asyncio.Event, asked: asyncio.Event) -> None:
    await start.wait()
    asked.set()
    async with t.db.transaction():
        await t.insert(value)


async def plain_child(t: Traced, value: str, start: asyncio.Event, asked: asyncio.Event) -> bool:
    await start.wait()
    asked.set()
    await t.insert(value)
    return t.adapter.in_transaction


@pytest.mark.asyncio
async def test_a_plain_waiter_cancelled_after_its_turn_and_before_it_runs_lets_the_next_one_go(make_db):
    t = await make_db()
    h = HeldNested(t)
    x_asked, x2_asked = asyncio.Event(), asyncio.Event()

    async with t.db.transaction():
        lease = t.adapter._task_lease.get()
        th = asyncio.ensure_future(h.run())
        await h.inside.wait()
        tx = asyncio.ensure_future(plain_child(t, "x", h.inside, x_asked))
        tx2 = asyncio.ensure_future(nested_child(t, "x2", x_asked, x2_asked))
        await x2_asked.wait()
        h.end.set()
        await until_a_nested_end_is_sent(lease)  # x has its turn and has not run yet
        tx.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tx
        await asyncio.wait_for(asyncio.gather(th, tx2), BOUND_S)
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT h",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT x2",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["h", "x2"]
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_plain_waiter_that_timed_out_leaves_a_nested_transaction_behind_it_its_turn(make_db):
    t = await make_db(acquire_timeout=0.2)
    h = HeldNested(t)
    x_out, x2_asked = asyncio.Event(), asyncio.Event()

    async def x() -> None:
        try:
            await t.insert("x")
        finally:
            x_out.set()

    async def x2() -> None:
        t.adapter.acquire_timeout = BOUND_S  # x2 asks after x, with a longer bound
        x2_asked.set()
        async with t.db.transaction():
            await t.insert("x2")

    async with t.db.transaction():
        th = asyncio.ensure_future(h.run())
        await h.inside.wait()
        tx = asyncio.ensure_future(x())
        await asyncio.sleep(0)  # x is waiting
        tx2 = asyncio.ensure_future(x2())
        await x2_asked.wait()
        with pytest.raises(LockTimeoutError):
            await tx
        h.end.set()
        await asyncio.wait_for(asyncio.gather(th, tx2), BOUND_S)
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT h",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT x2",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["h", "x2"]
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_close_between_a_plain_waiter_turn_and_its_run_raises_not_connected_and_sends_nothing(make_db):
    t = await make_db()
    h = HeldNested(t)
    x_asked = asyncio.Event()

    async with t.db.transaction():
        lease = t.adapter._task_lease.get()
        th = asyncio.ensure_future(h.run())
        await h.inside.wait()
        tx = asyncio.ensure_future(plain_child(t, "x", h.inside, x_asked))
        await x_asked.wait()
        h.end.set()
        await until_a_nested_end_is_sent(lease)  # x has its turn and has not run yet
        await t.db.close()
        with pytest.raises(NotConnectedError, match="closed"):
            await asyncio.wait_for(tx, BOUND_S)
        await asyncio.wait_for(th, BOUND_S)
    assert t.steps() == ["BEGIN IMMEDIATE", "SAVEPOINT sp_1", "INSERT h", "RELEASE SAVEPOINT sp_1"]
    assert t.adapter.pool_stats().total_connections == 0


@pytest.mark.asyncio
async def test_a_nested_transaction_given_its_turn_and_not_yet_opened_when_the_parent_ends_opens_nothing(make_db):
    """A nested transaction that has been given its turn counts as open: the parent's
    commit becomes a rollback and raises, and the waiter raises and sends no SAVEPOINT."""
    t = await make_db()
    h = HeldNested(t)
    b_asked = asyncio.Event()

    with pytest.raises(TransactionStateError, match="1 nested transaction of another task"):
        async with t.db.transaction():
            lease = t.adapter._task_lease.get()
            th = asyncio.ensure_future(h.run())
            await h.inside.wait()
            tb = asyncio.ensure_future(nested_child(t, "b", h.inside, b_asked))
            await b_asked.wait()
            h.end.set()
            await until_a_nested_end_is_sent(lease)  # b has its turn and has not run yet
    with pytest.raises(TransactionStateError, match=ENDED):
        await asyncio.wait_for(tb, BOUND_S)
    await asyncio.wait_for(th, BOUND_S)
    assert t.steps() == ["BEGIN IMMEDIATE", "SAVEPOINT sp_1", "INSERT h", "RELEASE SAVEPOINT sp_1", "ROLLBACK"]
    assert await t.rows() == []
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_plain_waiter_given_its_turn_that_runs_after_the_transaction_ended_commits_alone(make_db):
    t = await make_db()
    h = HeldNested(t)
    x_asked = asyncio.Event()

    async with t.db.transaction():
        lease = t.adapter._task_lease.get()
        th = asyncio.ensure_future(h.run())
        await h.inside.wait()
        tx = asyncio.ensure_future(plain_child(t, "x", h.inside, x_asked))
        await x_asked.wait()
        h.end.set()
        await until_a_nested_end_is_sent(lease)  # x has its turn and has not run yet
    assert await asyncio.wait_for(tx, BOUND_S) is False
    await asyncio.wait_for(th, BOUND_S)
    await assert_pool_clean(t)
    assert t.steps_by_connection() == [
        ["BEGIN", "INSERT x", "COMMIT"],
        ["BEGIN IMMEDIATE", "SAVEPOINT sp_1", "INSERT h", "RELEASE SAVEPOINT sp_1", "COMMIT"],
    ]
    assert await t.rows() == ["h", "x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_commits", [True, False], ids=["owner-commits", "owner-rolls-back"])
async def test_a_plain_statement_whose_nested_transaction_ended_before_it_ran_is_sent_in_the_enclosing_one(
    make_db, owner_commits
):
    """X was started inside the owner's nested transaction S and waits behind a grandchild's
    nested transaction in S. When that ends X has its turn in S, but S ends before X runs:
    X's place is now the transaction, and its row is written there, after S's end."""
    t = await make_db()
    x_asked = asyncio.Event()

    async def grandchild() -> None:
        async with t.db.transaction():
            await t.insert("g")
            await x_asked.wait()

    async def x() -> None:  # runs after the grandchild has opened its nested transaction
        x_asked.set()
        await t.insert("x")

    async with t.db.transaction():
        lease = t.adapter._task_lease.get()
        try:
            async with t.db.transaction():
                tg = asyncio.ensure_future(grandchild())
                tx = asyncio.ensure_future(x())
                await until_a_nested_end_is_sent(lease)  # x has its turn in S and has not run yet
                if not owner_commits:
                    raise Undo()
        except Undo:
            pass
        await asyncio.wait_for(asyncio.gather(tg, tx), BOUND_S)
    s_end = ["RELEASE SAVEPOINT sp_1"] if owner_commits else ["ROLLBACK TO SAVEPOINT sp_1", "RELEASE SAVEPOINT sp_1"]
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "SAVEPOINT sp_2",
        "INSERT g",
        "RELEASE SAVEPOINT sp_2",
        *s_end,
        "INSERT x",
        "COMMIT",
    ]
    assert await t.rows() == (["g", "x"] if owner_commits else ["x"])
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_twenty_plain_waiters_and_three_nested_ones_go_in_the_order_they_asked(make_db):
    t = await make_db()
    h = HeldNested(t)
    kinds = ["plain"] * 23
    for i in (3, 11, 18):
        kinds[i] = "nested"
    labels, plain_count, nested_count = [], 0, 0
    for kind in kinds:
        if kind == "plain":
            labels.append(f"p{plain_count:02d}")
            plain_count += 1
        else:
            labels.append(f"n{nested_count}")
            nested_count += 1
    assert (plain_count, nested_count) == (20, 3)
    asked = [asyncio.Event() for _ in kinds]

    async def waiter(i: int) -> None:
        await (asked[i - 1] if i else h.inside).wait()
        asked[i].set()
        if kinds[i] == "plain":
            await t.insert(labels[i])
            return
        try:
            async with t.db.transaction():
                await t.insert(labels[i])
                if labels[i] == "n1":
                    raise Undo()
        except Undo:
            pass

    async def holder() -> None:
        try:
            async with t.db.transaction():
                await t.insert("h")
                h.inside.set()
                await asked[-1].wait()
                raise Undo()
        except Undo:
            pass

    async with t.db.transaction():
        await asyncio.wait_for(asyncio.gather(holder(), *(waiter(i) for i in range(len(kinds)))), BOUND_S)
    assert await t.rows_in_order_written() == [label for label in labels if label != "n1"]
    statements = t.statements()
    assert statements.count("SAVEPOINT sp_1") == 4
    assert max_open_savepoints(statements) == 1
    assert open_at_each_end(statements) == [0]
    await assert_pool_clean(t)


# --- a savepoint statement that fails ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("caught", [False, True], ids=["raised-on", "caught-in-the-block"])
async def test_a_nested_commit_whose_release_fails_commits_nothing_and_tells_both(make_db, caught):
    t = await make_db()
    await t.insert("before")
    with pytest.raises(TransactionStateError) as raised:
        async with t.db.transaction():
            await t.insert("parent")
            try:
                async with t.db.transaction():
                    await t.insert("nested")
                    await t.adapter.execute("RELEASE SAVEPOINT sp_1")  # the user ends it behind sqler's back
            except TransactionStateError as nested_raised:
                assert str(nested_raised) == BROKEN.format("committed")
                assert str(nested_raised.__cause__) == "no such savepoint: sp_1"
                if not caught:
                    raise
    assert str(raised.value) == BROKEN.format("committed")
    assert str(raised.value.__cause__) == "no such savepoint: sp_1"
    assert t.steps()[-7:] == [
        "BEGIN IMMEDIATE",
        "INSERT parent",
        "SAVEPOINT sp_1",
        "INSERT nested",
        "RELEASE SAVEPOINT sp_1",
        "RELEASE SAVEPOINT sp_1",
        "ROLLBACK",
    ]
    assert await t.rows() == ["before"]
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_nested_rollback_that_fails_sends_no_release_and_rolls_the_transaction_back(make_db):
    t = await make_db()
    await t.insert("before")
    with pytest.raises(TransactionStateError) as raised:
        async with t.db.transaction():
            await t.insert("parent")
            with pytest.raises(Undo) as nested_raised:
                async with t.db.transaction():
                    await t.insert("nested")
                    await t.adapter.execute("RELEASE SAVEPOINT sp_1")  # the user ends it behind sqler's back
                    raise Undo()
            assert nested_raised.value.__notes__ == [BROKEN.format("rolled back")]
    assert str(raised.value) == BROKEN.format("rolled back")
    assert str(raised.value.__cause__) == "no such savepoint: sp_1"
    assert t.steps()[-7:] == [
        "BEGIN IMMEDIATE",
        "INSERT parent",
        "SAVEPOINT sp_1",
        "INSERT nested",
        "RELEASE SAVEPOINT sp_1",
        "ROLLBACK TO SAVEPOINT sp_1",
        "ROLLBACK",
    ]
    assert await t.rows() == ["before"]
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_parent_raising_after_a_failed_nested_rollback_keeps_its_exception_with_a_note(make_db):
    t = await make_db()

    class ParentFailed(Exception):
        pass

    with pytest.raises(ParentFailed) as raised:
        async with t.db.transaction():
            await t.insert("parent")
            try:
                async with t.db.transaction():
                    await t.insert("nested")
                    await t.adapter.execute("RELEASE SAVEPOINT sp_1")
                    raise Undo()
            except Undo:
                pass
            raise ParentFailed("its own")
    assert str(raised.value) == "its own"
    assert raised.value.__notes__ == [BROKEN.format("rolled back")]
    assert await t.rows() == []
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_savepoint_that_sqlite_refuses_opened_nothing_and_leaves_no_place_behind(make_db):
    t = await make_db()
    async with t.db.transaction():
        # an INSERT ... RETURNING whose row is not read yet is a write in progress: SQLite refuses a SAVEPOINT
        cur = await t.adapter.execute("INSERT INTO t (v) VALUES ('r') RETURNING v")
        with pytest.raises(sqlite3.OperationalError, match="cannot open savepoint - SQL statements in progress"):
            async with t.db.transaction():
                await t.insert("never")
        assert await cur.fetchall() == [("r",)]
        start = asyncio.Event()
        start.set()
        # a sibling's nested transaction opens at once: the refused one left nothing on the stack
        await asyncio.wait_for(asyncio.ensure_future(nested_child(t, "after", start, asyncio.Event())), BOUND_S)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t (v)",
        "SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["after", "r"]
    await assert_pool_clean(t)


# --- the end of a transaction waits for nested ends that are being sent -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("child_raises", [False, True], ids=["release", "rollback-to"])
async def test_the_parent_committing_while_a_cancelled_child_end_is_being_sent_commits(make_db, child_raises):
    t = await make_db()
    ending = asyncio.Event()
    slow: list[asyncio.Future] = []

    async def child() -> None:
        async with t.db.transaction():
            await t.insert("child")
            slow.append(asyncio.ensure_future(t.adapter.execute(SLOW)))
            await asyncio.sleep(0)  # the slow statement is on the connection
            ending.set()
            if child_raises:
                raise Undo()
        # the block's end is queued behind the slow statement

    async def run() -> None:
        async with t.db.transaction():
            await t.insert("parent")
            task = asyncio.ensure_future(child())
            await ending.wait()
            for _ in range(3):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await (await slow[0]).close()

    await asyncio.wait_for(run(), BOUND_S * 2)
    child_end = ["ROLLBACK TO SAVEPOINT sp_1", "RELEASE SAVEPOINT sp_1"] if child_raises else ["RELEASE SAVEPOINT sp_1"]
    assert t.steps() == ["BEGIN IMMEDIATE", "INSERT parent", "SAVEPOINT sp_1", "INSERT child", *child_end, "COMMIT"]
    assert await t.rows() == (["parent"] if child_raises else ["child", "parent"])
    await assert_pool_clean(t)


# --- a transaction whose nested end failed refuses every statement ------------------------

BROKEN_STEPS = [
    "BEGIN IMMEDIATE",
    "INSERT parent",
    "SAVEPOINT sp_1",
    "INSERT h",
    "RELEASE SAVEPOINT sp_1",
    "RELEASE SAVEPOINT sp_1",
    "ROLLBACK",
]
BROKEN_COMMIT = (BROKEN.format("committed"), "no such savepoint: sp_1")
KINDS = ["execute", "executemany", "nested"]


async def send(t: Traced, kind: str, value: str) -> None:
    """Write ``value`` with a plain ``execute``, an ``executemany`` or in a nested transaction of its own."""
    if kind == "execute":
        await t.insert(value)
    elif kind == "executemany":
        await t.adapter.executemany("INSERT INTO t (v) VALUES (?)", [[value]])
    else:
        async with t.db.transaction():
            await t.insert(value)


async def nested_commit_that_fails(t: Traced, then=None, savepoint: str = "sp_1") -> None:
    """A nested transaction (its savepoint ``savepoint``) writes ``h`` and the user releases
    its savepoint with a raw statement, so the adapter's own RELEASE fails when the block
    commits. ``then`` runs after the raw RELEASE, inside the block."""
    with pytest.raises(TransactionStateError) as raised:
        async with t.db.transaction():
            await t.insert("h")
            await t.adapter.execute(f"RELEASE SAVEPOINT {savepoint}")  # the user ends it behind sqler's back
            if then is not None:
                await then()
    assert (str(raised.value), str(raised.value.__cause__)) == (
        BROKEN.format("committed"),
        f"no such savepoint: {savepoint}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("who", ["the-task-that-broke-it", "a-sibling"])
async def test_a_statement_sent_after_a_nested_end_failed_raises_and_sends_nothing(make_db, who, kind):
    t = await make_db()
    await t.insert("before")
    refused: list[tuple[str, str, list[str]]] = []

    async def refuse() -> None:
        sent = len(t.sent)
        with pytest.raises(TransactionStateError) as raised:
            await send(t, kind, "after")
        refused.append((str(raised.value), str(raised.value.__cause__), t.sent[sent:]))

    async def breaker() -> None:
        await nested_commit_that_fails(t)
        if who == "the-task-that-broke-it":
            await refuse()

    with pytest.raises(TransactionStateError) as ended:
        async with t.db.transaction():
            lease = t.adapter._task_lease.get()
            await t.insert("parent")
            await asyncio.wait_for(asyncio.ensure_future(breaker()), BOUND_S)
            if who == "a-sibling":
                await asyncio.wait_for(asyncio.ensure_future(refuse()), BOUND_S)
            assert (lease.turns, lease.savepoints) == ([], [])
    assert refused == [(*BROKEN_COMMIT, [])]
    assert (str(ended.value), str(ended.value.__cause__)) == BROKEN_COMMIT
    assert t.steps()[-7:] == BROKEN_STEPS
    assert await t.rows() == ["before"]
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_the_block_that_broke_its_transaction_then_raising_keeps_the_statement_error(make_db):
    """A statement in the outer block after the failed end raises; the block's end then
    rolls back and the statement's error leaves as it is (it already says it all)."""
    t = await make_db()
    await t.insert("before")
    with pytest.raises(TransactionStateError) as raised:
        async with t.db.transaction():
            await t.insert("parent")
            await asyncio.wait_for(asyncio.ensure_future(nested_commit_that_fails(t)), BOUND_S)
            await t.insert("after")
    assert (str(raised.value), str(raised.value.__cause__)) == BROKEN_COMMIT
    assert getattr(raised.value, "__notes__", []) == []
    assert t.steps()[-7:] == BROKEN_STEPS
    assert await t.rows() == ["before"]
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_nested_block_that_ends_after_its_transaction_broke_raises(make_db):
    """B's nested transaction is open below the one whose end fails; B's block then ends
    without an exception of its own: it raises, because what it wrote will not be committed."""
    t = await make_db()
    b_in = asyncio.Event()
    SP_2_BROKEN = (BROKEN.format("committed"), "no such savepoint: sp_2")

    async def b() -> None:
        with pytest.raises(TransactionStateError) as raised:
            async with t.db.transaction():
                await t.insert("b")
                b_in.set()
                # started inside B's nested transaction: it nests in it
                await asyncio.ensure_future(nested_commit_that_fails(t, savepoint="sp_2"))
        assert (str(raised.value), str(raised.value.__cause__)) == SP_2_BROKEN

    with pytest.raises(TransactionStateError) as ended:
        async with t.db.transaction():
            await asyncio.wait_for(b(), BOUND_S)
    assert (str(ended.value), str(ended.value.__cause__)) == SP_2_BROKEN
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT b",
        "SAVEPOINT sp_2",
        "INSERT h",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_2",
        "ROLLBACK",
    ]
    assert await t.rows() == []
    await assert_pool_clean(t)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kinds", [["execute"], ["executemany"], ["nested"], [KINDS[i % 3] for i in range(20)]],
    ids=["one-execute", "one-executemany", "one-nested", "twenty-mixed"],
)
async def test_siblings_waiting_when_a_nested_end_fails_all_raise_and_send_nothing(make_db, kinds):
    t = await make_db()
    await t.insert("before")
    h_in = asyncio.Event()
    asked = [asyncio.Event() for _ in kinds]
    outcomes: dict[int, object] = {}

    async def hold_until_all_asked() -> None:
        h_in.set()
        await asked[-1].wait()  # every sibling waits for this nested transaction to end

    async def sibling(i: int) -> None:
        await (asked[i - 1] if i else h_in).wait()
        asked[i].set()
        try:
            await send(t, kinds[i], f"s{i:02d}")
        except TransactionStateError as exc:
            outcomes[i] = (str(exc), str(exc.__cause__))
        else:
            outcomes[i] = "wrote"

    with pytest.raises(TransactionStateError) as ended:
        async with t.db.transaction():
            lease = t.adapter._task_lease.get()
            await t.insert("parent")
            await asyncio.wait_for(
                asyncio.gather(
                    nested_commit_that_fails(t, then=hold_until_all_asked), *(sibling(i) for i in range(len(kinds)))
                ),
                BOUND_S,
            )
            assert (lease.turns, lease.savepoints) == ([], [])
    assert outcomes == {i: BROKEN_COMMIT for i in range(len(kinds))}
    assert (str(ended.value), str(ended.value.__cause__)) == BROKEN_COMMIT
    assert t.steps()[-7:] == BROKEN_STEPS
    assert await t.rows() == ["before"]
    await assert_pool_clean(t)


# --- a nested transaction given its turn that never takes it ------------------------------


def block_the_loop_until(deadline: float) -> None:
    """Hold the event loop with real work in SQLite (not a sleep) until ``deadline`` has passed.

    Nothing else runs meanwhile: no timer, no task. Runs the slow statement on a private
    in-memory database until the loop's clock is past ``deadline`` (once, about a second).
    """
    loop = asyncio.get_running_loop()
    scratch = sqlite3.connect(":memory:")
    try:
        while loop.time() <= deadline:
            scratch.execute(SLOW).fetchall()
    finally:
        scratch.close()


@pytest.mark.asyncio
async def test_a_nested_transaction_cancelled_after_its_turn_and_before_it_runs_leaves_nothing(make_db):
    """B was given its turn (a savepoint is on the stack for it, not yet sent) and is
    cancelled before it runs: nothing of B stays on the stack, C (asked after B) opens next."""
    t = await make_db()
    h = HeldNested(t)
    b_asked, c_asked = asyncio.Event(), asyncio.Event()

    async with t.db.transaction():
        lease = t.adapter._task_lease.get()
        th = asyncio.ensure_future(h.run())
        await h.inside.wait()
        tb = asyncio.ensure_future(nested_child(t, "b", h.inside, b_asked))
        tc = asyncio.ensure_future(nested_child(t, "c", b_asked, c_asked))
        await c_asked.wait()
        h.end.set()
        await until_a_nested_end_is_sent(lease)  # b has its turn and has not run yet
        assert [sp.task for sp in lease.savepoints] == [tb]
        tb.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tb
        await asyncio.wait_for(asyncio.gather(th, tc), BOUND_S)
        assert (lease.turns, lease.savepoints) == ([], [])
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT h",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT c",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["c", "h"]
    await assert_pool_clean(t)


@pytest.mark.asyncio
async def test_a_nested_transaction_whose_deadline_passes_after_its_turn_still_opens(make_db):
    """A turn that has been given cannot time out: when the waiter runs, a turn it has is
    taken whatever the clock says, and the deadline is read only when it would wait (again).
    So a nested transaction whose deadline passes between its turn and its run opens: here
    the loop is held past B's deadline after B has its turn, and B opens, writes and commits.
    What happens to a waiter whose deadline has passed when it has to wait again is pinned in
    ``test_a_plain_statement_whose_place_ended_before_it_ran_waits_again``."""
    t = await make_db()
    h = HeldNested(t)
    b_asked = asyncio.Event()
    loop = asyncio.get_running_loop()

    async with t.db.transaction():
        lease = t.adapter._task_lease.get()
        th = asyncio.ensure_future(h.run())
        await h.inside.wait()
        t.adapter.acquire_timeout = 0.5  # b's bound
        b_deadline = loop.time() + 0.5
        tb = asyncio.ensure_future(nested_child(t, "b", h.inside, b_asked))
        await b_asked.wait()
        t.adapter.acquire_timeout = BOUND_S
        h.end.set()
        await until_a_nested_end_is_sent(lease)  # b has its turn and has not run yet
        assert [sp.task for sp in lease.savepoints] == [tb]
        block_the_loop_until(b_deadline)
        await asyncio.wait_for(asyncio.gather(th, tb), BOUND_S)
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "INSERT h",
        "RELEASE SAVEPOINT sp_1",
        "SAVEPOINT sp_1",
        "INSERT b",
        "RELEASE SAVEPOINT sp_1",
        "COMMIT",
    ]
    assert await t.rows() == ["b", "h"]
    await assert_pool_clean(t)


# --- a statement whose place ended before it ran waits again, where it was ------------------

# its first row comes at once; the rest (about a second of work) is read before another task's statement
LAZY = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) "
    "SELECT 1 UNION ALL SELECT count(*) FROM c"
)


async def x_waits_again(t: Traced, *, x_timeout: float, hold_s: float = 0.0, block: bool = False) -> dict:
    """X, started in the owner's nested transaction S, sends a plain insert while the
    grandchild G's nested transaction is open in S, and waits. Z, started in the
    transaction outside S, asks after X and waits. G ends: X has its turn in S (Z's place
    is not the top). Before X runs, the owner ends S, whose RELEASE is queued behind the
    rest of a long read of the owner's: X's place is now the transaction, under S's end
    being sent, so X waits again; Z asked after it.

    X asks with ``acquire_timeout = x_timeout`` (Z with ``BOUND_S``). G holds until
    ``hold_s`` after X asked. With ``block`` the loop is held past X's deadline after X has
    its turn and before it runs. Returns X's outcome and the clock at each point."""
    loop = asyncio.get_running_loop()
    g_in, g_end, x_asked, z_asked = (asyncio.Event() for _ in range(4))
    got: dict = {}

    async def grandchild() -> None:
        async with t.db.transaction():
            await t.insert("g")
            g_in.set()
            await g_end.wait()

    async def x() -> None:
        t.adapter.acquire_timeout = x_timeout
        got["asked_at"] = loop.time()
        x_asked.set()
        try:
            await t.insert("x")
        except LockTimeoutError:
            got["x"] = "timed out"
        else:
            got["x"] = "wrote"
        got["x_done_at"] = loop.time()

    async def z() -> None:
        await x_asked.wait()
        t.adapter.acquire_timeout = BOUND_S
        z_asked.set()
        await t.insert("z")

    async with t.db.transaction():
        lease = t.adapter._task_lease.get()
        tz = asyncio.ensure_future(z())  # outside S
        async with t.db.transaction():
            cur = await t.adapter.execute(LAZY)
            tg = asyncio.ensure_future(grandchild())
            await g_in.wait()
            tx = asyncio.ensure_future(x())
            await z_asked.wait()
            await turns_until(lambda: loop.time() >= got["asked_at"] + hold_s)
            g_end.set()
            await until_a_nested_end_is_sent(lease)  # x has its turn in S and has not run yet
            assert "x" not in got
            if block:
                block_the_loop_until(got["asked_at"] + x_timeout)
                got["unblocked_at"] = loop.time()
            fetch = asyncio.ensure_future(cur.fetchall())
            # G's insert had the rest of the read done before it; S's RELEASE is queued behind this
            busy = asyncio.ensure_future(lease.conn.execute(SLOW))
        assert await fetch == [(1,), (3000000,)]
        await cur.close()
        await (await busy).close()
        await asyncio.wait_for(asyncio.gather(tg, tx, tz), BOUND_S)
        assert (lease.turns, lease.savepoints) == ([], [])
    await assert_pool_clean(t)
    return got


@pytest.mark.asyncio
async def test_a_plain_statement_that_waits_again_keeps_its_place_before_one_asked_after_it(make_db):
    t = await make_db()
    got = await x_waits_again(t, x_timeout=BOUND_S)
    assert got["x"] == "wrote"
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "SAVEPOINT sp_2",
        "INSERT g",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_1",
        "INSERT x",
        "INSERT z",
        "COMMIT",
    ]
    assert await t.rows_in_order_written() == ["g", "x", "z"]


@pytest.mark.asyncio
async def test_a_plain_statement_that_waits_again_times_out_at_the_deadline_of_its_first_ask(make_db):
    """X asks with a 1 s bound and has its turn after half of it; the place it waits again
    for is held longer than the rest. X raises 1 s after it asked, not 1 s after its turn
    (1.5 s); Z, asked after it, writes."""
    t = await make_db()
    got = await x_waits_again(t, x_timeout=1.0, hold_s=0.5)
    assert got["x"] == "timed out"
    assert 1.0 <= got["x_done_at"] - got["asked_at"] < 1.4
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "SAVEPOINT sp_2",
        "INSERT g",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_1",
        "INSERT z",
        "COMMIT",
    ]
    assert await t.rows() == ["g", "z"]


@pytest.mark.asyncio
async def test_a_plain_statement_whose_place_ended_before_it_ran_waits_again(make_db):
    """X's deadline passes after X has its turn and before it runs (the loop is held); when
    it runs, its place has ended under it, so it has to wait again: it raises
    ``LockTimeoutError`` at once instead, and leaves no turn behind. Z, asked after it, writes."""
    t = await make_db()
    got = await x_waits_again(t, x_timeout=0.5, block=True)
    assert got["x"] == "timed out"
    assert got["x_done_at"] - got["unblocked_at"] < 0.25
    assert t.steps() == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sp_1",
        "SAVEPOINT sp_2",
        "INSERT g",
        "RELEASE SAVEPOINT sp_2",
        "RELEASE SAVEPOINT sp_1",
        "INSERT z",
        "COMMIT",
    ]
    assert await t.rows() == ["g", "z"]
