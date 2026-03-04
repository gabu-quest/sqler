"""Tests for AsyncSQLiteAdapter connection pool (BUG-1 fix).

Verifies that concurrent coroutines can operate on the same on-disk DB
without colliding on commit.
"""

import asyncio
import os
import tempfile

import pytest
from sqler import AsyncSQLerModel
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.query import SQLerField as F


class PoolItem(AsyncSQLerModel):
    value: int


# ---- BUG-1 core: concurrent writes on on-disk DB ----


@pytest.mark.asyncio
async def test_concurrent_writes_on_disk_no_operational_error():
    """50 coroutines each save a row concurrently on on-disk DB with pool_size=4.

    This is the core BUG-1 regression test. The old single-connection adapter
    crashes with 'cannot commit transaction - SQL statements in progress'.
    """
    n = 50
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "pool_test.db")
        db = AsyncSQLerDB.on_disk(db_path)
        await db.connect()
        PoolItem.set_db(db)
        try:
            await PoolItem._ensure_schema()

            async def insert_one(i: int):
                item = PoolItem(value=i)
                await item.save()
                return item._id

            ids = await asyncio.gather(*[insert_one(i) for i in range(n)])

            assert len(ids) == n
            assert all(isinstance(i, int) and i > 0 for i in ids)
            assert len(set(ids)) == n
            count = await PoolItem.query().count()
            assert count == n
        finally:
            PoolItem.set_db(None)
            await db.close()


@pytest.mark.asyncio
async def test_concurrent_mixed_operations():
    """Concurrent inserts, reads, updates, and deletes on the same on-disk DB.

    Exercises pool contention across different operation types — the scenario
    that actually crashes the single-connection adapter.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "mixed_test.db")
        db = AsyncSQLerDB.on_disk(db_path)
        await db.connect()
        PoolItem.set_db(db)
        try:
            await PoolItem._ensure_schema()

            # Seed 20 rows sequentially
            seed_ids = []
            for i in range(20):
                item = PoolItem(value=i)
                await item.save()
                seed_ids.append(item._id)

            errors: list[Exception] = []

            async def do_insert(i: int):
                try:
                    item = PoolItem(value=1000 + i)
                    await item.save()
                except Exception as e:
                    errors.append(e)

            async def do_read(id_: int):
                try:
                    await PoolItem.from_id(id_)
                except Exception as e:
                    errors.append(e)

            async def do_query():
                try:
                    await PoolItem.query().filter(F("value") >= 0).count()
                except Exception as e:
                    errors.append(e)

            async def do_update(id_: int, new_val: int):
                try:
                    await PoolItem.query().filter(F("_id") == id_).update(value=new_val)
                except Exception as e:
                    errors.append(e)

            # Fire 50 mixed operations concurrently
            tasks = []
            for i in range(15):
                tasks.append(do_insert(i))
            for id_ in seed_ids[:10]:
                tasks.append(do_read(id_))
            for _ in range(15):
                tasks.append(do_query())
            for i, id_ in enumerate(seed_ids[:10]):
                tasks.append(do_update(id_, 5000 + i))

            await asyncio.gather(*tasks)

            assert len(errors) == 0, f"Concurrent ops raised errors: {errors}"

            # Verify: 20 seed + 15 new inserts = 35 total
            total = await PoolItem.query().count()
            assert total == 35
        finally:
            PoolItem.set_db(None)
            await db.close()


# ---- pool_size=1 still serializes correctly ----


@pytest.mark.asyncio
async def test_pool_size_one_serializes():
    """In-memory adapter (pool_size=1) still works with sequential access."""
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    PoolItem.set_db(db)
    try:
        await PoolItem._ensure_schema()
        for i in range(5):
            item = PoolItem(value=i)
            await item.save()
        count = await PoolItem.query().count()
        assert count == 5
    finally:
        PoolItem.set_db(None)
        await db.close()


# ---- transaction isolation ----


@pytest.mark.asyncio
async def test_transaction_pins_connection():
    """An explicit transaction pins one connection; concurrent non-txn ops proceed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "txn_test.db")
        db = AsyncSQLerDB.on_disk(db_path)
        await db.connect()
        PoolItem.set_db(db)
        try:
            await PoolItem._ensure_schema()

            async with db.transaction():
                item = PoolItem(value=100)
                await item.save()

            found = await PoolItem.query().filter(F("value") == 100).all()
            assert len(found) == 1
            assert found[0].value == 100
        finally:
            PoolItem.set_db(None)
            await db.close()


