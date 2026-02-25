import pytest
from sqler.query import SQLerField as F
from sqler.query.async_query import AsyncSQLerQuery


@pytest.mark.asyncio
async def test_async_adapter_connect_execute(async_adapter):  # use fixture
    await async_adapter.execute("CREATE TABLE t (_id INTEGER PRIMARY KEY, data JSON NOT NULL)")
    await async_adapter.execute("INSERT INTO t (data) VALUES (json(?))", ['{"a":1}'])
    await async_adapter.commit()

    async with await async_adapter.execute("SELECT json_extract(data,'$.a') FROM t") as cur:
        row = await cur.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_async_db_insert_find_and_query(async_db):  # use fixture
    _id = await async_db.insert_document("users", {"name": "Ada", "age": 36})
    doc = await async_db.find_document("users", _id)
    assert doc["name"] == "Ada"

    q = AsyncSQLerQuery("users", adapter=async_db.adapter).filter(F("age") >= 30)
    rows = await q.all_dicts()
    assert rows and rows[0]["name"] == "Ada"


@pytest.mark.asyncio
async def test_async_transaction_rollback(async_db):
    """Test that async transactions properly rollback on error."""
    # Insert one document that should persist
    await async_db.insert_document("items", {"name": "persisted"})

    # Try a transaction that will fail
    try:
        async with async_db.transaction():
            await async_db.insert_document("items", {"name": "should_rollback_1"})
            await async_db.insert_document("items", {"name": "should_rollback_2"})
            raise RuntimeError("Simulated failure")
    except RuntimeError:
        pass

    # Verify only the first insert persisted
    q = AsyncSQLerQuery("items", adapter=async_db.adapter)
    rows = await q.all_dicts()
    assert len(rows) == 1
    assert rows[0]["name"] == "persisted"


@pytest.mark.asyncio
async def test_async_transaction_commit(async_db):
    """Test that async transactions properly commit on success."""
    async with async_db.transaction():
        await async_db.insert_document("items", {"name": "item1"})
        await async_db.insert_document("items", {"name": "item2"})

    # Verify both inserts committed
    q = AsyncSQLerQuery("items", adapter=async_db.adapter)
    rows = await q.all_dicts()
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"item1", "item2"}


@pytest.mark.asyncio
async def test_pragma_report_in_memory(async_adapter):
    """pragma_report returns expected keys with in-memory values."""
    report = await async_adapter.pragma_report()
    assert isinstance(report, dict)
    expected_keys = {
        "foreign_keys", "busy_timeout", "journal_mode", "synchronous",
        "cache_size", "wal_autocheckpoint", "mmap_size", "temp_store",
    }
    assert set(report.keys()) == expected_keys
    assert report["foreign_keys"] == 1
    assert report["journal_mode"] == "memory"
    assert report["temp_store"] == 2  # MEMORY = 2


@pytest.mark.asyncio
async def test_async_nested_transaction_rollback(async_db):
    """Test that async nested transaction rollback only affects inner scope.

    With SAVEPOINT support, inner transaction changes are rolled back
    while outer transaction continues normally.
    """
    async with async_db.transaction():
        await async_db.insert_document("items", {"name": "outer1", "value": 1})

        try:
            async with async_db.transaction():
                await async_db.insert_document("items", {"name": "inner", "value": 2})
                raise RuntimeError("Inner error")
        except RuntimeError:
            pass  # Inner rollback happens, outer continues

        await async_db.insert_document("items", {"name": "outer2", "value": 3})

    # Inner item should be rolled back, only outer items remain
    q = AsyncSQLerQuery("items", adapter=async_db.adapter).order_by("value")
    rows = await q.all_dicts()
    assert len(rows) == 2
    assert [r["name"] for r in rows] == ["outer1", "outer2"]
