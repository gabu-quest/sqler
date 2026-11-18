"""AsyncTrackedModel and AsyncPartialUpdateMixin: a partial save of an async model writes the
changed fields through the row's own awaited save(), as the sync twins do."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from pydantic import create_model

from sqler import (
    AsyncPartialUpdateMixin,
    AsyncSQLerDB,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
    AsyncTrackedModel,
)
from sqler.exceptions import (
    InvalidFieldNameError,
    NotFoundError,
    StaleVersionError,
    UnstorableValueError,
)
from sqler.logging import query_logger
from sqler.models import _dirty
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.mixins import AsyncAuditLogMixin, TimestampMixin
from sqler.tracking import PartialUpdateMixin, TrackedModel

pytestmark = pytest.mark.asyncio


class APsOwner(AsyncSQLerModel):
    __tablename__ = "aps_owners"
    name: str = ""


class APsDoc(AsyncPartialUpdateMixin, AsyncTrackedModel, AsyncSQLerModel):
    __tablename__ = "aps_docs"
    key: str = ""
    value: str = "v"
    tags: list = []
    flag: bool = False
    at: Optional[datetime] = None
    price: Optional[Decimal] = None
    took: Optional[timedelta] = None
    note: Optional[str] = "n"
    owner: Optional[APsOwner] = None
    blob: Optional[bytes] = None


class APsSafeDoc(APsDoc, AsyncSQLerSafeModel):
    __tablename__ = "aps_safe_docs"


@dataclass
class APsLiteDoc(AsyncPartialUpdateMixin, AsyncTrackedModel, AsyncSQLerLiteModel):
    __tablename__ = "aps_lite_docs"
    key: str = ""
    value: str = "v"
    flag: bool = False
    at: Optional[datetime] = None
    price: Optional[Decimal] = None
    took: Optional[timedelta] = None
    note: Optional[str] = "n"


@dataclass
class APsLiteSafeDoc(APsLiteDoc, AsyncSQLerLiteSafeModel):
    __tablename__ = "aps_lite_safe_docs"


class APsPromo(AsyncPartialUpdateMixin, AsyncTrackedModel, AsyncSQLerModel):
    __tablename__ = "aps_promos"
    __promoted__ = {"title": "TEXT"}
    title: str = ""
    body: str = ""


BASES = [APsDoc, APsSafeDoc, APsLiteDoc, APsLiteSafeDoc]
VERSIONED = [APsSafeDoc, APsLiteSafeDoc]
KINDS = [
    ("value", "new", "new"),
    ("flag", True, True),
    ("at", datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05"),
    ("price", Decimal("0.1"), 0.1),
    ("took", timedelta(minutes=90), 5400),
    ("note", None, None),
]


@pytest_asyncio.fixture
async def db():
    d = AsyncSQLerDB.in_memory(shared=False)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


async def _row(db, table, rid):
    cur = await db.adapter.execute(f"SELECT * FROM {table} WHERE _id = ?", [rid])
    try:
        r = await cur.fetchone()
        names = [d[0] for d in cur.description]
        return None if r is None else {k: v for k, v in zip(names, r) if k != "_id"}
    finally:
        await cur.close()


async def _load(cls, db, rid):
    found = [m for m in await cls.using(db).all() if m._id == rid]
    assert len(found) == 1
    return found[0]


async def _stored_changes(model):
    """The base's stored-form answer: the fields save() would write differently than the
    snapshot of the row it last loaded or saved."""
    return _dirty.changed_fields(model._snapshot, await model._adump_with_relations())


async def _twins(cls, db):
    return await cls(key="k").save(db=db), await cls(key="k").save(db=db)


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("name,value,in_json", KINDS, ids=[k[0] for k in KINDS])
async def test_an_async_partial_save_stores_each_kind_as_save_does(db, cls, name, value, in_json):
    partial, full = await _twins(cls, db)
    setattr(partial, name, value)
    setattr(full, name, value)
    await partial.save_partial(db=db)
    await full.save(db=db)
    row = await _row(db, cls.__tablename__, partial._id)
    assert row == await _row(db, cls.__tablename__, full._id)
    assert json.loads(row["data"])[name] == in_json
    assert (partial.is_dirty, await _stored_changes(partial)) == (False, set())


async def test_an_async_partial_save_stores_a_reference_as_save_does(db):
    owner = await APsOwner(name="o").save(db=db)
    partial, full = await _twins(APsDoc, db)
    partial.owner = full.owner = owner
    await partial.save_partial(db=db)
    await full.save(db=db)
    row = await _row(db, "aps_docs", partial._id)
    assert row == await _row(db, "aps_docs", full._id)
    assert json.loads(row["data"])["owner"] == {"_table": "aps_owners", "_id": owner._id}


async def test_bytes_in_a_json_field_is_refused_naming_the_field(db):
    m = await APsDoc(key="k").save(db=db)
    m.blob = b"\x00"
    with pytest.raises(UnstorableValueError) as err:
        await m.save_partial(db=db)
    assert err.value.field == "blob"
    assert m.changed_fields == {"blob"}


@pytest.mark.parametrize(
    "change", [{"title": "t2"}, {"title": "t2", "body": "b2"}], ids=["alone", "with plain"]
)
async def test_an_async_promoted_field_is_written_to_its_column(db, change):
    partial = await APsPromo(title="t1", body="b1").save(db=db)
    full = await APsPromo(title="t1", body="b1").save(db=db)
    for m in (partial, full):
        for k, v in change.items():
            setattr(m, k, v)
    with query_logger.capture() as sent:
        await partial.save_partial(db=db)
    await full.save(db=db)
    assert len([q for q in sent if q.sql.lstrip().upper().startswith("UPDATE")]) == 1
    row = await _row(db, "aps_promos", partial._id)
    assert row == await _row(db, "aps_promos", full._id)
    assert row["title"] == "t2"


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
async def test_no_changed_field_sends_no_statement(db, cls):
    m = await cls(key="k").save(db=db)
    with query_logger.capture() as sent:
        await m.save_partial(db=db)
    assert sent == []


@pytest.mark.parametrize("cls", VERSIONED, ids=lambda c: c.__name__)
async def test_a_stale_async_partial_save_is_refused(db, cls):
    m = await cls(key="k").save(db=db)
    a, b = await _load(cls, db, m._id), await _load(cls, db, m._id)
    a.value = "from a"
    b.flag = True
    await a.save_partial(db=db)
    assert a._version == 1
    with pytest.raises(StaleVersionError):
        await b.save_partial(db=db)
    row = await _row(db, cls.__tablename__, m._id)
    assert (row["_version"], json.loads(row["data"])["flag"]) == (1, False)
    assert b.changed_fields == {"flag"}


@pytest.mark.parametrize("cls", [APsDoc, APsLiteDoc], ids=lambda c: c.__name__)
async def test_two_async_instances_that_change_other_fields_both_land(db, cls):
    m = await cls(key="k").save(db=db)
    a, b = await _load(cls, db, m._id), await _load(cls, db, m._id)
    a.value = "from a"
    b.flag = True
    await a.save_partial(db=db)
    await b.save_partial(db=db)
    loaded = await _load(cls, db, m._id)
    assert (loaded.value, loaded.flag) == ("from a", True)


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
async def test_an_async_partial_save_of_a_row_that_is_gone_raises_not_found(db, cls):
    m = await cls(key="k").save(db=db)
    cur = await db.adapter.execute(f"DELETE FROM {cls.__tablename__} WHERE _id = ?", [m._id])
    await cur.close()
    await db.adapter.auto_commit()
    m.value = "new"
    with pytest.raises(NotFoundError):
        await m.save_partial(db=db)
    assert await _row(db, cls.__tablename__, m._id) is None
    assert (m.changed_fields, await _stored_changes(m)) == ({"value"}, {"value"})


async def test_an_async_model_never_saved_is_saved_in_full(db):
    m = APsDoc(key="k", value="first")
    await m.save_partial(db=db)
    assert (await _load(APsDoc, db, m._id)).value == "first"
    assert m.is_dirty is False


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
async def test_save_many_leaves_the_tracker_as_a_loop_of_save_does(db, cls):
    batch, looped = cls(key="batch"), cls(key="looped")
    batch.value = looped.value = "changed"
    await cls.save_many([batch], db=db)
    await looped.save(db=db)
    assert [(m.is_dirty, m.changed_fields, m.get_change_history()) for m in (batch, looped)] == [
        (False, set(), []),
        (False, set(), []),
    ]


class APsBound(AsyncPartialUpdateMixin, AsyncTrackedModel, AsyncSQLerModel):
    __tablename__ = "aps_bound"
    name: str = ""


async def test_from_id_is_awaited_and_gives_a_tracked_model(db):
    with pytest.warns(DeprecationWarning):
        APsBound.set_db(db)
    try:
        saved = await APsBound(name="a").save()
        loaded = await APsBound.from_id(saved._id)
        assert (loaded.name, loaded.is_dirty) == ("a", False)
        loaded.name = "b"
        assert loaded.changed_fields == {"name"}
        await loaded.save_partial()
        assert (await APsBound.from_id(saved._id)).name == "b"
        assert loaded.is_dirty is False
    finally:
        APsBound._db = None
        APsBound._table = None


class APsStamped(
    TimestampMixin, AsyncAuditLogMixin, AsyncPartialUpdateMixin, AsyncTrackedModel, AsyncSQLerModel
):
    __tablename__ = "aps_stamped"
    __promoted__ = {"title": "TEXT"}
    title: str = ""
    body: str = ""


async def test_timestamps_and_the_async_audit_log_hold_for_a_partial_save(db, ticking_clock):
    m = await APsStamped(title="t", body="b").save(db=db)
    stamped = m.updated_at
    other = await _load(APsStamped, db, m._id)
    other.title = "theirs"
    await other.save(db=db)
    m.body = "b2"
    await m.save_partial(db=db)
    assert m.updated_at > stamped
    loaded = await _load(APsStamped, db, m._id)
    assert (loaded.title, loaded.body, loaded.updated_at) == ("theirs", "b2", m.updated_at)
    log = await m.get_audit_log(db=db)
    assert [e["action"] for e in log] == ["update", "update", "create"]
    assert set(log[0]["changes"]) == {"body", "updated_at"}
    assert m.is_dirty is False


async def test_the_sync_tracked_model_on_an_async_base_is_refused_at_class_creation():
    with pytest.raises(TypeError, match="use AsyncTrackedModel"):

        class APsWrong(PartialUpdateMixin, TrackedModel, AsyncSQLerModel):
            __tablename__ = "aps_wrong"
            name: str = ""

    with pytest.raises(TypeError, match="use AsyncTrackedModel"):

        @dataclass
        class APsWrongLite(TrackedModel, AsyncSQLerLiteModel):
            __tablename__ = "aps_wrong_lite"
            name: str = ""


async def test_a_field_whatever_its_name_is_written_and_one_no_path_can_name_is_refused(db):
    names = ["a.b", "c'd", "名前", 'g"h']
    Odd = create_model(
        "APsOdd",
        __base__=(AsyncPartialUpdateMixin, AsyncTrackedModel, AsyncSQLerModel),
        **{name: (str, "") for name in names},
    )
    m = await Odd(**{name: "v" for name in names}).save(db=db)
    for name in names[:3]:
        setattr(m, name, "z")
        await m.save_partial(db=db)
    cur = await db.adapter.execute("SELECT data FROM apsodds WHERE _id = ?", [m._id])
    written = (await cur.fetchone())[0]
    await db.adapter.auto_commit()
    assert json.loads(written) == {"a.b": "z", "c'd": "z", "名前": "z", 'g"h': "v"}
    setattr(m, 'g"h', "z")
    with pytest.raises(InvalidFieldNameError) as refused:
        await m.save_partial(db=db)
    assert refused.value.field == 'g"h'
    cur = await db.adapter.execute("SELECT data FROM apsodds WHERE _id = ?", [m._id])
    assert (await cur.fetchone())[0] == written
    await db.adapter.auto_commit()
