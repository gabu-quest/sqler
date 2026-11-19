"""Async: what a database object learns about tables inside a transaction belongs to it.

The async twin of ``tests/test_rollback_table_memory.py``. Levels are counted per task; a task
started inside a level shares it. Statements are counted with the driver's trace callback on
every pooled connection.
"""

import asyncio
import contextvars
import warnings
from dataclasses import dataclass
from typing import Optional

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel, AsyncSQLerSafeModel
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel

pytestmark = pytest.mark.asyncio


class AMemPromo(AsyncSQLerModel):
    __tablename__ = "amem_promos"
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


@dataclass
class AMemRefChild(AsyncSQLerLiteSafeModel):
    __tablename__ = "amem_ref_children"
    __promoted__ = {"tier": "TEXT NOT NULL DEFAULT 'basic'"}
    name: str = ""
    tier: str = "basic"


@dataclass
class AMemRefParent(AsyncSQLerLiteModel):
    __tablename__ = "amem_ref_parents"
    name: str = ""
    child: Optional[AMemRefChild] = None


@pytest_asyncio.fixture
async def db(tmp_path):
    db = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / "amem.db"), pool_size=2))
    await db.connect()
    yield db
    await db.close()


class Traced:
    """Statements every pooled connection sends while the block runs."""

    def __init__(self, db):
        self.conns = list(db.adapter._all_conns)
        self.sent: list[str] = []

    async def __aenter__(self):
        for conn in self.conns:
            await conn.set_trace_callback(self.sent.append)
        return self.sent

    async def __aexit__(self, *exc):
        for conn in self.conns:
            await conn.set_trace_callback(None)
        return False


def _kind(sent: list[str], word: str) -> list[str]:
    return [s for s in sent if s.lstrip().upper().startswith(word)]


async def _statuses(db) -> list[str]:
    return sorted(p.status for p in await AMemPromo.using(db).all())


async def test_tables_first_made_in_a_rolled_back_transaction_save_again_and_the_rollback_sends_only_rollback(db):
    async with db.transaction() as tx:
        await AMemPromo(name="gone").save(db=db)
        await db.ensure_table("amem_vers", versioned=True)
        async with Traced(db) as sent:
            await tx.rollback()
    assert sent == ["ROLLBACK"]
    await AMemPromo(name="kept", status="x").save(db=db)
    rid, version = await db.upsert_with_version("amem_vers", None, {"n": 1}, None)
    assert (await _statuses(db), version) == (["x"], 0)
    assert await db.find_document_with_version("amem_vers", rid) == {"_id": rid, "n": 1, "_version": 0}


async def test_inner_level_rolled_back_inside_an_outer_level_that_commits(db):
    async with db.transaction():
        with pytest.raises(RuntimeError, match="undo"):
            async with db.transaction():
                await AMemPromo(name="gone").save(db=db)
                await db.ensure_table("amem_vers", versioned=True)
                raise RuntimeError("undo")
        await AMemPromo(name="kept", status="x").save(db=db)
        _, version = await db.upsert_with_version("amem_vers", None, {"n": 1}, None)
    assert (await _statuses(db), version) == (["x"], 0)


async def test_inner_level_committed_inside_an_outer_level_that_rolls_back(db):
    with pytest.raises(RuntimeError, match="undo"):
        async with db:
            async with db.transaction():
                await AMemPromo(name="gone").save(db=db)
                await db.ensure_table("amem_vers", versioned=True)
            raise RuntimeError("undo")
    await AMemPromo(name="kept", status="x").save(db=db)
    _, version = await db.upsert_with_version("amem_vers", None, {"n": 1}, None)
    assert (await _statuses(db), version) == (["x"], 0)


async def test_a_table_remembered_before_the_transaction_stays_remembered_after_its_rollback(db):
    await AMemPromo(name="a", status="s").save(db=db)
    await db.upsert_with_version("amem_vers", None, {"n": 1}, None)
    with pytest.raises(RuntimeError, match="undo"):
        async with db.transaction():
            await AMemPromo(name="b").save(db=db)
            raise RuntimeError("undo")
    async with Traced(db) as sent:
        await AMemPromo(name="c", status="t").save(db=db)
        await db.upsert_with_version("amem_vers", None, {"n": 2}, None)
    assert (_kind(sent, "PRAGMA"), _kind(sent, "CREATE")) == ([], [])
    assert await _statuses(db) == ["s", "t"]


async def test_a_thousand_saves_in_one_transaction_create_the_new_table_once(db):
    await db.adapter.execute("SELECT 1")  # opens the pooled connection the transaction uses
    await db.adapter.auto_commit()
    async with Traced(db) as sent:
        async with db.transaction():
            for i in range(1000):
                await AMemPromo(name=str(i)).save(db=db)
    assert len(_kind(sent, "CREATE TABLE IF NOT EXISTS AMEM_PROMOS")) == 1
    assert len(_kind(sent, "CREATE TABLE IF NOT EXISTS _SQLER_PROMOTED")) == 1  # the record table
    assert await AMemPromo.using(db).count() == 1000


