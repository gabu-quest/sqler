"""Async: from_ids and query().all() read a reference into a dropped table as from_id does:
``None`` in a typed field; an untyped field is never followed and keeps the stored reference."""

import warnings
from dataclasses import dataclass
from typing import Any, Optional

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.adapter import AsyncSQLiteAdapter
from sqler.models.lite.async_model import AsyncSQLerLiteModel


class AmtPTarget(AsyncSQLerModel):
    __tablename__ = "amt_p_targets"
    name: str = ""


class AmtPTyped(AsyncSQLerModel):
    __tablename__ = "amt_p_typed"
    owner: Optional[AmtPTarget] = None


class AmtPUntyped(AsyncSQLerModel):
    __tablename__ = "amt_p_untyped"
    owner: Any = None


@dataclass
class AmtLTarget(AsyncSQLerLiteModel):
    __tablename__ = "amt_l_targets"
    name: str = ""


@dataclass
class AmtLTyped(AsyncSQLerLiteModel):
    __tablename__ = "amt_l_typed"
    owner: Optional[AmtLTarget] = None


@dataclass
class AmtLUntyped(AsyncSQLerLiteModel):
    __tablename__ = "amt_l_untyped"
    owner: Any = None


CASES = {
    "pydantic-typed": (AmtPTarget, AmtPTyped),
    "pydantic-untyped": (AmtPTarget, AmtPUntyped),
    "lite-typed": (AmtLTarget, AmtLTyped),
    "lite-untyped": (AmtLTarget, AmtLUntyped),
}


@pytest_asyncio.fixture(params=sorted(CASES))
async def world(request, tmp_path):
    target_cls, holder_cls = CASES[request.param]
    db = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / "amt.db"), pool_size=2))
    await db.connect()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        target_cls.set_db(db)
        holder_cls.set_db(db)
    t = target_cls(name="x")
    await t.save()
    h = holder_cls(owner=t)
    await h.save()
    await db.adapter.execute(f"DROP TABLE {target_cls.__tablename__}")
    await db.adapter.commit()
    untyped = request.param.endswith("-untyped")
    stored = {"_table": target_cls.__tablename__, "_id": t._id}
    yield holder_cls, h._id, stored if untyped else None
    await db.close()


@pytest.mark.asyncio
async def test_from_id_value_is_none(world):
    cls, rid, expected = world
    assert (await cls.from_id(rid)).owner == expected


@pytest.mark.asyncio
async def test_from_ids_matches_from_id(world):
    cls, rid, expected = world
    assert (await cls.from_ids([rid]))[0].owner == expected


@pytest.mark.asyncio
async def test_query_all_matches_from_id(world):
    cls, rid, expected = world
    assert (await cls.query().all())[0].owner == expected
