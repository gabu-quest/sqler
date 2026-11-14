"""``from_id`` and ``refresh`` load references with the batched loader ``from_ids`` uses.

``other`` is typed: it admits both classes, alone, in a list or inside a dict.

Counted through the driver's trace callback: a two-row cycle takes 3 SELECTs, a row
holding 20 references takes 2, whichever way the row is loaded.
"""

from dataclasses import dataclass
from typing import Optional, Union

import pytest
from sqler import SQLerDB, SQLerModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel


class PxA(SQLerModel):
    __tablename__ = "fx_px_a"
    name: str
    other: "Optional[Union[PxA, PxB, list, dict]]" = None


class PxB(SQLerModel):
    __tablename__ = "fx_px_b"
    name: str
    other: "Optional[Union[PxA, PxB, list, dict]]" = None


@dataclass
class LxA(SQLerLiteModel):
    __tablename__ = "fx_lx_a"
    name: str = ""
    other: "Optional[Union[LxA, LxB, list, dict]]" = None


@dataclass
class LxB(SQLerLiteModel):
    __tablename__ = "fx_lx_b"
    name: str = ""
    other: "Optional[Union[LxA, LxB, list, dict]]" = None


@dataclass
class LsA(SQLerLiteSafeModel):
    __tablename__ = "fx_ls_a"
    name: str = ""
    other: "Optional[Union[LsA, LsB, list, dict]]" = None


@dataclass
class LsB(SQLerLiteSafeModel):
    __tablename__ = "fx_ls_b"
    name: str = ""
    other: "Optional[Union[LsA, LsB, list, dict]]" = None


PxA.model_rebuild()
PxB.model_rebuild()

BASES = {"pydantic": (PxA, PxB), "lite": (LxA, LxB), "lite-safe": (LsA, LsB)}


def ref(cls, id_):
    return {"_table": cls.__tablename__, "_id": id_}


@pytest.fixture(params=sorted(BASES))
def world(request, tmp_path):
    a_cls, b_cls = BASES[request.param]
    db = SQLerDB.on_disk(str(tmp_path / "w.db"))
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        a_cls.set_db(db)
        b_cls.set_db(db)
    yield db, a_cls, b_cls
    db.close()


def selects(db, action):
    traced: list[str] = []
    conn = db.adapter._conn()
    conn.set_trace_callback(traced.append)
    try:
        result = action()
    finally:
        conn.set_trace_callback(None)
    return result, [s for s in traced if s.lstrip().upper().startswith("SELECT")]


def _doc_of(x):
    return x


def put(db, cls, **data):
    return db.insert_document(cls.__tablename__, data)


def test_from_id_of_a_two_row_cycle_runs_as_many_selects_as_from_ids(world):
    db, a_cls, b_cls = world
    a = put(db, a_cls, name="a", other=None)
    b = put(db, b_cls, name="b", other=ref(a_cls, a))
    db.upsert_document(a_cls.__tablename__, a, {"name": "a", "other": ref(b_cls, b)})
    got, from_id_selects = selects(db, lambda: a_cls.from_id(a))
    _, from_ids_selects = selects(db, lambda: a_cls.from_ids([a]))
    assert len(from_ids_selects) == 3
    assert len(from_id_selects) == 3
    assert got.name == "a"
    assert got.other.name == "b"
    assert got.other.other.name == "a"
    # the cycle is cut where the batched loader cuts it: a reference is left
    assert got.other.other.other == ref(b_cls, b)


def test_from_id_of_a_row_with_twenty_references_runs_two_selects(world):
    db, a_cls, b_cls = world
    ids = [put(db, b_cls, name=f"b{i}") for i in range(20)]
    a = put(db, a_cls, name="a", other=[ref(b_cls, i) for i in ids])
    got, n = selects(db, lambda: a_cls.from_id(a))
    assert len(n) == 2
    assert [x.name for x in got.other] == [f"b{i}" for i in range(20)]


