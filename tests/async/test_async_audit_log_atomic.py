"""AsyncAuditLogMixin: a save and its audit row are both there or neither is."""

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.exceptions import UniqueConstraintError
from sqler.models.mixins import AsyncAuditLogMixin

pytestmark = pytest.mark.asyncio


class AAtomicItem(AsyncAuditLogMixin, AsyncSQLerModel):
    __tablename__ = "aatomic_items"
    name: str


@pytest_asyncio.fixture
async def db():
    db = AsyncSQLerDB.in_memory()
    await db.connect()
    AAtomicItem.set_db(db)
    yield db
    await db.close()


async def _rows(db, sql):
    cur = await db.adapter.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    return [r[0] for r in rows]


async def _names(db):
    return await _rows(db, "SELECT json_extract(data, '$.name') FROM aatomic_items ORDER BY _id;")


async def _actions(db):
    return await _rows(db, "SELECT action FROM aatomic_items_audit ORDER BY _id;")


async def _ddl(db, sql):
    cur = await db.adapter.execute(sql)
    await cur.close()
    await db.adapter.auto_commit()


async def test_failing_audit_row_leaves_no_saved_change(db):
    item = await AAtomicItem(name="one").save()
    item.name = "two"
    await item.save()
    await _ddl(db, "CREATE UNIQUE INDEX a_audit_once ON aatomic_items_audit(record_id, action);")
    item.name = "three"
    with pytest.raises(UniqueConstraintError):
        await item.save()
    assert await _names(db) == ["two"]


async def test_failed_create_leaves_the_instance_unsaved_and_a_second_save_creates(db):
    await AAtomicItem(name="seed").save()
    await _ddl(db, "CREATE UNIQUE INDEX a_audit_once ON aatomic_items_audit(action);")
    item = AAtomicItem(name="second")
    with pytest.raises(UniqueConstraintError):
        await item.save()
    assert item._id is None
    assert await _names(db) == ["seed"]
    await _ddl(db, "DROP INDEX a_audit_once;")
    await item.save()
    assert await _names(db) == ["seed", "second"]


async def test_failing_audit_row_on_delete_keeps_the_row(db):
    item = await AAtomicItem(name="keep").save()
    item.name = "keep2"
    await item.save()
    await _ddl(db, "CREATE UNIQUE INDEX a_audit_once ON aatomic_items_audit(action);")
    await _ddl(
        db,
        "INSERT INTO aatomic_items_audit(record_id, action, timestamp) VALUES (99, 'delete', 'x');",
    )
    with pytest.raises(UniqueConstraintError):
        await item.delete()
    assert await _names(db) == ["keep2"]


async def test_delete_refused_by_a_trigger_leaves_no_delete_audit_row(db):
    item = await AAtomicItem(name="keep").save()
    await _ddl(
        db,
        "CREATE TRIGGER a_no_delete BEFORE DELETE ON aatomic_items "
        "BEGIN SELECT RAISE(ABORT, 'delete refused'); END;",
    )
    with pytest.raises(Exception, match="delete refused"):
        await item.delete()
    assert await _names(db) == ["keep"]
    assert await _actions(db) == ["create"]
    assert item._id is not None


async def test_inside_a_rolled_back_transaction_both_vanish(db):
    await AAtomicItem(name="base").save()
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await AAtomicItem(name="inside").save()
            raise RuntimeError("boom")
    assert await _names(db) == ["base"]
    assert await _actions(db) == ["create"]


async def test_inside_a_committed_transaction_both_stay(db):
    async with db.transaction():
        await AAtomicItem(name="inside").save()
    assert await _names(db) == ["inside"]
    assert await _actions(db) == ["create"]


async def test_ordinary_save_update_delete_still_log(db):
    item = await AAtomicItem(name="a").save()
    item.name = "b"
    await item.save()
    assert [e["action"] for e in await item.get_audit_log()] == ["update", "create"]
    await item.delete()
    assert await _actions(db) == ["create", "update", "delete"]
