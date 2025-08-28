import asyncio

import pytest_asyncio
from sqler.adapter import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB


# On Windows, an explicit loop prevents weird policy hiccups.
@pytest_asyncio.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def async_adapter():
    adapter = AsyncSQLiteAdapter.in_memory(shared=False)
    await adapter.connect()
    try:
        yield adapter
    finally:
        await adapter.close()


@pytest_asyncio.fixture
async def async_db():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
