"""``using(db, table)``: one query reads a named table; the class binding is untouched.

A row read this way remembers its table: ``save()`` and ``delete()`` write back to
that table, never to the class's usual one.
"""

import asyncio
import json
import warnings
from dataclasses import dataclass

import msgspec
import pytest
import pytest_asyncio
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerModel,
    SQLerDB,
    SQLerLiteModel,
    SQLerModel,
    SQLerMsgspecModel,
)
from sqler.adapter import AsyncSQLiteAdapter
from sqler.exceptions import InvalidTableNameError
from sqler.query import F


class UtPyJob(SQLerModel):
    __tablename__ = "ut_py_jobs"
    name: str = ""
    status: str = "new"


@dataclass
class UtLiteJob(SQLerLiteModel):
    __tablename__ = "ut_lite_jobs"
    name: str = ""
    status: str = "new"


class UtMsJob(SQLerMsgspecModel):
    __tablename__ = "ut_ms_jobs"
    name: str = ""
    status: str = "new"


class UtAsJob(AsyncSQLerModel):
    __tablename__ = "ut_as_jobs"
    name: str = ""
    status: str = "new"


@dataclass
class UtALiteJob(AsyncSQLerLiteModel):
    __tablename__ = "ut_alite_jobs"
    name: str = ""
    status: str = "new"


SYNC = [UtPyJob, UtLiteJob, UtMsJob]
SYNC_IDS = ["pydantic", "lite", "msgspec"]
ASYNC = [UtAsJob, UtALiteJob]
ASYNC_IDS = ["async-pydantic", "async-lite"]


def _binding(cls):
    return (cls.__dict__.get("_db"), cls.__dict__.get("_table"), cls.__dict__.get("__tablename__"))


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    try:
        yield d
    finally:
        d.close()


@pytest_asyncio.fixture
async def adb():
    d = AsyncSQLerDB.in_memory(shared=False)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


