"""The async migration runner: a migration and its version row are one transaction.

Before, ``AsyncMigrationRunner`` ran ``up()`` and wrote the version row with no
transaction, so an ``up()`` that failed halfway left a half-applied schema with no
version recorded, and the runner kept its pooled connection after every call.
These tests run on a file database with a pool of one, so a connection that is not
returned shows at once, and compare the runner with the sync ``MigrationRunner``.
"""

import asyncio

import pytest
import pytest_asyncio
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.db.sqler_db import SQLerDB
from sqler.migrations import (
    MIGRATIONS_TABLE,
    AsyncMigration,
    AsyncMigrationRunner,
    Migration,
    MigrationRunner,
)

BOUND_S = 2.0


@pytest_asyncio.fixture
async def db(tmp_path):
    adapter = AsyncSQLiteAdapter(
        str(tmp_path / "m.db"),
        pragmas=["PRAGMA busy_timeout = 5000", "PRAGMA journal_mode = WAL"],
        pool_size=1,
    )
    d = AsyncSQLerDB(adapter)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


async def _tables(db: AsyncSQLerDB) -> set[str]:
    cur = await db.adapter.execute("SELECT name FROM sqlite_master WHERE type='table'")
    rows = await cur.fetchall()
    await cur.close()
    await db.adapter.auto_commit()
    return {r[0] for r in rows}


async def _versions(db: AsyncSQLerDB) -> list[int]:
    cur = await db.adapter.execute(f"SELECT version FROM {MIGRATIONS_TABLE} ORDER BY version")
    rows = await cur.fetchall()
    await cur.close()
    await db.adapter.auto_commit()
    return [r[0] for r in rows]


async def _exec(db: AsyncSQLerDB, sql: str) -> None:
    cur = await db.adapter.execute(sql)
    await cur.close()


def _assert_returned(db: AsyncSQLerDB) -> None:
    stats = db.pool_stats()
    assert stats.available_connections == stats.total_connections == 1, stats


async def _another_task_can_write(db: AsyncSQLerDB) -> None:
    async def write():
        cur = await db.adapter.execute("CREATE TABLE IF NOT EXISTS probe (v)")
        await cur.close()
        cur = await db.adapter.execute("INSERT INTO probe VALUES (1)")
        await cur.close()
        await db.adapter.auto_commit()

    await asyncio.wait_for(asyncio.ensure_future(write()), BOUND_S)


async def up_create_a(db):
    await _exec(db, "CREATE TABLE a (x)")


async def down_drop_a(db):
    await _exec(db, "DROP TABLE a")


async def up_create_b(db):
    await _exec(db, "CREATE TABLE b (x)")


async def up_create_then_raise(db):
    await _exec(db, "CREATE TABLE half (x)")
    await _exec(db, "INSERT INTO half VALUES (1)")
    raise RuntimeError("boom")


async def down_drop_then_raise(db):
    await _exec(db, "DROP TABLE a")
    raise RuntimeError("down boom")


# --- item 1: atomic -------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_up_leaves_schema_data_and_versions_as_they_were(db):
    runner = AsyncMigrationRunner(
        db,
        [
            AsyncMigration(1, "a", up_create_a, down_drop_a),
            AsyncMigration(2, "half", up_create_then_raise),
        ],
    )
    result = await runner.migrate()

    assert result.success is False
    assert result.error == "boom"
    assert result.error_at_version == 2
    assert [r.version for r in result.applied] == [1]
    assert result.current_version == 1
    assert "half" not in await _tables(db)
    assert await _versions(db) == [1]
    _assert_returned(db)


@pytest.mark.asyncio
async def test_cancelled_up_leaves_nothing_and_returns_the_connection(db):
    started = asyncio.Event()

    async def up_hangs(d):
        await _exec(d, "CREATE TABLE hung (x)")
        started.set()
        await asyncio.Event().wait()  # until cancelled

    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "hung", up_hangs)])
    task = asyncio.ensure_future(runner.migrate())
    await asyncio.wait_for(started.wait(), BOUND_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "hung" not in await _tables(db)
    assert await _versions(db) == []
    _assert_returned(db)
    await _another_task_can_write(db)


@pytest.mark.asyncio
async def test_up_timing_out_with_wait_for_is_a_failed_result(db):
    async def up_slow(d):
        await _exec(d, "CREATE TABLE slow (x)")
        await asyncio.wait_for(asyncio.Event().wait(), 0.05)

    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "slow", up_slow)])
    result = await runner.migrate()

    assert result.success is False
    assert result.error_at_version == 1
    assert "slow" not in await _tables(db)
    assert await _versions(db) == []
    _assert_returned(db)


