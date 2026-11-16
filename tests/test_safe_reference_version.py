"""A safe model reached through a reference keeps its stored version and snapshot."""

import warnings
from typing import Optional

import pytest

from sqler import SQLerDB, SQLerModel, SQLerSafeModel


class SrvSafe(SQLerSafeModel):
    __tablename__ = "srv_safes"
    name: str = ""


class SrvPlain(SQLerModel):
    __tablename__ = "srv_plains"
    title: str = ""
    owner: Optional[SrvSafe] = None
    many: Optional[list[SrvSafe]] = None


@pytest.fixture
def world(tmp_path):
    db = SQLerDB.on_disk(str(tmp_path / "srv.db"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        SrvSafe.set_db(db)
        SrvPlain.set_db(db)
    yield db
    db.close()


def _bump(row, times):
    row.save()
    for i in range(times):
        row.name = f"{row.name}{i}"
        row.save()
    return row


def _count_selects(db, fn, table):
    seen = []
    conn = db.adapter._conn()
    conn.set_trace_callback(
        lambda q: seen.append(q) if q.lstrip().upper().startswith("SELECT") and table in q else None
    )
    try:
        return fn(), seen
    finally:
        conn.set_trace_callback(None)


def test_from_id_gives_nested_safe_row_its_version_and_snapshot(world):
    s = _bump(SrvSafe(name="a"), 2)
    assert s._version == 2
    p = SrvPlain(title="t", owner=s)
    p.save()
    got = SrvPlain.from_id(p._id)
    assert got.owner._version == 2
    assert got.owner._snapshot == {"name": "a01"}
    got.owner.name = "z"
    got.owner.save()
    assert got.owner._version == 3


def test_refresh_of_parent_keeps_nested_version(world):
    s = _bump(SrvSafe(name="a"), 2)
    p = SrvPlain(title="t", owner=s)
    p.save()
    p.refresh()
    assert p.owner._version == 2
    p.owner.name = "q"
    p.owner.save()
    assert p.owner._version == 3


def test_list_of_three_references_at_different_versions_one_select(world):
    rows = [_bump(SrvSafe(name=f"r{n}"), bumps) for n, bumps in enumerate((0, 1, 3))]
    p = SrvPlain(title="t", many=rows)
    p.save()
    got, selects = _count_selects(world, lambda: SrvPlain.from_id(p._id), "srv_safes")
    assert [x._version for x in got.many] == [0, 1, 3]
    assert len(selects) == 1
    got.many[2].name = "w"
    got.many[2].save()
    assert got.many[2]._version == 4
