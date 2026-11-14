"""The async operations in ``sqler.ops`` return their connection and match their sync twins.

``async_health_check``, ``async_get_stats`` and ``async_checkpoint`` used to read and
never release; ``async_vacuum`` committed and never released. The calling task then kept
a pooled connection until it ended. Every test here runs on a file database with a pool
of one, so a connection that stays out shows at once, and compares the result with the
sync function on the same file.
"""

import asyncio
import sqlite3
import sys

import pytest
import pytest_asyncio
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.db.sqler_db import SQLerDB
from sqler.exceptions import QueryError
from sqler.ops import (
    async_backup,
    async_checkpoint,
    async_get_stats,
    async_health_check,
    async_is_healthy,
    async_vacuum,
    backup,
    checkpoint,
    get_stats,
    health_check,
    is_healthy,
    vacuum,
)

BOUND_S = 2.0
PRAGMAS = ["PRAGMA busy_timeout = 5000", "PRAGMA journal_mode = WAL"]


def _adapter(path: str) -> AsyncSQLiteAdapter:
    return AsyncSQLiteAdapter(path, pragmas=PRAGMAS, pool_size=1)


@pytest_asyncio.fixture
async def db(tmp_path):
    d = AsyncSQLerDB(_adapter(str(tmp_path / "o.db")))
    await d.connect()
    for i in range(20):
        await d.insert_document("things", {"n": i, "pad": "x" * 200})
    try:
        yield d
    finally:
        await d.close()


@pytest.fixture
def sync_db(db):
    """A sync handle on the very same file."""
    sdb = SQLerDB.on_disk(db.adapter.path)
    try:
        yield sdb
    finally:
        sdb.close()


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


async def _checkpointed(db: AsyncSQLerDB) -> None:
    """Move the WAL into the main file so a sync and an async reading see the same sizes."""
    await async_checkpoint(db, "TRUNCATE")


# --- the same results as the sync twin ------------------------------------


@pytest.mark.asyncio
async def test_health_check_matches_sync(db, sync_db):
    a = await async_health_check(db)
    s = health_check(sync_db)

    assert (a.healthy, a.message, a.details) == (s.healthy, s.message, s.details)
    assert a.healthy is True
    assert await async_is_healthy(db) is is_healthy(sync_db) is True
    _assert_returned(db)


@pytest.mark.asyncio
async def test_get_stats_matches_sync(db, sync_db):
    await _checkpointed(db)
    a = await async_get_stats(db)
    s = get_stats(sync_db)

    a_d, s_d = a.to_dict(), s.to_dict()
    a_d.pop("timestamp"), s_d.pop("timestamp")
    assert a_d == s_d
    assert a.table_count >= 1 and a.page_count > 1
    _assert_returned(db)


@pytest.mark.asyncio
async def test_checkpoint_matches_sync_for_every_mode(db, sync_db):
    for mode in ("PASSIVE", "FULL", "RESTART", "TRUNCATE", "passive"):
        a = await async_checkpoint(db, mode)
        s = checkpoint(sync_db, mode)
        assert a == s, mode
        _assert_returned(db)
    with pytest.raises(ValueError, match="Invalid checkpoint mode"):
        await async_checkpoint(db, "BOGUS")
    _assert_returned(db)


@pytest.mark.asyncio
async def test_backup_matches_sync(db, sync_db, tmp_path):
    a = await async_backup(db, str(tmp_path / "a.bak"))
    s = backup(sync_db, str(tmp_path / "s.bak"))

    assert (a.success, a.error, a.source_path) == (s.success, s.error, s.source_path)
    assert a.success is True
    assert a.size_bytes > 0 and a.size_bytes == s.size_bytes

    def rows(path):
        con = sqlite3.connect(path)
        try:
            return con.execute("SELECT _id, data FROM things ORDER BY _id").fetchall()
        finally:
            con.close()

    assert rows(str(tmp_path / "a.bak")) == rows(str(tmp_path / "s.bak"))
    assert len(rows(str(tmp_path / "a.bak"))) == 20
    _assert_returned(db)


@pytest.mark.asyncio
async def test_backup_failure_matches_sync_and_returns_the_connection(db, sync_db, tmp_path):
    bad = str(tmp_path / "no-such-dir" / "x.bak")
    a = await async_backup(db, bad)
    s = backup(sync_db, bad)

    assert a.success is s.success is False
    assert a.error == s.error
    assert a.size_bytes == s.size_bytes == 0
    _assert_returned(db)


