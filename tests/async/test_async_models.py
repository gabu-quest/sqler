import pytest
from sqler import AsyncSQLerModel
from sqler.db.async_db import AsyncSQLerDB
from sqler.query import SQLerField as F


class AUser(AsyncSQLerModel):
    name: str
    age: int


@pytest.mark.asyncio
async def test_async_model_crud_and_query(async_db):
    AUser.set_db(async_db)
    try:
        u = AUser(name="Alice", age=30)
        await u.save()
        assert isinstance(u._id, int)
        assert u._id > 0

        u2 = await AUser.from_id(u._id)
        assert u2 is not None
        assert u2._id == u._id
        assert u2.name == "Alice"
        assert u2.age == 30

        adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
        assert [a.name for a in adults] == ["Alice"]

        original_id = u._id
        u.age = 31
        await u.save()
        u.age = 0
        u.name = "CORRUPTED"
        await u.refresh()
        assert u._id == original_id
        assert u.age == 31
        assert u.name == "Alice"
    finally:
        # Important if the class keeps a reference
        AUser.set_db(None)


# ---- BUG-2: using() method ----


class UsingItem(AsyncSQLerModel):
    label: str


@pytest.mark.asyncio
async def test_using_returns_queryset_bound_to_specific_db():
    """using() queries the correct DB without mutating class state."""
    db1 = AsyncSQLerDB.in_memory(shared=True, name="using_db1")
    db2 = AsyncSQLerDB.in_memory(shared=True, name="using_db2")
    await db1.connect()
    await db2.connect()
    try:
        # Bind to db1 for save operations
        UsingItem.set_db(db1)
        await UsingItem._ensure_schema()

        a = UsingItem(label="from_db1")
        await a.save()

        # Bind to db2 for save operations
        UsingItem.set_db(db2)
        await UsingItem._ensure_schema()

        b = UsingItem(label="from_db2")
        await b.save()

        # Query each DB independently via using()
        items_db1 = await UsingItem.using(db1).all()
        items_db2 = await UsingItem.using(db2).all()

        assert len(items_db1) == 1
        assert items_db1[0].label == "from_db1"
        assert len(items_db2) == 1
        assert items_db2[0].label == "from_db2"
    finally:
        UsingItem.set_db(None)
        await db1.close()
        await db2.close()


@pytest.mark.asyncio
async def test_using_does_not_mutate_class_db():
    """using() doesn't change cls._db."""
    db1 = AsyncSQLerDB.in_memory(shared=True, name="nomutate_db1")
    db2 = AsyncSQLerDB.in_memory(shared=True, name="nomutate_db2")
    await db1.connect()
    await db2.connect()
    try:
        UsingItem.set_db(db1)
        await UsingItem._ensure_schema()

        original_db = UsingItem._db

        # Call using() with a different DB
        _qs = UsingItem.using(db2)

        # Class-level _db should be unchanged
        assert UsingItem._db is original_db
        assert UsingItem._db is db1
    finally:
        UsingItem.set_db(None)
        await db1.close()
        await db2.close()
