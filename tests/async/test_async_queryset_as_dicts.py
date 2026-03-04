import pytest
import pytest_asyncio
from sqler import AsyncSQLerModel
from sqler.db.async_db import AsyncSQLerDB
from sqler.query import SQLerField as F


class Widget(AsyncSQLerModel):
    name: str
    weight: float


@pytest_asyncio.fixture
async def adb():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    Widget.set_db(db)
    await db._ensure_table("widgets")
    try:
        yield db
    finally:
        Widget.set_db(None)
        await db.close()


@pytest.mark.asyncio
async def test_as_dicts_returns_list_of_dicts(adb):
    await Widget(name="A", weight=1.0).save()
    await Widget(name="B", weight=2.0).save()

    results = await Widget.query().as_dicts()
    assert len(results) == 2
    assert all(isinstance(d, dict) for d in results)
    assert not any(isinstance(d, Widget) for d in results)
    names = {d["name"] for d in results}
    assert names == {"A", "B"}


@pytest.mark.asyncio
async def test_as_dicts_includes_id(adb):
    w = Widget(name="X", weight=5.0)
    await w.save()

    results = await Widget.query().as_dicts()
    assert len(results) == 1
    assert results[0]["_id"] == w._id
    assert results[0]["name"] == "X"
    assert results[0]["weight"] == 5.0


@pytest.mark.asyncio
async def test_as_dicts_with_filter(adb):
    await Widget(name="light", weight=1.0).save()
    await Widget(name="medium", weight=5.0).save()
    await Widget(name="heavy", weight=10.0).save()

    results = await Widget.query().filter(F("weight") > 3).as_dicts()
    assert len(results) == 2
    names = {d["name"] for d in results}
    assert names == {"medium", "heavy"}


@pytest.mark.asyncio
async def test_as_dicts_with_select(adb):
    await Widget(name="gadget", weight=3.14).save()

    results = await Widget.query().select("name").as_dicts()
    assert len(results) == 1
    assert results[0]["name"] == "gadget"
    assert results[0]["_id"] >= 1
    # select("name") must exclude weight
    assert "weight" not in results[0]


@pytest.mark.asyncio
async def test_as_dicts_with_order_and_limit(adb):
    await Widget(name="C", weight=30.0).save()
    await Widget(name="A", weight=10.0).save()
    await Widget(name="B", weight=20.0).save()

    results = await Widget.query().order_by("weight").limit(2).as_dicts()
    assert len(results) == 2
    assert results[0]["name"] == "A"
    assert results[1]["name"] == "B"


@pytest.mark.asyncio
async def test_as_dicts_empty_result(adb):
    results = await Widget.query().as_dicts()
    assert results == []
