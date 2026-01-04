"""Tests for async bulk operations: find_documents, from_ids, count."""

import pytest
from sqler.db.async_db import AsyncSQLerDB
from sqler.models import AsyncSQLerModel


class AsyncBulkAddress(AsyncSQLerModel):
    city: str
    country: str


class AsyncBulkUserWithAddr(AsyncSQLerModel):
    name: str
    address: AsyncBulkAddress | None = None


@pytest.mark.asyncio
async def test_async_find_documents_batch(async_db: AsyncSQLerDB):
    """Test async db.find_documents() fetches multiple docs in one query."""

    class AsyncBulkUser(AsyncSQLerModel):
        name: str
        age: int

    AsyncBulkUser.set_db(async_db, "async_bulk_users")

    # Insert 5 users
    users = []
    for i in range(5):
        u = AsyncBulkUser(name=f"User{i}", age=20 + i)
        await u.save()
        users.append(u)
    ids = [u._id for u in users]

    # Fetch all in one batch
    docs = await async_db.find_documents("async_bulk_users", ids)
    assert len(docs) == 5
    assert [d["name"] for d in docs] == ["User0", "User1", "User2", "User3", "User4"]


@pytest.mark.asyncio
async def test_async_find_documents_preserves_order(async_db: AsyncSQLerDB):
    """Test that async find_documents returns docs in input order."""

    class AsyncBulkUser2(AsyncSQLerModel):
        name: str
        age: int

    AsyncBulkUser2.set_db(async_db, "async_bulk_users2")

    users = []
    for i in range(5):
        u = AsyncBulkUser2(name=f"User{i}", age=20 + i)
        await u.save()
        users.append(u)
    ids = [u._id for u in users]

    # Request in reverse order
    reversed_ids = list(reversed(ids))
    docs = await async_db.find_documents("async_bulk_users2", reversed_ids)
    assert [d["name"] for d in docs] == ["User4", "User3", "User2", "User1", "User0"]


@pytest.mark.asyncio
async def test_async_find_documents_skips_missing(async_db: AsyncSQLerDB):
    """Test that async find_documents skips missing ids."""

    class AsyncBulkUser3(AsyncSQLerModel):
        name: str
        age: int

    AsyncBulkUser3.set_db(async_db, "async_bulk_users3")

    users = []
    for i in range(3):
        u = AsyncBulkUser3(name=f"User{i}", age=20 + i)
        await u.save()
        users.append(u)
    ids = [users[0]._id, 9999, users[2]._id]  # 9999 doesn't exist

    docs = await async_db.find_documents("async_bulk_users3", ids)
    assert len(docs) == 2
    assert [d["name"] for d in docs] == ["User0", "User2"]


@pytest.mark.asyncio
async def test_async_find_documents_empty_list(async_db: AsyncSQLerDB):
    """Test async find_documents with empty list returns empty."""
    docs = await async_db.find_documents("nonexistent", [])
    assert docs == []


@pytest.mark.asyncio
async def test_async_from_ids_batch(async_db: AsyncSQLerDB):
    """Test async Model.from_ids() hydrates multiple instances."""

    class AsyncBulkUser4(AsyncSQLerModel):
        name: str
        age: int

    AsyncBulkUser4.set_db(async_db, "async_bulk_users4")

    users = []
    for i in range(5):
        u = AsyncBulkUser4(name=f"User{i}", age=20 + i)
        await u.save()
        users.append(u)
    ids = [u._id for u in users]

    # Fetch all in one batch
    hydrated = await AsyncBulkUser4.from_ids(ids)
    assert len(hydrated) == 5
    assert all(isinstance(u, AsyncBulkUser4) for u in hydrated)
    assert [u.name for u in hydrated] == ["User0", "User1", "User2", "User3", "User4"]


@pytest.mark.asyncio
async def test_async_from_ids_with_relations(async_db: AsyncSQLerDB):
    """Test async from_ids resolves nested relations."""
    AsyncBulkAddress.set_db(async_db, "async_bulk_addresses")
    AsyncBulkUserWithAddr.set_db(async_db, "async_bulk_users_addr")

    addr1 = AsyncBulkAddress(city="Tokyo", country="Japan")
    await addr1.save()
    addr2 = AsyncBulkAddress(city="Paris", country="France")
    await addr2.save()

    u1 = AsyncBulkUserWithAddr(name="Alice", address=addr1)
    await u1.save()
    u2 = AsyncBulkUserWithAddr(name="Bob", address=addr2)
    await u2.save()
    u3 = AsyncBulkUserWithAddr(name="Charlie", address=None)
    await u3.save()

    hydrated = await AsyncBulkUserWithAddr.from_ids([u1._id, u2._id, u3._id])
    assert len(hydrated) == 3
    assert hydrated[0].address.city == "Tokyo"
    assert hydrated[1].address.city == "Paris"
    assert hydrated[2].address is None


@pytest.mark.asyncio
async def test_async_from_ids_empty_list(async_db: AsyncSQLerDB):
    """Test async from_ids with empty list returns empty."""

    class AsyncBulkUser5(AsyncSQLerModel):
        name: str
        age: int

    AsyncBulkUser5.set_db(async_db, "async_bulk_users5")
    result = await AsyncBulkUser5.from_ids([])
    assert result == []


@pytest.mark.asyncio
async def test_async_from_ids_preserves_order(async_db: AsyncSQLerDB):
    """Test async from_ids preserves input order."""

    class AsyncBulkUser6(AsyncSQLerModel):
        name: str
        age: int

    AsyncBulkUser6.set_db(async_db, "async_bulk_users6")

    users = []
    for i in range(5):
        u = AsyncBulkUser6(name=f"User{i}", age=20 + i)
        await u.save()
        users.append(u)

    ids = [users[4]._id, users[0]._id, users[2]._id]

    hydrated = await AsyncBulkUser6.from_ids(ids)
    assert [u.name for u in hydrated] == ["User4", "User0", "User2"]


@pytest.mark.asyncio
async def test_async_model_count_shortcut(async_db: AsyncSQLerDB):
    """Test async Model.count() shortcut."""

    class AsyncBulkUser7(AsyncSQLerModel):
        name: str
        age: int

    AsyncBulkUser7.set_db(async_db, "async_bulk_users7")

    assert await AsyncBulkUser7.count() == 0

    for i in range(10):
        u = AsyncBulkUser7(name=f"User{i}", age=20 + i)
        await u.save()

    assert await AsyncBulkUser7.count() == 10
