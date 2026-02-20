"""Tests for AsyncSQLiteAdapter connection pool (BUG-1 fix).

Verifies that concurrent coroutines can operate on the same on-disk DB
without colliding on commit.
"""

import asyncio
import tempfile
import os

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
    """N coroutines each save a row concurrently. No OperationalError, all rows persisted."""
    n = 20
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
            assert len(set(ids)) == n  # all unique IDs
            count = await PoolItem.query().count()
            assert count == n
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

            # Start a transaction, insert inside it
            async with db.transaction():
                item = PoolItem(value=100)
                await item.save()

            # After transaction commits, the row is visible
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

            # Row should not be persisted after rollback
            count = await PoolItem.query().filter(F("value") == 999).count()
            assert count == 0
        finally:
            PoolItem.set_db(None)
            await db.close()


# ---- pool exhaustion: more tasks than pool_size ----


@pytest.mark.asyncio
async def test_pool_exhaustion_blocks_then_completes():
    """pool_size=2, 5 concurrent inserts: all block-and-complete without crash."""
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

            ids = await asyncio.gather(*[insert_one(i) for i in range(5)])

            assert len(ids) == 5
            assert all(isinstance(i, int) and i > 0 for i in ids)
            assert len(set(ids)) == 5
            count = await PoolItem.query().count()
            assert count == 5
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

        # Calling _release inside a transaction should NOT return the connection
        await adapter._release()
        assert adapter.connection is conn  # still pinned

        await adapter.end_transaction(commit=True)
        # After end_transaction, connection is released
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
        # Before any operation, no task-pinned connection
        assert adapter.connection is None
        # After acquire, it should be set
        conn = await adapter._acquire()
        assert adapter.connection is conn
        # After release, back to None
        await adapter._release()
        assert adapter.connection is None
    finally:
        await adapter.close()
