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
