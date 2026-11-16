"""Ending a transaction by rollback runs no statement of its own and raises nothing of its own.

The database object remembers the promoted columns and the ``_version`` column of the tables
it created. What it learned inside a transaction is kept with that transaction, and a rollback
drops it without a statement. So a statement cancelled inside a transaction reaches the caller at once, with its own
exception, whatever the database object remembers, even with a pool of one whose only
connection is still busy with the cancelled statement.
"""

import asyncio
import os
import tempfile
import time

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.adapter.asynchronous import AsyncSQLiteAdapter

# runs for seconds in the connection's thread; cancelled after CANCEL_AFTER_S
SLOW_COUNT = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 8000000) "
    "SELECT count(*) FROM c"
)
CANCEL_AFTER_S = 0.2
CALLER_SEES_IT_S = 0.5
pytestmark = pytest.mark.asyncio


class RollPromo(AsyncSQLerModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


def _db(tmpdir: str, acquire_timeout: float) -> AsyncSQLerDB:
    pragmas = ["PRAGMA busy_timeout = 5000", "PRAGMA journal_mode = WAL", "PRAGMA synchronous = OFF"]
    ad = AsyncSQLiteAdapter(
        os.path.join(tmpdir, "f.db"), pragmas=pragmas, pool_size=1, acquire_timeout=acquire_timeout
    )
    return AsyncSQLerDB(ad)


@pytest_asyncio.fixture
async def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


async def _cancel_in_transaction(db: AsyncSQLerDB) -> tuple[type, float]:
    """Insert, then a slow statement cancelled by ``wait_for``, in one transaction.

    Returns the exception type the caller saw and the seconds from the start of the transaction.
    """
    start = time.perf_counter()
    try:
        async with db.transaction():
            await db.adapter.execute("INSERT INTO t (v) VALUES ('x')")
            await asyncio.wait_for(db.adapter.execute(SLOW_COUNT), CANCEL_AFTER_S)
    except BaseException as exc:  # the type is what the test asserts
        return type(exc), time.perf_counter() - start
    raise AssertionError("the slow statement was not cancelled")


async def _setup(db: AsyncSQLerDB) -> None:
    await db.connect()
    await db.adapter.execute("CREATE TABLE t (v TEXT)")
    await db.adapter.auto_commit()


async def test_cancelled_statement_reaches_caller_at_once_with_a_remembered_versioned_table(tmpdir_path):
    db = _db(tmpdir_path, acquire_timeout=20)
    await _setup(db)
    try:
        await db.ensure_table("vers", versioned=True)
        exc_type, elapsed = await _cancel_in_transaction(db)
        assert (exc_type, elapsed < CALLER_SEES_IT_S) == (TimeoutError, True), elapsed
        # the next versioned write works, once the connection is back
        rid, version = await asyncio.wait_for(db.upsert_with_version("vers", None, {"n": 1}, None), 15)
        assert version == 0
        cur = await db.adapter.execute("SELECT count(*) FROM t")
        assert (await cur.fetchone())[0] == 0  # the insert was rolled back
        await db.adapter.auto_commit()
    finally:
        await db.close()


async def test_cancelled_statement_reaches_caller_at_once_with_a_remembered_promoted_table(tmpdir_path):
    db = _db(tmpdir_path, acquire_timeout=20)
    await _setup(db)
    try:
        await RollPromo(name="kept", status="s").save(db=db)
        exc_type, elapsed = await _cancel_in_transaction(db)
        assert (exc_type, elapsed < CALLER_SEES_IT_S) == (TimeoutError, True), elapsed
        await asyncio.wait_for(RollPromo(name="next", status="n").save(db=db), 15)
        assert sorted(p.status for p in await RollPromo.using(db).all()) == ["n", "s"]
        assert db._promoted_columns["rollpromos"] == ["status"]
    finally:
        await db.close()


async def test_cancelled_statement_with_short_acquire_timeout_gives_the_callers_exception(tmpdir_path):
    db = _db(tmpdir_path, acquire_timeout=1)
    await _setup(db)
    try:
        await db.ensure_table("vers", versioned=True)
        await RollPromo(name="kept").save(db=db)
        exc_type, elapsed = await _cancel_in_transaction(db)
        assert (exc_type, elapsed < CALLER_SEES_IT_S) == (TimeoutError, True), elapsed
    finally:
        await db.close()


async def test_rollback_on_a_closed_adapter_gives_the_callers_exception(tmpdir_path):
    db = _db(tmpdir_path, acquire_timeout=20)
    await _setup(db)
    await db.ensure_table("vers", versioned=True)
    try:
        with pytest.raises(RuntimeError, match="mine"):
            async with db.transaction():
                await db.adapter.execute("INSERT INTO t (v) VALUES ('x')")
                await db.adapter.close()
                raise RuntimeError("mine")
        # the rollback opened no connection on the closed adapter
        assert db.adapter.pool_stats().total_connections == 0
    finally:
        await db.close()


async def test_promoted_table_created_and_rolled_back_is_created_again_by_the_next_save(tmpdir_path):
    db = _db(tmpdir_path, acquire_timeout=20)
    await _setup(db)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="undo"):
                async with db.transaction():
                    await RollPromo(name="gone").save(db=db)
                    raise RuntimeError("undo")
        await RollPromo(name="kept", status="x").save(db=db)
        assert [p.status for p in await RollPromo.using(db).all()] == ["x"]
    finally:
        await db.close()