# ----- reading a named table --------------------------------------------------------


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_using_with_table_reads_only_that_table(db, cls):
    cls(name="live-1").save(db=db)
    cls(name="live-2").save(db=db)
    db.insert_document("ut_archive", {"name": "old-1", "status": "done"})
    before = _binding(cls)

    archived = cls.using(db, "ut_archive").all()
    live = cls.using(db).order_by("name").all()

    assert [r.name for r in archived] == ["old-1"]
    assert [r.name for r in live] == ["live-1", "live-2"]
    assert cls.using(db, table="ut_archive").filter(F("status") == "done").count() == 1
    assert _binding(cls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_using_with_table_reads_only_that_table(adb, cls):
    await cls(name="live-1").save(db=adb)
    await adb.insert_document("ut_archive", {"name": "old-1", "status": "done"})
    before = _binding(cls)

    archived = await cls.using(adb, "ut_archive").all()
    live = await cls.using(adb).all()

    assert [r.name for r in archived] == ["old-1"]
    assert [r.name for r in live] == ["live-1"]
    assert _binding(cls) == before


@pytest.mark.parametrize("bad", ["users; DROP TABLE ut_keep", "users\n", "", "1abc"])
@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_using_refuses_a_bad_table_name_before_any_sql(db, cls, bad):
    db.insert_document("ut_keep", {"k": 1})
    with pytest.raises(InvalidTableNameError):
        cls.using(db, bad)
    assert db.find_document("ut_keep", 1)["k"] == 1


@pytest.mark.parametrize("bad", ["users; DROP TABLE x", "users\n"])
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
def test_async_using_refuses_a_bad_table_name(cls, bad):
    adapter_less = AsyncSQLerDB(AsyncSQLiteAdapter(":memory:"))
    with pytest.raises(InvalidTableNameError):
        cls.using(adapter_less, bad)


def test_bound_class_keeps_its_binding_across_using_with_table(db):
    class UtBound(SQLerModel):
        name: str = ""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        UtBound.set_db(db, "ut_bound_live")
    UtBound(name="live").save()
    db.insert_document("ut_bound_archive", {"name": "old"})

    assert [r.name for r in UtBound.using(db, "ut_bound_archive").all()] == ["old"]
    assert UtBound._table == "ut_bound_live"
    assert UtBound.__tablename__ == "ut_bound_live"
    assert UtBound._db is db
    assert [r.name for r in UtBound.query().all()] == ["live"]


# ----- what a row read from a named table does on save() / delete() ------------------


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_row_read_from_named_table_saves_back_to_that_table(db, cls):
    cls(name="live").save(db=db)
    arch_id = db.insert_document("ut_archive", {"name": "old", "status": "done"})

    row = cls.using(db, "ut_archive").first()
    row.status = "restored"
    row.save(db=db)

    assert row._id == arch_id
    assert db.find_document("ut_archive", arch_id)["status"] == "restored"
    assert [r.name for r in cls.using(db).all()] == ["live"]

    row.delete(db=db)
    assert db.find_document("ut_archive", arch_id) is None
    assert [r.name for r in cls.using(db).all()] == ["live"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_row_read_from_named_table_saves_back_to_that_table(adb, cls):
    await cls(name="live").save(db=adb)
    arch_id = await adb.insert_document("ut_archive", {"name": "old", "status": "done"})

    row = await cls.using(adb, "ut_archive").first()
    row.status = "restored"
    await row.save(db=adb)

    assert row._id == arch_id
    assert (await adb.find_document("ut_archive", arch_id))["status"] == "restored"
    assert [r.name for r in await cls.using(adb).all()] == ["live"]

    await row.delete(db=adb)
    assert await adb.find_document("ut_archive", arch_id) is None


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_row_read_from_class_table_still_saves_to_class_table(db, cls):
    cls(name="live").save(db=db)
    row = cls.using(db).first()
    row.status = "seen"
    row.save(db=db)
    assert [(r.name, r.status) for r in cls.using(db).all()] == [("live", "seen")]


# ----- two tasks, one reading the archive and one the live table --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_interleaved_tasks_each_read_only_their_table(tmp_path, cls):
    adapter = AsyncSQLiteAdapter(str(tmp_path / "ut.db"), pool_size=2)
    db = AsyncSQLerDB(adapter)
    await db.connect()
    try:
        for i in range(3):
            await cls(name=f"live-{i}").save(db=db)
        for i in range(2):
            await db.insert_document("ut_archive", {"name": f"old-{i}", "status": "done"})
        before = _binding(cls)
        start = asyncio.Event()

        async def reader(table, rounds=25):
            await start.wait()
            seen = []
            for _ in range(rounds):
                qs = cls.using(db, table) if table else cls.using(db)
                rows = await qs.order_by("name").all()
                seen.append(tuple(r.name for r in rows))
                await asyncio.sleep(0)
            return seen

        t_archive = asyncio.create_task(reader("ut_archive"))
        t_live = asyncio.create_task(reader(None))
        start.set()
        archive_seen, live_seen = await asyncio.wait_for(
            asyncio.gather(t_archive, t_live), timeout=30
        )

        assert archive_seen == [("old-0", "old-1")] * 25
        assert live_seen == [("live-0", "live-1", "live-2")] * 25
        assert _binding(cls) == before
    finally:
        await db.close()


# ----- msgspec: the table a row was read from is in neither its encoding nor == ------


def test_msgspec_row_from_its_own_table_encodes_as_before(db):
    UtMsJob(name="a").save(db=db)
    row = UtMsJob.using(db).first()
    assert json.loads(msgspec.json.encode(row)) == {
        "name": "a",
        "status": "new",
        "_id": 1,
        "_snapshot": {"name": "a", "status": "new"},
    }


def test_msgspec_row_from_another_table_encodes_and_compares_like_its_twin(db):
    live_id = db.insert_document("ut_ms_jobs", {"name": "same", "status": "done"})
    arch_id = db.insert_document("ut_archive", {"name": "same", "status": "done"})
    assert live_id == arch_id == 1

    live = UtMsJob.using(db).first()
    archived = UtMsJob.using(db, "ut_archive").first()

    assert msgspec.json.encode(archived) == msgspec.json.encode(live)
    assert b"ut_archive" not in msgspec.json.encode(archived)
    assert archived == live
    assert msgspec.structs.asdict(archived) == msgspec.structs.asdict(live)


def test_msgspec_row_from_another_table_saves_deletes_and_refreshes_there(db):
    class UtMsBound(SQLerMsgspecModel):
        name: str = ""
        status: str = "new"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        UtMsBound.set_db(db, "ut_ms_bound")
    UtMsBound(name="live").save()
    arch_id = db.insert_document("ut_archive", {"name": "old", "status": "done"})

    row = UtMsBound.using(db, "ut_archive").first()
    row.status = "restored"
    row.save()
    assert db.find_document("ut_archive", arch_id)["status"] == "restored"

    db.upsert_document("ut_archive", arch_id, {"name": "old", "status": "changed-elsewhere"})
    row.refresh()
    assert row.status == "changed-elsewhere"

    row.delete()
    assert db.find_document("ut_archive", arch_id) is None
    assert [r.name for r in UtMsBound.query().all()] == ["live"]


# --- a queryset object run again after its class was rebound ---------------------


def _names(db, table):
    cur = db.adapter.execute(f"SELECT json_extract(data, '$.name') FROM {table} ORDER BY _id")
    return [r[0] for r in cur.fetchall()]


def test_queryset_run_again_after_the_class_was_rebound_still_writes_to_its_own_table(db):
    class UtRebound(SQLerModel):
        __tablename__ = "ut_rebound"
        name: str = ""

    UtRebound(name="one").save(db=db)
    db.ensure_table("ut_rebound_other")
    qs = UtRebound.using(db)
    assert [r.name for r in qs.all()] == ["one"]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        UtRebound.set_db(db, "ut_rebound_other")
    row = qs.all()[0]
    row.name = "changed"
    row.save()

    assert _names(db, "ut_rebound") == ["changed"]
    assert _names(db, "ut_rebound_other") == []


@pytest.mark.asyncio
async def test_async_queryset_run_again_after_the_class_was_rebound_still_writes_to_its_own_table(adb):
    class UtAsRebound(AsyncSQLerModel):
        __tablename__ = "ut_as_rebound"
        name: str = ""

    await UtAsRebound(name="one").save(db=adb)
    await adb.ensure_table("ut_as_rebound_other")
    qs = UtAsRebound.using(adb)
    assert [r.name for r in await qs.all()] == ["one"]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        UtAsRebound.set_db(adb, "ut_as_rebound_other")
    row = (await qs.all())[0]
    row.name = "changed"
    await row.save()

    cur = await adb.adapter.execute("SELECT json_extract(data, '$.name') FROM ut_as_rebound ORDER BY _id")
    own = [r[0] for r in await cur.fetchall()]
    cur = await adb.adapter.execute("SELECT count(*) FROM ut_as_rebound_other")
    other = (await cur.fetchone())[0]
    await adb.adapter.auto_commit()
    assert own == ["changed"]
    assert other == 0


@pytest.mark.parametrize("run", ["all", "first", "iter"])
def test_queryset_run_again_after_rebind_reports_its_own_source_table(db, run):
    class UtSrc(SQLerModel):
        __tablename__ = "ut_src"
        name: str = ""

    UtSrc(name="one").save(db=db)
    db.ensure_table("ut_src_other")
    qs = UtSrc.using(db)
    reads = {
        "all": lambda: qs.all()[0],
        "first": lambda: qs.first(),
        "iter": lambda: next(qs.iter()),
    }
    assert reads[run]()._source_table is None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        UtSrc.set_db(db, "ut_src_other")

    assert reads[run]()._source_table == "ut_src"


@pytest.mark.asyncio
@pytest.mark.parametrize("run", ["all", "first"])
async def test_async_queryset_run_again_after_rebind_reports_its_own_source_table(adb, run):
    class UtAsSrc(AsyncSQLerModel):
        __tablename__ = "ut_as_src"
        name: str = ""

    await UtAsSrc(name="one").save(db=adb)
    await adb.ensure_table("ut_as_src_other")
    qs = UtAsSrc.using(adb)

    async def read():
        if run == "all":
            return (await qs.all())[0]
        return await qs.first()

    assert (await read())._source_table is None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        UtAsSrc.set_db(adb, "ut_as_src_other")

    assert (await read())._source_table == "ut_as_src"


# --- a copy of a row read from another table -------------------------------------


def test_copy_of_a_row_from_another_table_by_base(db):
    """A pydantic copy keeps the table it was read from; a lite or msgspec copy made
    with ``replace`` is a row of the class's own table (docs/API.md says so)."""
    import dataclasses

    db.ensure_table("ut_copy_archive")
    for cls in (UtPyJob, UtLiteJob, UtMsJob):
        cls(name="live").save(db=db)
        db.insert_document("ut_copy_archive", {"name": "old", "status": "new"})

    py = UtPyJob.using(db, "ut_copy_archive").first().model_copy()
    lite = dataclasses.replace(UtLiteJob.using(db, "ut_copy_archive").first())
    ms = msgspec.structs.replace(UtMsJob.using(db, "ut_copy_archive").first())

    assert (py._source_table, lite._source_table, getattr(ms, "_source_table", None)) == (
        "ut_copy_archive",
        None,
        None,
    )
