"""``AsyncSQLerDB`` factories hand on ``acquire_timeout``; ``AsyncAdapterABC`` declares ``pool_stats``."""

import asyncio
import time

import pytest

from sqler import AsyncSQLerDB
from sqler.adapter.abstract import AsyncAdapterABC
from sqler.adapter.asynchronous import DEFAULT_ACQUIRE_TIMEOUT_S
from sqler.exceptions import ConnectionPoolExhaustedError


def test_defaults_match_the_adapter(tmp_path):
    assert AsyncSQLerDB.in_memory().adapter.acquire_timeout == DEFAULT_ACQUIRE_TIMEOUT_S == 30.0
    assert AsyncSQLerDB.on_disk(str(tmp_path / "d.db")).adapter.acquire_timeout == 30.0


def test_factories_hand_it_on(tmp_path):
    assert AsyncSQLerDB.in_memory(acquire_timeout=0.25).adapter.acquire_timeout == 0.25
    assert AsyncSQLerDB.in_memory(False, name=None, acquire_timeout=None).adapter.acquire_timeout is None
    assert AsyncSQLerDB.on_disk(str(tmp_path / "d.db"), acquire_timeout=2.0).adapter.acquire_timeout == 2.0


@pytest.mark.asyncio
async def test_on_disk_wait_runs_out_within_a_second(tmp_path):
    db = AsyncSQLerDB.on_disk(str(tmp_path / "d.db"), acquire_timeout=0.05)
    await db.connect()
    held: list = []
    try:
        # hold every connection of the pool (four on disk) in other tasks
        size = db.pool_stats().max_connections
        assert size == 4
        release = asyncio.Event()
        started = 0
        all_held = asyncio.Event()

        async def hold():
            # a statement whose commit has not run keeps its connection checked out
            nonlocal started
            cur = await db.adapter.execute("SELECT 1")
            await cur.close()
            started += 1
            if started == size:
                all_held.set()
            await release.wait()
            await db.adapter.auto_commit()

        held = [asyncio.create_task(hold()) for _ in range(size)]
        await asyncio.wait_for(all_held.wait(), 5)
        start = time.perf_counter()
        with pytest.raises(ConnectionPoolExhaustedError):
            await db.insert_document("t", {"a": 1})
        assert time.perf_counter() - start < 1.0
        release.set()
        await asyncio.gather(*held)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_in_memory_wait_runs_out(tmp_path):
    db = AsyncSQLerDB.in_memory(shared=False, acquire_timeout=0.05)
    await db.connect()
    release = asyncio.Event()
    holding = asyncio.Event()

    async def hold():
        async with db.transaction():
            holding.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(holding.wait(), 5)
    with pytest.raises(ConnectionPoolExhaustedError):
        await db.insert_document("t", {"a": 1})
    release.set()
    await task
    await db.close()


class _Minimal(AsyncAdapterABC):
    """An adapter written outside sqler, with no ``pool_stats``."""

    async def connect(self):
        pass

    async def close(self):
        pass

    async def execute(self, query, params=None):
        return None

    async def executemany(self, query, param_list):
        return None

    async def executescript(self, script):
        return None

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass


def test_adapter_without_pool_stats_instantiates_and_says_so():
    adapter = _Minimal()
    with pytest.raises(NotImplementedError, match=r"^_Minimal does not implement pool_stats\(\)$"):
        adapter.pool_stats()


def test_abc_declares_pool_stats_not_abstract():
    assert "pool_stats" in vars(AsyncAdapterABC)
    assert "pool_stats" not in AsyncAdapterABC.__abstractmethods__
