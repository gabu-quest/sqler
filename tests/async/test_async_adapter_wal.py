import pytest

from sqler.adapter import AsyncSQLiteAdapter


@pytest.mark.asyncio
async def test_on_disk_uses_wal_mode(tmp_path):
    db_path = tmp_path / "wal_test.db"
    adapter = AsyncSQLiteAdapter.on_disk(str(db_path))
    await adapter.connect()
    try:
        async with await adapter.execute("PRAGMA journal_mode;") as cur:
            row = await cur.fetchone()
        assert str(row[0]).lower() == "wal"
    finally:
        await adapter.close()