@pytest.mark.asyncio
async def test_migration_applied_twice_does_nothing_the_second_time(db):
    calls = []

    async def up(d):
        calls.append(1)
        await _exec(d, "CREATE TABLE a (x)")

    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up, down_drop_a)])
    first = await runner.migrate()
    second = await runner.migrate()

    assert first.success and [r.version for r in first.applied] == [1]
    assert second.success is True
    assert second.applied == []
    assert second.current_version == 1
    assert calls == [1]
    assert await _versions(db) == [1]
    _assert_returned(db)


@pytest.mark.asyncio
async def test_rollback_with_nothing_applied_is_a_successful_no_op(db):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a, down_drop_a)])
    result = await runner.rollback()

    assert result.success is True
    assert result.rolled_back == []
    assert result.current_version == 0
    assert "a" not in await _tables(db)
    _assert_returned(db)


@pytest.mark.asyncio
async def test_failing_down_keeps_the_schema_and_the_version_row(db):
    runner = AsyncMigrationRunner(
        db, [AsyncMigration(1, "a", up_create_a, down_drop_then_raise)]
    )
    assert (await runner.migrate()).success
    result = await runner.rollback()

    assert result.success is False
    assert result.error == "down boom"
    assert result.error_at_version == 1
    assert result.rolled_back == []
    assert result.current_version == 1
    assert "a" in await _tables(db)
    assert await _versions(db) == [1]
    _assert_returned(db)


@pytest.mark.asyncio
async def test_rollback_runs_down_and_removes_the_row_together(db):
    async def down_drop_b(d):
        await _exec(d, "DROP TABLE b")

    runner = AsyncMigrationRunner(
        db,
        [
            AsyncMigration(1, "a", up_create_a, down_drop_a),
            AsyncMigration(2, "b", up_create_b, down_drop_b),
        ],
    )
    assert (await runner.migrate()).success
    result = await runner.rollback(1)

    assert result.success is True
    assert [r.version for r in result.rolled_back] == [2]
    assert result.current_version == 1
    tables = await _tables(db)
    assert "b" not in tables and "a" in tables
    assert await _versions(db) == [1]
    _assert_returned(db)


@pytest.mark.asyncio
async def test_rollback_past_a_migration_without_down_stops_there(db):
    runner = AsyncMigrationRunner(
        db,
        [AsyncMigration(1, "a", up_create_a, down_drop_a), AsyncMigration(2, "b", up_create_b)],
    )
    assert (await runner.migrate()).success
    result = await runner.rollback(0)

    assert result.success is False
    assert result.error == "Migration 2 has no down() function"
    assert result.error_at_version == 2
    assert result.rolled_back == []
    assert await _versions(db) == [1, 2]
    _assert_returned(db)


@pytest.mark.asyncio
async def test_a_migration_whose_up_opens_its_own_transaction_still_works(db):
    async def up_own_txn(d):
        async with d.transaction():
            await _exec(d, "CREATE TABLE own (x)")
            await _exec(d, "INSERT INTO own VALUES (1)")

    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "own", up_own_txn)])
    result = await runner.migrate()

    assert result.success is True
    assert "own" in await _tables(db)
    assert await _versions(db) == [1]
    _assert_returned(db)


@pytest.mark.asyncio
async def test_up_that_commits_through_the_adapter_is_still_one_transaction(db):
    async def up_commits_then_raises(d):
        await _exec(d, "CREATE TABLE early (x)")
        await d.adapter.auto_commit()  # a no-op inside the runner's transaction
        raise RuntimeError("late")

    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "early", up_commits_then_raises)])
    result = await runner.migrate()

    assert result.success is False
    assert "early" not in await _tables(db)
    _assert_returned(db)


# --- item 2: the connection goes back -------------------------------------


@pytest.mark.asyncio
async def test_every_runner_method_returns_the_connection(db):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a, down_drop_a)])

    assert await runner.current_version() == 0
    _assert_returned(db)
    assert len(await runner.pending_migrations()) == 1
    _assert_returned(db)
    assert (await runner.status())["pending_count"] == 1
    _assert_returned(db)
    assert (await runner.migrate()).success
    _assert_returned(db)
    await _another_task_can_write(db)
    assert (await runner.rollback()).success
    _assert_returned(db)
    await _another_task_can_write(db)


