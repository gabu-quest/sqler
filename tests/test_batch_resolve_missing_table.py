"""from_ids and query().all() read a reference into a dropped table as from_id does: ``None``
in a typed field; an untyped field is never followed and keeps the stored reference."""

import warnings
from dataclasses import dataclass
from typing import Any, Optional

import pytest
from sqler import SQLerDB, SQLerModel
from sqler.models.lite.model import SQLerLiteModel


class MtPTarget(SQLerModel):
    __tablename__ = "mt_p_targets"
    name: str = ""


class MtPTyped(SQLerModel):
    __tablename__ = "mt_p_typed"
    owner: Optional[MtPTarget] = None


class MtPUntyped(SQLerModel):
    __tablename__ = "mt_p_untyped"
    owner: Any = None


@dataclass
class MtLTarget(SQLerLiteModel):
    __tablename__ = "mt_l_targets"
    name: str = ""


@dataclass
class MtLTyped(SQLerLiteModel):
    __tablename__ = "mt_l_typed"
    owner: Optional[MtLTarget] = None


@dataclass
class MtLUntyped(SQLerLiteModel):
    __tablename__ = "mt_l_untyped"
    owner: Any = None


CASES = {
    "pydantic-typed": (MtPTarget, MtPTyped),
    "pydantic-untyped": (MtPTarget, MtPUntyped),
    "lite-typed": (MtLTarget, MtLTyped),
    "lite-untyped": (MtLTarget, MtLUntyped),
}


@pytest.fixture(params=sorted(CASES))
def world(request, tmp_path):
    target_cls, holder_cls = CASES[request.param]
    db = SQLerDB.on_disk(str(tmp_path / "mt.db"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        target_cls.set_db(db)
        holder_cls.set_db(db)
    t = target_cls(name="x")
    t.save()
    h = holder_cls(owner=t)
    h.save()
    db.adapter.execute(f"DROP TABLE {target_cls.__tablename__}")
    db.adapter.commit()
    untyped = request.param.endswith("-untyped")
    stored = {"_table": target_cls.__tablename__, "_id": t._id}
    yield holder_cls, h._id, stored if untyped else None
    db.close()


def test_from_id_value_is_none(world):
    cls, rid, expected = world
    assert cls.from_id(rid).owner == expected


def test_from_ids_matches_from_id(world):
    cls, rid, expected = world
    assert cls.from_ids([rid])[0].owner == expected


def test_query_all_matches_from_id(world):
    cls, rid, expected = world
    assert cls.query().all()[0].owner == expected
