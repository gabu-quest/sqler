"""Async ``from_id`` and ``refresh`` load references with the batched loader ``from_ids`` uses.

``other`` is typed: it admits both classes, alone, in a list or inside a dict.
"""

import warnings
from dataclasses import dataclass
from typing import Optional, Union

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB
from sqler.models import AsyncSQLerModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel


class AxPA(AsyncSQLerModel):
    __tablename__ = "afx_px_a"
    name: str
    other: "Optional[Union[AxPA, AxPB, list, dict]]" = None


class AxPB(AsyncSQLerModel):
    __tablename__ = "afx_px_b"
    name: str
    other: "Optional[Union[AxPA, AxPB, list, dict]]" = None


@dataclass
class AxLA(AsyncSQLerLiteModel):
    __tablename__ = "afx_lx_a"
    name: str = ""
    other: "Optional[Union[AxLA, AxLB, list, dict]]" = None


@dataclass
class AxLB(AsyncSQLerLiteModel):
    __tablename__ = "afx_lx_b"
    name: str = ""
    other: "Optional[Union[AxLA, AxLB, list, dict]]" = None


@dataclass
class AxSA(AsyncSQLerLiteSafeModel):
    __tablename__ = "afx_ls_a"
    name: str = ""
    other: "Optional[Union[AxSA, AxSB, list, dict]]" = None


@dataclass
class AxSB(AsyncSQLerLiteSafeModel):
    __tablename__ = "afx_ls_b"
    name: str = ""
    other: "Optional[Union[AxSA, AxSB, list, dict]]" = None


AxPA.model_rebuild()
AxPB.model_rebuild()

BASES = {"pydantic": (AxPA, AxPB), "lite": (AxLA, AxLB), "lite-safe": (AxSA, AxSB)}


def ref(cls, id_):
    return {"_table": cls.__tablename__, "_id": id_}


@pytest_asyncio.fixture(params=sorted(BASES))
async def world(request, tmp_path):
    from sqler.adapter import AsyncSQLiteAdapter

    a_cls, b_cls = BASES[request.param]
    db = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / "w.db"), pool_size=2))
    await db.connect()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        a_cls.set_db(db)
        b_cls.set_db(db)
    for cls in (a_cls, b_cls):
        await db._ensure_table(cls.__tablename__)
    yield db, a_cls, b_cls
    await db.close()


async def selects(db, coro_fn):
    traced: list[str] = []
    conns = list(db.adapter._all_conns)
    for conn in conns:
        await conn.set_trace_callback(traced.append)
    try:
        result = await coro_fn()
    finally:
        for conn in conns:
            await conn.set_trace_callback(None)
    return result, [s for s in traced if s.lstrip().upper().startswith("SELECT")]


async def put(db, cls, **data):
    return await db.insert_document(cls.__tablename__, data)


@pytest.mark.asyncio
async def test_from_id_of_a_two_row_cycle_runs_as_many_selects_as_from_ids(world):
    db, a_cls, b_cls = world
    a = await put(db, a_cls, name="a")
    b = await put(db, b_cls, name="b", other=ref(a_cls, a))
    await db.upsert_document(a_cls.__tablename__, a, {"name": "a", "other": ref(b_cls, b)})
    got, from_id_n = await selects(db, lambda: a_cls.from_id(a))
    _, from_ids_n = await selects(db, lambda: a_cls.from_ids([a]))
    assert len(from_ids_n) == 3
    assert len(from_id_n) == 3
    assert got.other.name == "b"
    assert got.other.other.name == "a"
    assert got.other.other.other == ref(b_cls, b)


@pytest.mark.asyncio
async def test_from_id_of_a_row_with_twenty_references_runs_two_selects(world):
    db, a_cls, b_cls = world
    ids = [await put(db, b_cls, name=f"b{i}") for i in range(20)]
    a = await put(db, a_cls, name="a", other=[ref(b_cls, i) for i in ids])
    got, n = await selects(db, lambda: a_cls.from_id(a))
    assert len(n) == 2
    assert [x.name for x in got.other] == [f"b{i}" for i in range(20)]


@pytest.mark.asyncio
async def test_refresh_runs_as_many_selects_as_from_ids(world):
    db, a_cls, b_cls = world
    a = await put(db, a_cls, name="a")
    b = await put(db, b_cls, name="b", other=ref(a_cls, a))
    await db.upsert_document(a_cls.__tablename__, a, {"name": "a", "other": ref(b_cls, b)})
    inst = await a_cls.from_id(a)
    inst.name = "stale"
    _, n = await selects(db, inst.refresh)
    assert len(n) == 3
    assert inst.name == "a"
    assert inst.other.name == "b"


@pytest.mark.asyncio
async def test_a_row_referencing_itself_is_cut_after_one_round(world):
    db, a_cls, _ = world
    a = await put(db, a_cls, name="a")
    await db.upsert_document(a_cls.__tablename__, a, {"name": "a", "other": ref(a_cls, a)})
    got, n = await selects(db, lambda: a_cls.from_id(a))
    assert len(n) == 2
    assert got.other.other == ref(a_cls, a)


@pytest.mark.asyncio
async def test_a_reference_to_a_deleted_row_reads_as_none(world):
    db, a_cls, b_cls = world
    b = await put(db, b_cls, name="b")
    a = await put(db, a_cls, name="a", other=ref(b_cls, b))
    await db.delete_document(b_cls.__tablename__, b)
    assert (await a_cls.from_id(a)).other is None


@pytest.mark.asyncio
async def test_a_list_with_the_same_reference_twice_and_a_nested_dict_reference(world):
    db, a_cls, b_cls = world
    b = await put(db, b_cls, name="b")
    a = await put(db, a_cls, name="a", other=[ref(b_cls, b), {"k": ref(b_cls, b)}])
    got, n = await selects(db, lambda: a_cls.from_id(a))
    assert len(n) == 2
    assert got.other[0].name == "b"
    assert got.other[1]["k"].name == "b"


@pytest.mark.asyncio
async def test_a_reference_to_a_table_that_does_not_exist_stays_a_reference(world):
    db, a_cls, _ = world
    dangling = {"_table": "afx_no_such_table", "_id": 1}
    a = await put(db, a_cls, name="a", other=dangling)
    assert (await a_cls.from_id(a)).other == dangling
