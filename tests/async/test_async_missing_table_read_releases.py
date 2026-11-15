"""A read of a missing table through a database method gives an idle connection back.

A task that holds its connection with nothing uncommitted (it ran a read and has not ended it)
and then reads a table that does not exist through ``find_document`` / ``find_documents`` ends
holding no connection, as after a read of an existing table. A task with uncommitted work keeps
its connection and the work. Pools of one, so a connection kept by mistake blocks the next
task at once; every wait is bounded.
"""

import asyncio
import contextvars
import os
import tempfile

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB
from sqler.adapter.asynchronous import AsyncSQLiteAdapter

OTHER_TASK_BOUND_S = 2.0
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        pragmas = ["PRAGMA busy_timeout = 5000", "PRAGMA journal_mode = WAL"]
        ad = AsyncSQLiteAdapter(os.path.join(tmpdir, "f.db"), pragmas=pragmas, pool_size=1, acquire_timeout=20)
        database = AsyncSQLerDB(ad)
        await database.connect()
        await ad.execute("CREATE TABLE t (v TEXT)")
        await ad.execute("INSERT INTO t (v) VALUES ('a')")
        await ad.auto_commit()
        try:
            yield database
        finally:
            await database.close()


async def _other_task_reads(db: AsyncSQLerDB) -> int:
    async def read() -> int:
        cur = await db.adapter.execute("SELECT count(*) FROM t")
        n = (await cur.fetchone())[0]
        await db.adapter.auto_commit()
        return n

    # a task of its own, pinned to nothing: it does not share this task's connection
    other = asyncio.create_task(read(), context=contextvars.Context())
    return await asyncio.wait_for(other, OTHER_TASK_BOUND_S)


@pytest.mark.parametrize(
    ("read_missing", "empty"),
    [
        (lambda db: db.find_document("missing", 1), None),
        (lambda db: db.find_documents("missing", [1, 2]), []),
    ],
    ids=["find_document", "find_documents"],
)
async def test_idle_pinned_task_reading_a_missing_table_gives_its_connection_back(db, read_missing, empty):
    cur = await db.adapter.execute("SELECT v FROM t")  # pins the connection, nothing uncommitted
    assert [tuple(r) for r in await cur.fetchall()] == [("a",)]
    assert await read_missing(db) == empty
    held = db.adapter._pinned() is not None
    # while this task is still running, another task gets the only connection
    assert (held, await _other_task_reads(db)) == (False, 1)


async def test_task_with_uncommitted_work_keeps_its_connection_and_the_work(db):
    await db.adapter.execute("INSERT INTO t (v) VALUES ('b')")  # uncommitted
    assert await db.find_document("missing", 1) is None
    assert db.adapter._pinned() is not None
    await db.adapter.auto_commit()
    assert await _other_task_reads(db) == 2