@pytest.mark.asyncio
async def test_transaction_rollback_on_exception():
    """Transaction rolls back on exception; no row persisted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "rollback_test.db")
        db = AsyncSQLerDB.on_disk(db_path)
        await db.connect()
        PoolItem.set_db(db)
        try:
            await PoolItem._ensure_schema()

            with pytest.raises(RuntimeError, match="forced rollback"):
                async with db.transaction():
                    item = PoolItem(value=999)
                    await item.save()
                    raise RuntimeError("forced rollback")

            count = await PoolItem.query().filter(F("value") == 999).count()
            assert count == 0
        finally:
            PoolItem.set_db(None)
            await db.close()


@pytest.mark.asyncio
async def test_concurrent_transactions_and_standalone_ops():
    """Multiple transactions compete with standalone ops for pool connections.

    pool_size=4: two transactions each pin a connection, while 20 standalone
    inserts compete for the remaining 2 pool slots.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "txn_concurrent_test.db")
        db = AsyncSQLerDB.on_disk(db_path)
        await db.connect()
        PoolItem.set_db(db)
        try:
            await PoolItem._ensure_schema()

            async def transactional_insert(start: int, count: int):
                async with db.transaction():
                    for i in range(start, start + count):
                        item = PoolItem(value=i)
                        await item.save()

            async def standalone_insert(val: int):
                item = PoolItem(value=val)
                await item.save()

            tasks = [
                transactional_insert(0, 5),
                transactional_insert(100, 5),
                *[standalone_insert(200 + i) for i in range(20)],
            ]
            await asyncio.gather(*tasks)

            total = await PoolItem.query().count()
            assert total == 30  # 5 + 5 + 20
        finally:
            PoolItem.set_db(None)
            await db.close()


# ---- pool exhaustion: more tasks than pool_size ----


@pytest.mark.asyncio
async def test_pool_exhaustion_blocks_then_completes():
    """pool_size=2, 30 concurrent inserts: tasks queue for connections, all complete."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "exhaust_test.db")
        adapter = AsyncSQLiteAdapter(db_path, pragmas=[
            "PRAGMA foreign_keys = ON",
            "PRAGMA busy_timeout = 5000",
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA temp_store = MEMORY",
        ], pool_size=2)
        db = AsyncSQLerDB(adapter)
        await db.connect()
        PoolItem.set_db(db)
        try:
            await PoolItem._ensure_schema()

            async def insert_one(i: int):
                item = PoolItem(value=i)
                await item.save()
                return item._id

            ids = await asyncio.gather(*[insert_one(i) for i in range(30)])

            assert len(ids) == 30
            assert all(isinstance(i, int) and i > 0 for i in ids)
            assert len(set(ids)) == 30
            count = await PoolItem.query().count()
            assert count == 30
        finally:
            PoolItem.set_db(None)
            await db.close()


# ---- release suppressed during transaction ----


@pytest.mark.asyncio
async def test_release_suppressed_during_transaction():
    """_release() is a no-op while _txn_depth > 0 (connection stays pinned)."""
    adapter = AsyncSQLiteAdapter.in_memory(shared=False)
    await adapter.connect()
    try:
        await adapter.begin_transaction()
        conn = adapter.connection
        assert conn is not None

        await adapter._release()
        assert adapter.connection is conn  # still pinned

        await adapter.end_transaction(commit=True)
        assert adapter.connection is None
    finally:
        await adapter.close()


# ---- adapter-level pool basics ----


@pytest.mark.asyncio
async def test_adapter_pool_size_matches_config():
    """on_disk factory creates adapter with pool_size=4."""
    adapter = AsyncSQLiteAdapter.on_disk("/tmp/_sqler_pool_test_unused.db")
    assert adapter._pool_size == 4


@pytest.mark.asyncio
async def test_adapter_in_memory_pool_size_one():
    """in_memory factory creates adapter with pool_size=1."""
    adapter = AsyncSQLiteAdapter.in_memory()
    assert adapter._pool_size == 1


@pytest.mark.asyncio
async def test_connection_property_backward_compat():
    """adapter.connection returns the task-pinned connection (or None)."""
    adapter = AsyncSQLiteAdapter.in_memory(shared=False)
    await adapter.connect()
    try:
        assert adapter.connection is None
        conn = await adapter._acquire()
        assert adapter.connection is conn
        await adapter._release()
        assert adapter.connection is None
    finally:
        await adapter.close()