async def test_a_child_task_writing_inside_a_transaction_that_rolls_back(db):
    with pytest.raises(RuntimeError, match="undo"):
        async with db.transaction():
            await asyncio.create_task(AMemPromo(name="child", status="c").save(db=db))
            raise RuntimeError("undo")
    # the child's table and row went with the transaction; the next save creates the table again
    await AMemPromo(name="kept", status="x").save(db=db)
    assert await _statuses(db) == ["x"]


async def test_a_table_made_in_a_transaction_the_adapter_opened_directly_is_not_remembered(db):
    await db.adapter.begin_transaction()
    await db.ensure_table("amem_promos", promoted=AMemPromo.__promoted__)
    await db.adapter.end_transaction(commit=False)
    await AMemPromo(name="kept", status="x").save(db=db)
    assert await _statuses(db) == ["x"]


async def test_reference_to_a_lite_safe_row_loads_after_the_column_promotion_rolled_back(db):
    await db.ensure_table("amem_ref_children", versioned=True)
    cid, _ = await db.upsert_with_version("amem_ref_children", None, {"name": "c"}, None)
    pid = await db.insert_document(
        "amem_ref_parents", {"name": "p", "child": {"_table": "amem_ref_children", "_id": cid}}
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        AMemRefParent.set_db(db)
        AMemRefChild.set_db(db)
    with pytest.raises(RuntimeError, match="undo"):
        async with db:
            await AMemRefChild(name="gone").save()  # adds the promoted column, undone below
            raise RuntimeError("undo")
    got = await AMemRefParent.from_id(pid)
    assert (type(got.child).__name__, got.child.name, got.child._version) == ("AMemRefChild", "c", 0)


class AMemPromoTwo(AsyncSQLerModel):
    __tablename__ = "amem_promos_two"
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


class AMemVersioned(AsyncSQLerSafeModel):
    __tablename__ = "amem_versioned"
    name: str = ""


async def test_a_promoted_table_made_in_an_adapter_savepoint_rolled_back_inside_a_level_saves_afterwards(db):
    async with db:
        await db.adapter.begin_transaction()
        await db.ensure_table("amem_promos", promoted=AMemPromo.__promoted__)
        await db.adapter.end_transaction(commit=False)
    await AMemPromo(name="k", status="x").save(db=db)
    assert await _statuses(db) == ["x"]


async def test_a_versioned_table_made_in_an_adapter_savepoint_rolled_back_inside_a_level_saves_afterwards(db):
    async with db:
        await db.adapter.begin_transaction()
        await AMemVersioned(name="gone").save(db=db)
        await db.adapter.end_transaction(commit=False)
    await AMemVersioned(name="k").save(db=db)
    assert [v.name for v in await AMemVersioned.using(db).all()] == ["k"]


async def test_a_level_committed_inside_an_adapter_savepoint_that_rolls_back_is_not_remembered(db):
    async with db:
        await db.adapter.begin_transaction()
        async with db.transaction():
            await AMemPromo(name="gone").save(db=db)
        await db.adapter.end_transaction(commit=False)
    await AMemPromo(name="k", status="x").save(db=db)
    assert await _statuses(db) == ["x"]


async def test_a_transaction_ended_in_a_task_without_its_level_raises_nothing_and_merges_nothing(db):
    txn = db.transaction()
    await txn.__aenter__()

    async def end():
        await txn.__aexit__(None, None, None)

    # a task with an empty context sees neither the level nor the adapter's depth
    await asyncio.wait_for(asyncio.create_task(end(), context=contextvars.Context()), timeout=10)
    await txn.__aexit__(None, None, None)


async def _both_statuses(db) -> tuple[list[str], list[str]]:
    return (
        sorted(p.status for p in await AMemPromo.using(db).all()),
        sorted(p.status for p in await AMemPromoTwo.using(db).all()),
    )


async def test_two_children_of_a_level_each_make_a_table_and_the_commit_remembers_both(db):
    async with db:
        await asyncio.gather(
            AMemPromo(name="child", status="c1").save(db=db),
            AMemPromoTwo(name="child", status="c2").save(db=db),
        )
    async with Traced(db) as sent:
        await AMemPromo(name="kept", status="a").save(db=db)
        await AMemPromoTwo(name="kept", status="b").save(db=db)
    assert await _both_statuses(db) == (["a", "c1"], ["b", "c2"])
    assert _kind(sent, "CREATE") == []
    assert _kind(sent, "PRAGMA") == []


async def test_two_children_of_a_level_each_make_a_table_and_the_rollback_forgets_both(db):
    with pytest.raises(RuntimeError, match="undo"):
        async with db:
            await asyncio.gather(
                AMemPromo(name="child", status="c1").save(db=db),
                AMemPromoTwo(name="child", status="c2").save(db=db),
            )
            raise RuntimeError("undo")
    await AMemPromo(name="kept", status="a").save(db=db)
    await AMemPromoTwo(name="kept", status="b").save(db=db)
    assert await _both_statuses(db) == (["a"], ["b"])
