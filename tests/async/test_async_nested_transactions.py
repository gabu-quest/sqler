"""Nested async transactions of sibling tasks that share their parent's connection.

Tasks started inside ``async with db.transaction():`` share the parent's pinned
connection, and a connection has one stack of savepoints. A nested transaction one
task opens while a sibling's nested transaction is open waits until that one has
committed or rolled back; the savepoints on the connection are then opened and closed
in stack order, so each child's commit or rollback touches only its own rows.

Nesting in one task, and in a task started inside a nested transaction and awaited in
it, does not wait. Tasks are ordered with ``asyncio.Event``s; every test is bounded.
"""

import asyncio
import os
import tempfile

import pytest
import pytest_asyncio
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import LockTimeoutError
from sqler.logging import query_logger

BOUND_S = 5.0
PRAGMAS = ["PRAGMA busy_timeout = 2000", "PRAGMA journal_mode = WAL", "PRAGMA synchronous = OFF"]
TXN_WORDS = {"BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT", "INSERT"}


class Undo(Exception):
    pass


class Traced:
    """A database on a temporary file with two pooled connections and a statement trace."""

    def __init__(self, db: AsyncSQLerDB):
        self.db = db
        self.adapter: AsyncSQLiteAdapter = db.adapter
        self.sent: list[str] = []

    async def trace(self) -> None:
        for conn in self.adapter._all_conns:
            await conn.set_trace_callback(self.sent.append)

    def statements(self) -> list[str]:
        """The transaction statements and inserts sent, in order, without their values."""
        out = []
        for sql in self.sent:
            words = sql.split()
            if words and words[0].upper() in TXN_WORDS:
                out.append(sql.split(" VALUES")[0])
        return out

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


@pytest.mark.asyncio
async def test_a_nested_transaction_of_a_task_that_outlived_its_parent_transaction(make_db):
    """Pins what the released version does: the task still counts as nested, so its
    ``SAVEPOINT`` on the connection it kept starts a transaction of its own and its
    ``RELEASE`` commits it."""
    t = await make_db()
    parent_done = asyncio.Event()

    async def late() -> None:
        await t.insert("early")
        await parent_done.wait()
        async with t.db.transaction():
            await t.insert("late")

    async def parent() -> asyncio.Future:
        async with t.db.transaction():
            task = asyncio.ensure_future(late())
            await t.insert("parent")
        return task

    task = await parent()
    parent_done.set()
    await asyncio.wait_for(task, BOUND_S)
    if t.adapter._reclaims:
        await asyncio.wait_for(asyncio.gather(*t.adapter._reclaims), BOUND_S)
    assert t.statements() == [
        "BEGIN IMMEDIATE",
        "INSERT INTO t (v)",
        "INSERT INTO t (v)",
        "COMMIT",
        "SAVEPOINT sp_1",
        "INSERT INTO t (v)",
        "RELEASE SAVEPOINT sp_1",
    ]
    assert await t.rows() == ["early", "late", "parent"]
    await assert_pool_whole_and_usable(t)
