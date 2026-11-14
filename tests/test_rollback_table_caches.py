"""A table created inside a transaction that rolls back is not remembered as existing.

``ensure_table`` remembers a table's promoted columns and its ``_version`` column. After a
rollback removed the table, the next promoted save and the next versioned write create it
again and work; a table that still exists keeps what is remembered about it.
"""

import pytest
from sqler import AsyncSQLerDB, AsyncSQLerModel, SQLerDB, SQLerModel


class Promo(SQLerModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


class APromo(AsyncSQLerModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


def test_promoted_save_works_after_the_creating_transaction_rolled_back_twice():
    db = SQLerDB.in_memory(shared=False)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="undo"):
                with db.transaction():
                    Promo(name="gone").save(db=db)
                    raise RuntimeError("undo")
        saved = Promo(name="kept", status="x").save(db=db)
        assert Promo.using(db).count() == 1
        assert [p.status for p in Promo.using(db).all()] == ["x"]
    finally:
        db.close()


def test_versioned_write_works_after_the_creating_transaction_rolled_back():
    db = SQLerDB.in_memory(shared=False)
    try:
        with pytest.raises(RuntimeError, match="undo"):
            with db.transaction():
                db.ensure_table("vers", versioned=True)
                raise RuntimeError("undo")
        rid, version = db.upsert_with_version("vers", None, {"n": 1}, None)
        assert version == 0
        assert db.find_document_with_version("vers", rid)["_version"] == 0
    finally:
        db.close()


def test_a_table_that_still_exists_keeps_its_promoted_columns():
    db = SQLerDB.in_memory(shared=False)
    try:
        Promo(name="a", status="s").save(db=db)
        with pytest.raises(RuntimeError, match="undo"):
            with db.transaction():
                Promo(name="b").save(db=db)
                raise RuntimeError("undo")
        assert db._promoted_columns["promos"] == ["status"]
        assert [p.status for p in Promo.using(db).all()] == ["s"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_promoted_save_works_after_the_creating_transaction_rolled_back_twice():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="undo"):
                async with db.transaction():
                    await APromo(name="gone").save(db=db)
                    raise RuntimeError("undo")
        saved = await APromo(name="kept", status="x").save(db=db)
        assert await APromo.using(db).count() == 1
        assert [p.status for p in await APromo.using(db).all()] == ["x"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_async_versioned_write_works_after_the_creating_transaction_rolled_back():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        with pytest.raises(RuntimeError, match="undo"):
            async with db.transaction():
                await db.ensure_table("vers", versioned=True)
                raise RuntimeError("undo")
        rid, version = await db.upsert_with_version("vers", None, {"n": 1}, None)
        assert version == 0
        assert (await db.find_document_with_version("vers", rid))["_version"] == 0
    finally:
        await db.close()