def test_refresh_runs_as_many_selects_as_from_ids(world):
    db, a_cls, b_cls = world
    a = put(db, a_cls, name="a")
    b = put(db, b_cls, name="b", other=ref(a_cls, a))
    db.upsert_document(a_cls.__tablename__, a, {"name": "a", "other": ref(b_cls, b)})
    inst = a_cls.from_id(a)
    inst.name = "stale"
    _, n = selects(db, inst.refresh)
    assert len(n) == 3
    assert inst.name == "a"
    assert inst.other.name == "b"


def test_a_row_referencing_itself_is_cut_after_one_round(world):
    db, a_cls, _ = world
    a = put(db, a_cls, name="a")
    db.upsert_document(a_cls.__tablename__, a, {"name": "a", "other": ref(a_cls, a)})
    got, n = selects(db, lambda: a_cls.from_id(a))
    assert len(n) == 2
    assert got.other.name == "a"
    assert got.other.other == ref(a_cls, a)


def test_a_three_row_cycle_across_two_tables_takes_one_select_per_table_per_round(world):
    db, a_cls, b_cls = world
    a1 = put(db, a_cls, name="a1")
    b1 = put(db, b_cls, name="b1", other=ref(a_cls, a1))
    a2 = put(db, a_cls, name="a2", other=ref(b_cls, b1))
    db.upsert_document(a_cls.__tablename__, a1, {"name": "a1", "other": ref(a_cls, a2)})
    got, from_id_n = selects(db, lambda: a_cls.from_id(a1))
    _, from_ids_n = selects(db, lambda: a_cls.from_ids([a1]))
    assert len(from_id_n) == len(from_ids_n) == 4
    assert [got.name, got.other.name, got.other.other.name] == ["a1", "a2", "b1"]


def test_a_reference_to_a_deleted_row_reads_as_none(world):
    db, a_cls, b_cls = world
    b = put(db, b_cls, name="b")
    a = put(db, a_cls, name="a", other=ref(b_cls, b))
    db.delete_document(b_cls.__tablename__, b)
    assert a_cls.from_id(a).other is None


def test_a_list_holding_the_same_reference_twice_is_one_select_and_two_values(world):
    db, a_cls, b_cls = world
    b = put(db, b_cls, name="b")
    a = put(db, a_cls, name="a", other=[ref(b_cls, b), ref(b_cls, b)])
    got, n = selects(db, lambda: a_cls.from_id(a))
    assert len(n) == 2
    assert [x.name for x in got.other] == ["b", "b"]


def test_a_reference_nested_in_a_dict_is_loaded(world):
    db, a_cls, b_cls = world
    b = put(db, b_cls, name="b")
    a = put(db, a_cls, name="a", other={"deep": {"x": ref(b_cls, b)}})
    got, n = selects(db, lambda: a_cls.from_id(a))
    assert len(n) == 2
    assert got.other["deep"]["x"].name == "b"


def test_a_reference_to_a_table_that_does_not_exist_stays_a_reference(world):
    db, a_cls, _ = world
    dangling = {"_table": "fx_no_such_table", "_id": 1}
    a = put(db, a_cls, name="a", other=dangling)
    assert a_cls.from_id(a).other == dangling


def test_a_safe_row_whose_referenced_row_changed_version_loads_the_new_one():
    import warnings

    db = SQLerDB.in_memory(shared=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        LsA.set_db(db)
        LsB.set_db(db)
    b = LsB(name="b1").save()
    a = LsA(name="a", other=ref(LsB, b._id)).save()
    first = LsA.from_id(a._id)
    assert first.other.name == "b1"
    b.name = "b2"
    b.save()
    assert b._version == 1
    second = LsA.from_id(a._id)
    assert second.other.name == "b2"
    assert first.refresh().other.name == "b2"
    assert first._version == second._version == 0
    db.close()
