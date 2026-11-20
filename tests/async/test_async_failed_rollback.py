"""The async adapter when the rollback at the end of a block fails.

A progress handler on the block's connection stops every statement from the moment it is
set, the rollback at the end of the block too. The connection is closed and a fresh one
takes its place in the pool; the block's own exception leaves as it is, and the row the
block wrote is gone.
"""

import logging
import sqlite3

import pytest

from sqler.db.async_db import AsyncSQLerDB

REPLACED = "rollback of a reclaimed connection failed (OperationalError: interrupted); replacing it"


def _committed(path) -> list[str]:
    """The values of ``t`` as another connection sees them."""
    raw = sqlite3.connect(path)
    try:
        return sorted(row[0] for row in raw.execute("SELECT json_extract(data, '$.n') FROM t"))
    finally:
        raw.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["with-db", "transaction"])
async def test_a_block_whose_rollback_fails_replaces_its_connection(tmp_path, caplog, block):
    path = str(tmp_path / "failed_rollback.db")
    db = AsyncSQLerDB.on_disk(path)
    await db.connect()
    try:
        await db.insert_document("t", {"n": "before"})
        total = db.adapter.pool_stats().total_connections
        caplog.set_level(logging.WARNING, logger="sqler.adapter.asynchronous")
        with pytest.raises(ValueError) as raised:
            async with db if block == "with-db" else db.transaction():
                await db.insert_document("t", {"n": "in the block"})
                old = db.adapter.connection
                await old.set_progress_handler(lambda: 1, 1)
                raise ValueError("the block's own error")
        assert (str(raised.value), getattr(raised.value, "__notes__", [])) == (
            "the block's own error",
            [],
        )
        assert _committed(path) == ["before"]
        assert [r.getMessage() for r in caplog.records] == [REPLACED]
        pooled = list(db.adapter._pool._queue)
        assert (
            db.adapter.pool_stats().total_connections,
            old in db.adapter._all_conns,
            old in pooled,
        ) == (
            total,
            False,
            False,
        )
        await db.insert_document("t", {"n": "after the block"})
        assert _committed(path) == ["after the block", "before"]
    finally:
        await db.close()