@pytest.mark.asyncio
async def test_a_caller_transaction_keeps_its_connection_and_decides_the_outcome(db):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a, down_drop_a)])
    await runner.current_version()  # the bookkeeping table is made before the caller's transaction

    with pytest.raises(RuntimeError, match="caller gives up"):
        async with db.transaction():
            result = await runner.migrate()
            assert result.success
            assert db.adapter.in_transaction
            assert db.pool_stats().available_connections == 0  # still the caller's
            raise RuntimeError("caller gives up")

    assert "a" not in await _tables(db)  # the caller's rollback undid the migration
    assert await _versions(db) == []
    _assert_returned(db)


@pytest.mark.asyncio
async def test_a_failing_up_inside_a_caller_transaction_leaves_it_usable(db):
    runner = AsyncMigrationRunner(
        db,
        [
            AsyncMigration(1, "a", up_create_a),
            AsyncMigration(2, "half", up_create_then_raise),
        ],
    )
    async with db.transaction():
        await _exec(db, "CREATE TABLE mine (x)")
        result = await runner.migrate()
        assert result.success is False
        assert result.error_at_version == 2
        await _exec(db, "INSERT INTO mine VALUES (1)")  # the caller's transaction still works

    tables = await _tables(db)
    assert "mine" in tables and "a" in tables and "half" not in tables
    assert await _versions(db) == [1]
    _assert_returned(db)


@pytest.mark.asyncio
async def test_migrate_waits_for_a_task_holding_the_only_connection(db):
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with db.transaction():
            holding.set()
            await release.wait()

    holder_task = asyncio.ensure_future(holder())
    await asyncio.wait_for(holding.wait(), BOUND_S)
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a)])
    mig = asyncio.ensure_future(runner.migrate())

    async def waiting():
        while db.adapter._waiting < 1:
            await asyncio.sleep(0)

    await asyncio.wait_for(waiting(), BOUND_S)
    assert not mig.done()
    release.set()
    result = await asyncio.wait_for(mig, BOUND_S)
    await holder_task

    assert result.success
    _assert_returned(db)


# --- item 3: the same results as the sync runner --------------------------


def _untimed(status: dict) -> dict:
    """A status without the fields that hold a clock reading."""
    out = dict(status)
    out["applied"] = [
        {k: v for k, v in rec.items() if k not in ("applied_at", "duration_ms")}
        for rec in status["applied"]
    ]
    return out


@pytest.mark.asyncio
async def test_results_match_the_sync_runner(db, tmp_path):
    def s_up_a(d):
        d.adapter.execute("CREATE TABLE a (x)")

    def s_down_a(d):
        d.adapter.execute("DROP TABLE a")

    def s_up_bad(d):
        d.adapter.execute("CREATE TABLE half (x)")
        raise RuntimeError("boom")

    sdb = SQLerDB.on_disk(str(tmp_path / "sync.db"))
    srunner = MigrationRunner(
        sdb,
        [Migration(1, "a", s_up_a, s_down_a), Migration(2, "half", s_up_bad)],
    )
    arunner = AsyncMigrationRunner(
        db,
        [AsyncMigration(1, "a", up_create_a, down_drop_a), AsyncMigration(2, "half", up_create_then_raise)],
    )

    def shape(r):
        return (
            r.success,
            [x.version for x in r.applied],
            [(x.version, x.name) for x in r.rolled_back],
            r.current_version,
            r.target_version,
            r.error,
            r.error_at_version,
        )

    s1, a1 = srunner.migrate(), await arunner.migrate()
    assert shape(a1) == shape(s1)
    assert await arunner.current_version() == srunner.current_version() == 1
    assert [m.version for m in await arunner.pending_migrations()] == [
        m.version for m in srunner.pending_migrations()
    ]
    assert _untimed(await arunner.status()) == _untimed(srunner.status())
    assert [(r.version, r.name) for r in await arunner.applied_migrations()] == [
        (r.version, r.name) for r in srunner.applied_migrations()
    ]

    s2, a2 = srunner.rollback(0), await arunner.rollback(0)
    assert shape(a2) == shape(s2)
    assert _untimed(await arunner.status()) == _untimed(srunner.status())

    s3, a3 = srunner.rollback(), await arunner.rollback()
    assert shape(a3) == shape(s3)

    stables = {r[0] for r in sdb.adapter.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert (await _tables(db)) == stables


# --- WS-T fixes -----------------------------------------------------------


def _failing_fetch(db, method: str, message: str):
    """Make ``adapter.execute`` hand back a cursor whose ``method`` raises (the brief allows it:
    a fetch that fails midway cannot be had from a real database)."""
    real = db.adapter.execute

    async def execute(query, params=None):
        cursor = await real(query, params)
        if MIGRATIONS_TABLE in query and query.lstrip().upper().startswith("SELECT"):

            class Wrapped:
                def __getattr__(self, name):
                    return getattr(cursor, name)

                async def close(self):
                    await cursor.close()

            async def boom():
                raise RuntimeError(message)

            wrapped = Wrapped()
            setattr(wrapped, method, boom)
            return wrapped
        return cursor

    return real, execute


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call,method",
    [("current_version", "fetchone"), ("applied_migrations", "fetchall")],
)
async def test_a_read_that_fails_returns_the_connection_and_raises_its_error(db, call, method):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a)])
    await runner.current_version()
    real, execute = _failing_fetch(db, method, "fetch broke")
    db.adapter.execute = execute
    try:
        with pytest.raises(RuntimeError, match="fetch broke"):
            await getattr(runner, call)()
    finally:
        db.adapter.execute = real

    _assert_returned(db)
    await _another_task_can_write(db)