@pytest.mark.asyncio
async def test_vacuum_does_what_the_sync_vacuum_does(db, tmp_path):
    """Two files built the same way, one vacuumed by each function."""
    other = SQLerDB.on_disk(str(tmp_path / "twin.db"))
    try:
        for i in range(20):
            other.insert_document("things", {"n": i, "pad": "x" * 200})
        cur = await db.adapter.execute("DELETE FROM things WHERE _id > 2")
        await cur.close()
        await db.adapter.auto_commit()
        other.adapter.execute("DELETE FROM things WHERE _id > 2")
        other.adapter.commit()
        await _checkpointed(db)
        before = (await async_get_stats(db)).freelist_count
        assert before > 0 and before == get_stats(other).freelist_count

        a_ms = await async_vacuum(db)
        s_ms = vacuum(other)

        assert isinstance(a_ms, float) and a_ms >= 0 and isinstance(s_ms, float)
        after_a = await async_get_stats(db)
        after_s = get_stats(other)
        assert after_a.freelist_count == after_s.freelist_count == 0
        assert after_a.page_count == after_s.page_count
        _assert_returned(db)
    finally:
        other.close()


# --- the connection goes back ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["health", "stats", "checkpoint", "vacuum", "backup"])
async def test_op_returns_the_connection_and_another_task_can_write(db, tmp_path, op):
    calls = {
        "health": lambda: async_health_check(db),
        "stats": lambda: async_get_stats(db),
        "checkpoint": lambda: async_checkpoint(db),
        "vacuum": lambda: async_vacuum(db),
        "backup": lambda: async_backup(db, str(tmp_path / "b.bak")),
    }
    await calls[op]()

    _assert_returned(db)
    await _another_task_can_write(db)


@pytest.mark.asyncio
async def test_a_failing_statement_inside_an_op_holds_nothing(db, monkeypatch):
    real = db.adapter.execute

    async def failing(query, params=None):
        if "page_size" in query:
            return await real("SELECT * FROM no_such_table")
        return await real(query, params)

    monkeypatch.setattr(db.adapter, "execute", failing)
    with pytest.raises(Exception, match="no such table"):
        await async_get_stats(db)
    _assert_returned(db)


@pytest.mark.asyncio
async def test_reads_inside_a_caller_transaction_use_its_connection_and_do_not_end_it(db):
    async with db.transaction():
        cur = await db.adapter.execute("INSERT INTO things (data) VALUES (json('{\"n\": 99}'))")
        await cur.close()

        assert (await async_health_check(db)).healthy
        await async_get_stats(db)

        assert db.adapter.in_transaction
        assert db.pool_stats().available_connections == 0  # still the caller's
        cur = await db.adapter.execute("SELECT COUNT(*) FROM things")  # the caller's insert is seen
        assert (await cur.fetchone())[0] == 21
        await cur.close()

    _assert_returned(db)
    cur = await db.adapter.execute("SELECT COUNT(*) FROM things")
    assert (await cur.fetchone())[0] == 21  # committed with the caller's transaction
    await cur.close()
    await db.adapter.auto_commit()


@pytest.mark.asyncio
async def test_vacuum_inside_a_transaction_is_a_clear_error_and_the_transaction_survives(db):
    with pytest.raises(QueryError, match="VACUUM cannot run inside a transaction"):
        async with db.transaction():
            cur = await db.adapter.execute(
                "INSERT INTO things (data) VALUES (json('{\"n\": 99}'))"
            )
            await cur.close()
            with pytest.raises(QueryError, match="VACUUM cannot run inside a transaction"):
                await async_vacuum(db)
            assert db.adapter.in_transaction
            cur = await db.adapter.execute("INSERT INTO things (data) VALUES (json('{\"n\": 100}'))")
            await cur.close()
            cur = await db.adapter.execute("SELECT COUNT(*) FROM things")
            assert (await cur.fetchone())[0] == 22  # the transaction took both inserts
            await cur.close()
            await async_vacuum(db)  # the error again, now leaving the block

    _assert_returned(db)
    cur = await db.adapter.execute("SELECT COUNT(*) FROM things")
    assert (await cur.fetchone())[0] == 20  # rolled back with the error
    await cur.close()
    await db.adapter.auto_commit()


