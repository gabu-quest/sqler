"""``save_many()`` of the msgspec bases: each case is a row over the sync base
(``SQLerMsgspecModel`` on a ``SQLerDB``) and the async one (``AsyncSQLerMsgspecModel`` on an
``AsyncSQLerDB``), and the two answer alike."""

import inspect
import sqlite3
import warnings

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, SQLerDB
from sqler.models.msgspec.async_model import AsyncSQLerMsgspecModel
from sqler.models.msgspec.model import SQLerMsgspecModel

# ---- test models: one of each on both bases ----

PROMOTED = {
    "status": "TEXT NOT NULL DEFAULT 'active'",
    "priority": "INTEGER DEFAULT 0",
}
CHECKS = {"valid_status": "status IN ('active', 'inactive', 'archived')"}


class MItem(SQLerMsgspecModel):
    __tablename__ = "msgspec_items"
    name: str = ""
    value: int = 0


class MPromotedItem(SQLerMsgspecModel):
    __tablename__ = "msgspec_promoted_items"
    __promoted__ = PROMOTED
    __checks__ = CHECKS
    name: str = ""
    status: str = "active"
    priority: int = 0


class AMItem(AsyncSQLerMsgspecModel):
    __tablename__ = "msgspec_items"
    name: str = ""
    value: int = 0


class AMPromotedItem(AsyncSQLerMsgspecModel):
    __tablename__ = "msgspec_promoted_items"
    __promoted__ = PROMOTED
    __checks__ = CHECKS
    name: str = ""
    status: str = "active"
    priority: int = 0


KINDS = {"sync": (MItem, MPromotedItem), "async": (AMItem, AMPromotedItem)}


async def aw(value):
    return await value if inspect.isawaitable(value) else value


@pytest_asyncio.fixture(params=list(KINDS))
async def kit(request):
    """``(item class, promoted class, database)`` of one base, the classes bound to the
    database for the length of the test."""
    item, promoted = KINDS[request.param]
    if request.param == "async":
        db = AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
    else:
        db = SQLerDB.in_memory(shared=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        item.set_db(db)
        promoted.set_db(db)
    try:
        yield item, promoted, db
    finally:
        for cls in (item, promoted):
            cls._db = None
            cls._table = None
        await aw(db.close())


# ---- save_many on both bases ----


@pytest.mark.asyncio
async def test_save_many_basic(kit):
    item, _, _ = kit
    items = [item(name=f"item-{i}", value=i) for i in range(5)]
    result = await aw(item.save_many(items))

    assert result is items
    assert [i._id for i in result] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_save_many_empty_list(kit):
    item, _, _ = kit
    assert await aw(item.save_many([])) == []


@pytest.mark.asyncio
async def test_save_many_rejects_existing(kit):
    item, _, _ = kit
    existing = await aw(item(name="existing").save())
    assert existing._id == 1

    with pytest.raises(ValueError, match="only accepts new instances"):
        await aw(item.save_many([existing]))


@pytest.mark.asyncio
async def test_save_many_with_promoted(kit):
    _, promoted, _ = kit
    items = [
        promoted(name="a", status="active", priority=1),
        promoted(name="b", status="inactive", priority=2),
        promoted(name="c", status="archived", priority=3),
    ]
    await aw(promoted.save_many(items))

    assert [i._id for i in items] == [1, 2, 3]
    found = await aw(promoted.query().order_by("priority").all())
    assert [(f.name, f.status, f.priority) for f in found] == [
        ("a", "active", 1),
        ("b", "inactive", 2),
        ("c", "archived", 3),
    ]


@pytest.mark.asyncio
async def test_save_many_atomicity_promoted(kit):
    """If one row violates a CHECK constraint, no rows should be saved."""
    _, promoted, _ = kit
    items = [
        promoted(name="good", status="active", priority=1),
        promoted(name="bad", status="INVALID_STATUS", priority=2),
    ]
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await aw(promoted.save_many(items))

    assert await aw(promoted.query().count()) == 0
    assert [i._id for i in items] == [None, None]


@pytest.mark.asyncio
async def test_save_many_roundtrip(kit):
    item, _, _ = kit
    items = [item(name=f"rt-{i}", value=i * 10) for i in range(3)]
    await aw(item.save_many(items))

    loaded = [await aw(item.from_id(i._id)) for i in items]
    assert [(m._id, m.name, m.value) for m in loaded] == [(1, "rt-0", 0), (2, "rt-1", 10), (3, "rt-2", 20)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(KINDS))
async def test_save_many_with_db_param(kind):
    """save_many works with an explicit db= in place of the class binding."""
    item = KINDS[kind][0]
    if kind == "async":
        db = AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
    else:
        db = SQLerDB.in_memory(shared=False)
    try:
        items = [item(name=f"db-{i}", value=i) for i in range(3)]
        await aw(item.save_many(items, db=db))
        assert [i._id for i in items] == [1, 2, 3]
        assert item._db is None
        assert sorted(m.name for m in await aw(item.using(db).all())) == ["db-0", "db-1", "db-2"]
    finally:
        await aw(db.close())


@pytest.mark.asyncio
async def test_save_many_atomicity(kit):
    """If one row violates a UNIQUE constraint, no rows should be saved."""
    item, _, _ = kit
    await aw(item.add_index("name", unique=True))
    await aw(item(name="duplicate").save())

    with pytest.raises(sqlite3.IntegrityError):
        await aw(item.save_many([item(name="good", value=1), item(name="duplicate", value=2)]))

    assert await aw(item.query().count()) == 1


@pytest.mark.asyncio
async def test_save_many_chunking(kit):
    """Insert enough rows to trigger chunking (>999 params)."""
    item, _, _ = kit
    items = [item(name=f"chunk-{i}", value=i) for i in range(1005)]
    await aw(item.save_many(items))

    assert [i._id for i in items] == list(range(1, 1006))
    assert await aw(item.query().count()) == 1005
