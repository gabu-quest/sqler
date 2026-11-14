"""The async twin of ``tests/test_db_reads_create_nothing.py``.

A read through ``AsyncSQLerDB`` creates no table, waits for no write lock on a pooled
connection that read the schema before the table existed, and on a missing table keeps the
uncommitted statements of the task that reads.
"""

import asyncio
import sqlite3
import time

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel

pytestmark = pytest.mark.asyncio


class AThing(AsyncSQLerModel):
    n: int = 0


@pytest_asyncio.fixture
async def path(tmp_path):
    return str(tmp_path / "areads.db")


@pytest_asyncio.fixture
async def db(path):
    d = AsyncSQLerDB.on_disk(path)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


def _tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def _rows(path, table):
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


async def test_reads_of_a_missing_table_give_empty_and_create_nothing(db, path):
    assert await db.find_document("ghost", 1) is None
    assert await db.find_documents("ghost", [1, 2]) == []
    assert await db.find_document_with_version("ghost", 1) is None
    q = await db.query("ghost")
    assert await q.count() == 0
    assert await q.all() == []
    assert await q.first() is None
    assert await q.exists() is False
    assert "ghost" not in _tables(path)
    assert db.pool_stats().in_use_connections == 0


async def test_a_write_after_a_read_of_a_missing_table_creates_it(db):
    assert await db.find_document("ghost", 1) is None
    rid = await db.insert_document("ghost", {"n": 1})
    assert await db.find_document("ghost", rid) == {"n": 1, "_id": rid}


async def test_a_table_dropped_by_another_connection_reads_as_missing(db, path):
    rid = await db.insert_document("gone", {"n": 1})
    assert await db.find_document("gone", rid) == {"n": 1, "_id": rid}
    other = sqlite3.connect(path)
    other.execute("DROP TABLE gone")
    other.commit()
    other.close()
    assert await db.find_document("gone", rid) is None
    assert await db.find_documents("gone", [rid]) == []
    assert await (await db.query("gone")).count() == 0


async def test_a_pooled_read_does_not_wait_for_the_write_lock_on_a_stale_schema(db, path):
    # the pool's other connections read the schema before this table existed
    rid = await db.insert_document("docs", {"n": 1})
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO docs (data) VALUES ('{\"n\": 2}')")
    try:
        start = time.perf_counter()
        for _ in range(4):  # every connection of the pool, once
            assert await asyncio.wait_for(db.find_document("docs", rid), 3) == {"n": 1, "_id": rid}
        assert await asyncio.wait_for(db.find_documents("docs", [rid]), 3) == [{"n": 1, "_id": rid}]
        assert await asyncio.wait_for(AThing.using(db, "docs").count(), 3) == 1
        waited = time.perf_counter() - start
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert waited < 1.0, f"the reads waited {waited:.2f}s"


READS = {
    "using_all": lambda d: AThing.using(d, "absent").all(),
    "using_count": lambda d: AThing.using(d, "absent").count(),
    "find_document": lambda d: d.find_document("absent", 1),
    "find_documents": lambda d: d.find_documents("absent", [1]),
}


@pytest.mark.parametrize("read", list(READS))
async def test_a_read_of_a_missing_table_keeps_uncommitted_work(db, path, read):
    await db.insert_document("kept", {"n": 1})
    cur = await db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")  # uncommitted
    await cur.close()
    await READS[read](db)
    await db.adapter.commit()
    await db.adapter.auto_commit()
    assert _rows(path, "kept") == 2
    assert db.pool_stats().in_use_connections == 0


async def test_a_write_that_fails_still_rolls_back_uncommitted_work(db, path):
    await db.insert_document("kept", {"n": 1})
    await db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await db.adapter.execute("INSERT INTO absent (data) VALUES ('{}')")
    assert _rows(path, "kept") == 1