@pytest.mark.asyncio
async def test_health_check_on_a_closed_database_returns_and_holds_nothing(tmp_path):
    """The sync twin answers "not connected"; the async adapter connects again on first use
    (its lazy connect), so the probe runs on a fresh pool. What is pinned here is that the
    call returns a status and leaves that pool whole."""
    d = AsyncSQLerDB(_adapter(str(tmp_path / "closed.db")))
    await d.connect()
    await d.close()
    try:
        status = await async_health_check(d)

        assert status.healthy is True
        assert status.message == "OK"
        stats = d.pool_stats()
        assert stats.available_connections == stats.total_connections
    finally:
        await d.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["health", "stats", "checkpoint", "vacuum", "backup"])
async def test_op_waits_for_a_task_holding_the_only_connection_then_runs(db, tmp_path, op):
    calls = {
        "health": lambda: async_health_check(db),
        "stats": lambda: async_get_stats(db),
        "checkpoint": lambda: async_checkpoint(db),
        "vacuum": lambda: async_vacuum(db),
        "backup": lambda: async_backup(db, str(tmp_path / "w.bak")),
    }
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with db.transaction():
            cur = await db.adapter.execute("INSERT INTO things (data) VALUES (json('{\"n\": 7}'))")
            await cur.close()
            holding.set()
            await release.wait()

    holder_task = asyncio.ensure_future(holder())
    await asyncio.wait_for(holding.wait(), BOUND_S)
    runner = asyncio.ensure_future(calls[op]())

    async def waiting():
        while db.adapter._waiting < 1:
            await asyncio.sleep(0)

    await asyncio.wait_for(waiting(), BOUND_S)
    assert not runner.done()
    release.set()
    result = await asyncio.wait_for(runner, BOUND_S)
    await holder_task

    if op == "health":
        assert result.healthy
    elif op == "backup":
        assert result.success
    _assert_returned(db)


@pytest.mark.asyncio
async def test_checkpoint_inside_a_caller_transaction_is_refused_by_sqlite_and_does_not_end_it(db):
    """SQLite will not checkpoint under the caller's write lock; the transaction stays open."""
    async with db.transaction():
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await async_checkpoint(db)

        assert db.adapter.in_transaction
        assert db.pool_stats().available_connections == 0
        cur = await db.adapter.execute("SELECT COUNT(*) FROM things")
        assert (await cur.fetchone())[0] == 20  # the connection is still usable
        await cur.close()

    _assert_returned(db)


@pytest.mark.asyncio
async def test_health_check_returns_unhealthy_when_returning_the_connection_fails(tmp_path):
    """A deferred foreign key left unmet by this task makes the probe's own commit fail."""
    adapter = AsyncSQLiteAdapter(
        str(tmp_path / "fk.db"),
        pragmas=["PRAGMA foreign_keys = ON", "PRAGMA busy_timeout = 5000"],
        pool_size=1,
    )
    d = AsyncSQLerDB(adapter)
    await d.connect()
    try:
        for sql in (
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child (p INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)",
            "INSERT INTO child VALUES (1)",  # no parent 1: the commit will refuse
        ):
            cur = await d.adapter.execute(sql)
            await cur.close()

        status = await async_health_check(d)

        assert status.healthy is False
        assert "could not return the connection" in status.message
        assert "FOREIGN KEY constraint failed" in status.message
        _assert_returned_n(d)
    finally:
        await d.close()


def _assert_returned_n(d: AsyncSQLerDB) -> None:
    stats = d.pool_stats()
    assert stats.available_connections == stats.total_connections, stats


@pytest.mark.skipif(sys.platform == "win32", reason="Windows refuses to delete a file that is open")
@pytest.mark.asyncio
async def test_health_check_on_a_database_file_deleted_after_connecting_returns_a_status(tmp_path):
    path = tmp_path / "gone.db"
    d = AsyncSQLerDB(_adapter(str(path)))
    await d.connect()
    try:
        for suffix in ("", "-wal", "-shm"):
            (tmp_path / f"gone.db{suffix}").unlink(missing_ok=True)

        status = await async_health_check(d)

        assert isinstance(status.healthy, bool)
        assert status.message
        _assert_returned_n(d)
    finally:
        await d.close()
