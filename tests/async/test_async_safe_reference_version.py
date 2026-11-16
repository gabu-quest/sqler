"""Async: a safe model reached through a reference keeps its stored version and snapshot."""

import warnings
from typing import Optional

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel, AsyncSQLerSafeModel
from sqler.adapter import AsyncSQLiteAdapter


class AsrvSafe(AsyncSQLerSafeModel):
    __tablename__ = "asrv_safes"
    name: str = ""


class AsrvPlain(AsyncSQLerModel):
    __tablename__ = "asrv_plains"
    title: str = ""
    owner: Optional[AsrvSafe] = None
    many: Optional[list[AsrvSafe]] = None


@pytest_asyncio.fixture
async def db(tmp_path):
    db = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / "asrv.db"), pool_size=2))
    await db.connect()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        AsrvSafe.set_db(db)
        AsrvPlain.set_db(db)
    yield db
    await db.close()


async def _bump(row, times):
    await row.save()
    for i in range(times):
        row.name = f"{row.name}{i}"
        await row.save()
    return row


@pytest.mark.asyncio
async def test_from_id_gives_nested_safe_row_its_version_and_snapshot(db):
    s = await _bump(AsrvSafe(name="a"), 2)
    assert s._version == 2
    p = AsrvPlain(title="t", owner=s)
    await p.save()
    got = await AsrvPlain.from_id(p._id)
    assert got.owner._version == 2
    assert got.owner._snapshot == {"name": "a01"}
    got.owner.name = "z"
    await got.owner.save()
    assert got.owner._version == 3


@pytest.mark.asyncio
async def test_refresh_of_parent_keeps_nested_version(db):
    s = await _bump(AsrvSafe(name="a"), 2)
    p = AsrvPlain(title="t", owner=s)
    await p.save()
    await p.refresh()
    assert p.owner._version == 2
    p.owner.name = "q"
    await p.owner.save()
    assert p.owner._version == 3


@pytest.mark.asyncio
async def test_list_of_three_references_at_different_versions_one_select(db):
    rows = [await _bump(AsrvSafe(name=f"r{n}"), bumps) for n, bumps in enumerate((0, 1, 3))]
    p = AsrvPlain(title="t", many=rows)
    await p.save()
    seen: list[str] = []
    conns = list(db.adapter._all_conns)
    for conn in conns:
        await conn.set_trace_callback(
            lambda q: seen.append(q) if q.lstrip().upper().startswith("SELECT") and "asrv_safes" in q else None
        )
    try:
        got = await AsrvPlain.from_id(p._id)
    finally:
        for conn in conns:
            await conn.set_trace_callback(None)
    assert [x._version for x in got.many] == [0, 1, 3]
    assert len(seen) == 1
    got.many[2].name = "w"
    await got.many[2].save()
    assert got.many[2]._version == 4