@pytest.mark.asyncio
async def test_a_read_that_fails_inside_a_caller_transaction_leaves_it_with_the_caller(db):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a)])
    await runner.current_version()
    async with db.transaction():
        await _exec(db, "CREATE TABLE mine (x)")
        real, execute = _failing_fetch(db, "fetchone", "fetch broke")
        db.adapter.execute = execute
        try:
            with pytest.raises(RuntimeError, match="fetch broke"):
                await runner.current_version()
        finally:
            db.adapter.execute = real
        assert db.adapter.in_transaction
        assert db.pool_stats().available_connections == 0  # still the caller's
        await _exec(db, "INSERT INTO mine VALUES (1)")

    assert "mine" in await _tables(db)
    _assert_returned(db)


@pytest.mark.asyncio
async def test_a_runner_first_called_in_a_rolled_back_transaction_makes_its_table_again(db):
    for _ in range(2):  # the second round starts from nothing again: a fresh runner each time
        runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a)])
        with pytest.raises(RuntimeError, match="caller gives up"):
            async with db.transaction():
                assert await runner.current_version() == 0
                raise RuntimeError("caller gives up")
        assert MIGRATIONS_TABLE not in await _tables(db)

        assert await runner.current_version() == 0  # the table is made again
        assert MIGRATIONS_TABLE in await _tables(db)
        await _exec(db, f"DROP TABLE {MIGRATIONS_TABLE}")
        await db.adapter.auto_commit()
    _assert_returned(db)


@pytest.mark.asyncio
async def test_a_runner_works_after_its_table_was_rolled_back_and_migrates(db):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a)])
    with pytest.raises(RuntimeError, match="caller gives up"):
        async with db.transaction():
            await runner.current_version()
            raise RuntimeError("caller gives up")

    result = await runner.migrate()
    assert result.success and result.current_version == 1
    assert await _versions(db) == [1]


EXECUTESCRIPT_HINT = "one execute() per statement"


async def up_executescript(db):
    cur = await db.adapter.executescript("CREATE TABLE s1 (x); CREATE TABLE s2 (x);")
    await cur.close()


async def down_executescript(db):
    cur = await db.adapter.executescript("DROP TABLE a;")
    await cur.close()


@pytest.mark.asyncio
async def test_executescript_in_up_fails_and_tells_the_author_what_to_do(db):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "s", up_executescript)])
    result = await runner.migrate()

    assert result.success is False
    assert result.error_at_version == 1
    assert "executescript cannot run inside a transaction" in result.error
    assert EXECUTESCRIPT_HINT in result.error
    tables = await _tables(db)
    assert "s1" not in tables and "s2" not in tables
    assert await _versions(db) == []
    _assert_returned(db)


@pytest.mark.asyncio
async def test_executescript_in_down_fails_and_tells_the_author_what_to_do(db):
    runner = AsyncMigrationRunner(db, [AsyncMigration(1, "a", up_create_a, down_executescript)])
    assert (await runner.migrate()).success

    result = await runner.rollback(0)

    assert result.success is False
    assert result.error_at_version == 1
    assert "executescript cannot run inside a transaction" in result.error
    assert EXECUTESCRIPT_HINT in result.error
    assert "a" in await _tables(db)
    assert await _versions(db) == [1]
    _assert_returned(db)
