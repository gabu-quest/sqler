"""A value has one stored form, and every writer and every filter uses it.

``save()`` of every model base, ``update`` / ``update_one`` / ``update_n``, the raw document
writes of the database, and the value a filter binds all give a value the same form in the
row: a ``datetime``, ``date`` or ``time`` is its ``isoformat()``, a ``UUID`` or ``Path`` its
``str``, an ``Enum`` its value, a set a list (sorted when its elements sort), a tuple a list,
a nested pydantic model its fields; the same at every depth of a list or dict.
"""

import asyncio
import enum
import inspect
import json
import os
import pathlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, NamedTuple, Optional

import pydantic
import pytest
from sqler import AsyncSQLerDB, SQLerDB, SQLerModel, SQLerSafeModel
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.msgspec.model import SQLerMsgspecModel
from sqler.query import SQLerField as F

WHEN = datetime(2026, 11, 14, 9, 30, 0)
WHEN_TEXT = "2026-11-14T09:30:00"
WHEN_UTC = datetime(2026, 11, 14, 9, 30, 0, tzinfo=timezone.utc)
WHEN_UTC_TEXT = "2026-11-14T09:30:00+00:00"
DAY = date(2026, 11, 14)
DAY_TEXT = "2026-11-14"
AT = time(9, 30, 0)
AT_TEXT = "09:30:00"
UID = uuid.UUID("12345678-1234-5678-1234-567812345678")
UID_TEXT = "12345678-1234-5678-1234-567812345678"
PATH = pathlib.Path("a", "b")
PATH_TEXT = "a\\b" if os.sep == "\\" else "a/b"  # a path is stored as str(path): the writing OS's form


class Colour(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Mood(str, enum.Enum):
    CALM = "calm"
    LOUD = "loud"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 3


class Era(enum.Enum):
    START = date(2026, 11, 14)


class Inner(pydantic.BaseModel):
    day: date


class Slot(pydantic.BaseModel):
    at: datetime
    tags: set[str] = set()
    inner: Optional[Inner] = None


# Its elements do not sort, and the set itself gives them as 8, 1, (0,): stored by type name
# and repr, so the same set is the same list in every process.
MIXED = {8, 1, (0,)}


class Kind(NamedTuple):
    name: str
    ann: Any  # what a pydantic base declares the field as; None for ``Any``
    value: Any
    plain: Any  # what the row holds, as JSON read back
    other: Any  # another value of the kind


KINDS = [
    Kind("datetime", datetime, WHEN, WHEN_TEXT, WHEN + timedelta(days=1)),
    Kind("datetime_tz", datetime, WHEN_UTC, WHEN_UTC_TEXT, WHEN_UTC + timedelta(days=1)),
    Kind("date", date, DAY, DAY_TEXT, date(2026, 11, 15)),
    Kind("time", time, AT, AT_TEXT, time(10, 0, 0)),
    Kind("uuid", uuid.UUID, UID, UID_TEXT, uuid.UUID(int=7)),
    Kind("path", pathlib.Path, PATH, PATH_TEXT, pathlib.Path("c")),
    Kind("enum", Colour, Colour.RED, "red", Colour.BLUE),
    Kind("str_enum", Mood, Mood.CALM, "calm", Mood.LOUD),
    Kind("int_enum", Level, Level.HIGH, 3, Level.LOW),
    Kind("enum_of_date", None, Era.START, DAY_TEXT, "later"),
    Kind("set", set, {3, 1, 2}, [1, 2, 3], {4}),
    Kind("frozenset", frozenset, frozenset({2, 1}), [1, 2], frozenset({4})),
    Kind("empty_set", set, set(), [], {4}),
    Kind("mixed_set", None, MIXED, [1, 8, [0]], {4}),
    Kind("set_of_date", None, {date(2026, 11, 15), DAY}, [DAY_TEXT, "2026-11-15"], {4}),
    Kind("tuple", tuple, (1, 2), [1, 2], (3,)),
    Kind("list_of_date", list[date], [DAY], [DAY_TEXT], [date(2026, 11, 15)]),
    Kind("dict_of_date", dict[str, date], {"d": DAY}, {"d": DAY_TEXT}, {"d": date(2026, 11, 15)}),
    Kind("deep", None, [{"d": [DAY, (AT,)], "u": {UID}}], [{"d": [DAY_TEXT, [AT_TEXT]], "u": [UID_TEXT]}], []),
    Kind("nested_model", Inner, Inner(day=DAY), {"day": DAY_TEXT}, Inner(day=date(2026, 11, 15))),
    Kind(
        "nested_model_deep",
        Slot,
        Slot(at=WHEN, tags={"b", "a"}, inner=Inner(day=DAY)),
        {"at": WHEN_TEXT, "tags": ["a", "b"], "inner": {"day": DAY_TEXT}},
        Slot(at=WHEN),
    ),
]


def _pydantic(base, ann):
    return type("SvM", (base,), {"__annotations__": {"v": Any if ann is None else ann}, "v": None})


def _lite(base, ann):
    return dataclass(type("SvM", (base,), {"__annotations__": {"v": Any}, "v": None}))


def _msgspec(base, ann):
    return type("SvM", (base,), {"__annotations__": {"v": Any}, "v": None}, kw_only=True)


class Base(NamedTuple):
    cls: Any
    make: Any
    is_async: bool
    validates: bool  # a load gives the declared type back (pydantic); else the stored value


BASES = {
    "pydantic": Base(SQLerModel, _pydantic, False, True),
    "safe": Base(SQLerSafeModel, _pydantic, False, True),
    "lite": Base(SQLerLiteModel, _lite, False, False),
    "lite-safe": Base(SQLerLiteSafeModel, _lite, False, False),
    "msgspec": Base(SQLerMsgspecModel, _msgspec, False, False),
    "async": Base(AsyncSQLerModel, _pydantic, True, True),
    "async-safe": Base(AsyncSQLerSafeModel, _pydantic, True, True),
    "async-lite": Base(AsyncSQLerLiteModel, _lite, True, False),
    "async-lite-safe": Base(AsyncSQLerLiteSafeModel, _lite, True, False),
}
# the bases that have is_dirty() (the async pydantic bases have none)
DIRTY_BASES = [name for name in BASES if name not in {"async", "async-safe"}]


async def _aw(value):
    return await value if inspect.isawaitable(value) else value


class Rows:
    """One fresh in-memory database, sync or async, and the raw text of what it holds."""

    def __init__(self, is_async: bool):
        self.is_async = is_async

    async def __aenter__(self):
        if self.is_async:
            self.db = AsyncSQLerDB.in_memory(shared=False)
            await self.db.connect()
        else:
            self.db = SQLerDB.in_memory(shared=False)
        return self

    async def __aexit__(self, *exc):
        await _aw(self.db.close())

    async def sql(self, statement: str) -> list[tuple]:
        cur = await _aw(self.db.adapter.execute(statement))
        rows = [tuple(r) for r in await _aw(cur.fetchall())]
        await _aw(cur.close())
        return rows

    async def stored(self, table: str, field: str = "v") -> list:
        """``json_extract`` of ``field`` for each row: text, a number, or JSON text."""
        return [r[0] for r in await self.sql(f"SELECT json_extract(data, '$.{field}') FROM {table} ORDER BY _id")]

    async def documents(self, table: str) -> list[dict]:
        return [json.loads(r[0]) for r in await self.sql(f"SELECT data FROM {table} ORDER BY _id")]


def _text(plain):
    """What ``json_extract`` gives for the stored ``plain``: a scalar as it is, else JSON text."""
    return plain if isinstance(plain, (str, int)) else json.dumps(plain, separators=(",", ":"))


def _run(steps, is_async: bool):
    async def scenario():
        async with Rows(is_async) as rows:
            return await steps(rows)

    return asyncio.run(scenario())


_BASE_KIND = [pytest.param(b, k, id=f"{b}-{k.name}") for b in BASES for k in KINDS]


# ---- save -----------------------------------------------------------------------------


@pytest.mark.parametrize("base, kind", _BASE_KIND)
def test_save_stores_the_one_form_of_each_kind(base, kind):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, kind.ann)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=kind.value).save())
        loaded = await _aw(cls.from_id(saved._id))
        return await rows.stored("t"), loaded.v

    stored, loaded = _run(steps, spec.is_async)
    assert stored == [_text(kind.plain)]
    expect = kind.value if spec.validates and kind.ann is not None else kind.plain
    assert loaded == expect
    assert type(loaded) is type(expect)


@pytest.mark.parametrize("base", list(BASES))
@pytest.mark.parametrize("kind", [k for k in KINDS if k.name in {"time", "uuid", "set", "nested_model"}], ids=lambda k: k.name)
def test_save_many_stores_the_same_form(base, kind):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, kind.ann)
        cls.set_db(rows.db, "t")
        many = cls.asave_many if spec.is_async else cls.save_many
        await _aw(many([cls(v=kind.value), cls(v=kind.value)]))
        return await rows.stored("t")

    assert _run(steps, spec.is_async) == [_text(kind.plain)] * 2


@pytest.mark.parametrize("base", list(BASES))
@pytest.mark.parametrize(
    "value, kind_name",
    [(Decimal("1.10"), "Decimal"), (timedelta(hours=2), "timedelta"), (b"ab", "bytes")],
    ids=["decimal", "timedelta", "bytes"],
)
def test_a_kind_with_no_stored_form_is_refused_as_before(base, value, kind_name):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        with pytest.raises(TypeError, match=f"^Object of type {kind_name} is not JSON serializable$"):
            await _aw(cls(v=value).save())
        return await rows.sql("SELECT count(*) FROM t")

    assert _run(steps, spec.is_async) == [(0,)]


# ---- is_dirty -------------------------------------------------------------------------


@pytest.mark.parametrize("base, kind", [pytest.param(b, k, id=f"{b}-{k.name}") for b in DIRTY_BASES for k in KINDS])
def test_is_dirty_compares_stored_forms(base, kind):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, kind.ann)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=kind.value).save())
        loaded = await _aw(cls.from_id(saved._id))
        clean = (saved.is_dirty(), saved.get_dirty_fields(), loaded.is_dirty(), loaded.get_dirty_fields())
        loaded.v = kind.other
        return clean, (loaded.is_dirty(), loaded.get_dirty_fields())

    clean, changed = _run(steps, spec.is_async)
    assert clean == (False, set(), False, set())
    assert changed == (True, {"v"})


# ---- references beside other kinds ------------------------------------------------------


def _family(parent_base, child_base, make):
    if make is _pydantic:
        child = type("SvKid", (child_base,), {"__annotations__": {"label": str}, "label": ""})
        parent = type(
            "SvPar",
            (parent_base,),
            {"__annotations__": {"one": Optional[child], "items": list}, "one": None, "items": []},
        )
    elif make is _lite:
        child = dataclass(type("SvKid", (child_base,), {"__annotations__": {"label": str}, "label": ""}))
        parent = dataclass(
            type("SvPar", (parent_base,), {"__annotations__": {"one": Any, "items": Any}, "one": None, "items": None})
        )
    else:
        child = type("SvKid", (child_base,), {"__annotations__": {"label": str}, "label": ""}, kw_only=True)
        parent = type(
            "SvPar", (parent_base,), {"__annotations__": {"one": Any, "items": Any}, "one": None, "items": None}, kw_only=True
        )
    return parent, child


_FAMILIES = {
    "pydantic": (SQLerModel, SQLerModel, _pydantic, False),
    "async": (AsyncSQLerModel, AsyncSQLerModel, _pydantic, True),
    "async-holds-sync": (AsyncSQLerModel, SQLerModel, _pydantic, True),
    "lite": (SQLerLiteModel, SQLerLiteModel, _lite, False),
    "async-lite": (AsyncSQLerLiteModel, AsyncSQLerLiteModel, _lite, True),
    "msgspec": (SQLerMsgspecModel, SQLerMsgspecModel, _msgspec, False),
}


@pytest.mark.parametrize("family", list(_FAMILIES))
def test_a_reference_and_a_stored_reference_beside_a_date(family):
    parent_base, child_base, make, is_async = _FAMILIES[family]

    async def steps(rows):
        parent, child = _family(parent_base, child_base, make)
        if family == "async-holds-sync":
            child.set_db(SQLerDB.in_memory(shared=False), "sv_kids")
        else:
            child.set_db(rows.db, "sv_kids")
        parent.set_db(rows.db, "sv_pars")
        kid = await _aw(child(label="k").save())
        held = {"_table": "sv_kids", "_id": kid._id, "label": "not part of a reference"}
        await _aw(parent(one=kid, items=[kid, held, DAY, {"deep": [kid, AT]}]).save())
        return kid._id, (await rows.documents("sv_pars"))[0]

    kid_id, doc = _run(steps, is_async)
    ref = {"_table": "sv_kids", "_id": 1}
    assert kid_id == 1
    assert doc == {"one": ref, "items": [ref, ref, DAY_TEXT, {"deep": [ref, AT_TEXT]}]}


# ---- filters --------------------------------------------------------------------------


class SvEvent(SQLerModel):
    name: str = ""
    when: Optional[datetime] = None
    day: Optional[date] = None
    days: list[date] = []
    slots: list[dict[str, Any]] = []
    extra: Any = None


class SvAsyncEvent(AsyncSQLerModel):
    name: str = ""
    when: Optional[datetime] = None
    day: Optional[date] = None
    extra: Any = None


_TABLE = {SvEvent: "sv_events", SvAsyncEvent: "sv_async_events"}


@pytest.fixture
def events():
    """Three rows: ``a`` at WHEN on DAY, ``b`` one day later, ``c`` with no date at all."""
    db = SQLerDB.in_memory(shared=False)
    SvEvent.set_db(db, "sv_events")
    SvEvent(name="a", when=WHEN, day=DAY, days=[DAY], slots=[{"day": DAY}]).save()
    later = DAY + timedelta(days=1)
    SvEvent(name="b", when=WHEN + timedelta(days=1), day=later, days=[later], slots=[{"day": later}]).save()
    SvEvent(name="c").save()
    yield db
    db.close()


def _names(expression) -> list[str]:
    return [e.name for e in SvEvent.query().filter(expression).order_by("name").all()]


@pytest.mark.parametrize(
    "expression, names",
    [
        (lambda: F("when") == WHEN, ["a"]),
        (lambda: F("when") != WHEN, ["b"]),
        (lambda: F("when") >= WHEN, ["a", "b"]),
        (lambda: F("when") <= WHEN, ["a"]),
        (lambda: F("when") > WHEN, ["b"]),
        (lambda: F("when") < WHEN, []),
        (lambda: F("when").between(WHEN, WHEN), ["a"]),
        (lambda: F("when").in_list([WHEN]), ["a"]),
        (lambda: F("when").isin([WHEN]), ["a"]),
        (lambda: F("day") == DAY, ["a"]),
        (lambda: F("day").between(DAY, DAY), ["a"]),
        (lambda: F("days").contains(DAY), ["a"]),
        (lambda: F("days").isin([DAY]), ["a"]),
        (lambda: F("slots").any()["day"] == DAY, ["a"]),
    ],
    ids=["eq", "ne", "ge", "le", "gt", "lt", "between", "in_list", "isin", "date-eq", "date-between",
         "contains", "list-isin", "any"],
)
def test_a_filter_finds_what_save_stored(events, expression, names):
    assert _names(expression()) == names


@pytest.mark.parametrize(
    "kind",
    [k for k in KINDS if k.name in {"datetime_tz", "date", "time", "uuid", "path", "enum", "str_enum", "int_enum", "enum_of_date"}],
    ids=lambda k: k.name,
)
def test_a_filter_on_a_kind_finds_the_row_saved_with_it(kind):
    db = SQLerDB.in_memory(shared=False)
    try:
        SvEvent.set_db(db, "sv_events")
        SvEvent(name="hit", extra=kind.value).save()
        SvEvent(name="miss", extra=kind.other).save()
        SvEvent(name="none").save()
        assert _names(F("extra") == kind.value) == ["hit"]
        assert _names(F("extra").in_list([kind.value])) == ["hit"]
    finally:
        db.close()


def test_a_filter_through_a_reference_binds_the_stored_form():
    class SvOwner(SQLerModel):
        since: Optional[datetime] = None

    class SvPet(SQLerModel):
        name: str = ""
        sv_owner: Optional[SvOwner] = None

    db = SQLerDB.in_memory(shared=False)
    try:
        SvOwner.set_db(db, "sv_owners")
        SvPet.set_db(db, "sv_pets")
        SvPet(name="rex", sv_owner=SvOwner(since=WHEN).save()).save()
        SvPet(name="tom", sv_owner=SvOwner(since=WHEN + timedelta(days=1)).save()).save()
        found = SvPet.query().filter(SvPet.ref("sv_owner").field("since") == WHEN).all()
        assert [p.name for p in found] == ["rex"]
    finally:
        db.close()


def test_a_filter_on_none_stays_is_null(events):
    expression = F("when") == None  # noqa: E711 - the filter under test
    assert expression.sql == "JSON_EXTRACT(data, '$.when') IS NULL"
    assert expression.params == []
    assert _names(expression) == ["c"]
    negated = F("when") != None  # noqa: E711
    assert negated.sql == "JSON_EXTRACT(data, '$.when') IS NOT NULL"
    assert _names(negated) == ["a", "b"]


@pytest.mark.parametrize("value", ["text", 7, 2.5, True, False, 10**30], ids=repr)
def test_a_filter_on_a_plain_value_binds_it_as_it_is(value):
    for expression in (F("x") == value, F("x") >= value, F("x").contains(value), F("xs").any()["x"] == value):
        assert len(expression.params) == 1
        assert expression.params[0] is value
    low_high = F("x").between(value, value).params
    assert len(low_high) == 2 and low_high[0] is value and low_high[1] is value
    for many in (F("x").in_list([value, None]).params, F("x").isin([value, None]).params):
        assert len(many) == 2 and many[0] is value and many[1] is None


def test_a_filter_on_a_kind_with_no_stored_form_is_refused_as_before(events):
    with pytest.raises(sqlite3.ProgrammingError, match="type 'decimal.Decimal' is not supported"):
        SvEvent.query().filter(F("extra") == Decimal("1.10")).count()


def test_an_async_filter_finds_what_save_stored():
    async def steps(rows):
        SvAsyncEvent.set_db(rows.db, "sv_async_events")
        await SvAsyncEvent(name="a", when=WHEN, day=DAY, extra=UID).save()
        await SvAsyncEvent(name="b", when=WHEN + timedelta(days=1), day=DAY + timedelta(days=1)).save()
        return [
            await SvAsyncEvent.query().filter(F("when") == WHEN).count(),
            await SvAsyncEvent.query().filter(F("day") == DAY).count(),
            await SvAsyncEvent.query().filter(F("extra") == UID).count(),
            await SvAsyncEvent.query().filter(F("when").between(WHEN, WHEN)).count(),
        ]

    assert _run(steps, True) == [1, 1, 1, 1]


# ---- update, update_one, update_n --------------------------------------------------------

_UPDATES = {
    "update": lambda q, **f: q.update(**f),
    "update_one": lambda q, **f: q.update_one(**f),
    "update_n": lambda q, **f: q.update_n(5, **f),
}
_UPDATE_VALUES = [
    ("datetime", WHEN, WHEN_TEXT, "text"),
    ("datetime_tz", WHEN_UTC, WHEN_UTC_TEXT, "text"),
    ("date", DAY, DAY_TEXT, "text"),
    ("time", AT, AT_TEXT, "text"),
    ("uuid", UID, UID_TEXT, "text"),
    ("enum", Colour.RED, "red", "text"),
    ("int_enum", Level.HIGH, 3, "integer"),
    ("set", {2, 1}, "[1,2]", "array"),
    ("tuple", (DAY, AT), f'["{DAY_TEXT}","{AT_TEXT}"]', "array"),
    ("list_of_date", [DAY], f'["{DAY_TEXT}"]', "array"),
    ("dict_of_date", {"d": DAY}, f'{{"d":"{DAY_TEXT}"}}', "object"),
    ("nested_model", Inner(day=DAY), f'{{"day":"{DAY_TEXT}"}}', "object"),
    ("plain_list", [1, "a"], '[1,"a"]', "array"),
    ("plain_dict", {"k": [1]}, '{"k":[1]}', "object"),
    ("text", "plain", "plain", "text"),
    ("int", 5, 5, "integer"),
    ("true", True, 1, "true"),  # json_extract gives 1 for a JSON true; the type tells them apart
    ("false", False, 0, "false"),
]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("call", list(_UPDATES))
@pytest.mark.parametrize("name, value, text, json_type", _UPDATE_VALUES, ids=[u[0] for u in _UPDATE_VALUES])
def test_an_update_stores_the_form_save_stores(is_async, call, name, value, text, json_type):
    model = SvAsyncEvent if is_async else SvEvent

    async def steps(rows):
        model.set_db(rows.db, _TABLE[model])
        await _aw(model(name="a").save())
        await _aw(model(name="b").save())
        await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), extra=value))
        return await rows.sql(
            f"SELECT json_extract(data, '$.extra'), json_type(data, '$.extra') FROM {_TABLE[model]} ORDER BY _id"
        )

    # the row the filter left out still holds the JSON null it was saved with
    assert _run(steps, is_async) == [(text, json_type), (None, "null")]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("call", list(_UPDATES))
def test_a_row_updated_with_a_datetime_is_found_and_loads(is_async, call):
    model = SvAsyncEvent if is_async else SvEvent

    async def steps(rows):
        model.set_db(rows.db, _TABLE[model])
        row = await _aw(model(name="a").save())
        await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), when=WHEN_UTC, day=DAY))
        found = await _aw(model.query().filter(F("when") == WHEN_UTC).count())
        loaded = await _aw(model.from_id(row._id))
        return found, loaded.when, loaded.day, await rows.stored(_TABLE[model], "when")

    assert _run(steps, is_async) == (1, WHEN_UTC, DAY, [WHEN_UTC_TEXT])


# ---- raw document writes -----------------------------------------------------------------

RAW = {
    "when": WHEN,
    "utc": WHEN_UTC,
    "day": DAY,
    "at": AT,
    "uid": UID,
    "colour": Colour.RED,
    "level": Level.HIGH,
    "path": PATH,
    "set": {2, 1},
    "pair": (1, 2),
    "inner": Inner(day=DAY),
    "deep": [{"d": DAY, "s": {AT}}],
    "text": "t",
    "n": 1,
}
RAW_STORED = {
    "when": WHEN_TEXT,
    "utc": WHEN_UTC_TEXT,
    "day": DAY_TEXT,
    "at": AT_TEXT,
    "uid": UID_TEXT,
    "colour": "red",
    "level": 3,
    "path": PATH_TEXT,
    "set": [1, 2],
    "pair": [1, 2],
    "inner": {"day": DAY_TEXT},
    "deep": [{"d": DAY_TEXT, "s": [AT_TEXT]}],
    "text": "t",
    "n": 1,
}


async def _w_insert(db):
    await _aw(db.insert_document("raw", dict(RAW)))


async def _w_upsert_new(db):
    await _aw(db.upsert_document("raw", None, dict(RAW)))


async def _w_upsert_existing(db):
    rid = await _aw(db.insert_document("raw", {"text": "before"}))
    await _aw(db.upsert_document("raw", rid, dict(RAW)))


async def _w_bulk_new(db):
    await _aw(db.bulk_upsert("raw", [dict(RAW)]))


async def _w_bulk_existing(db):
    rid = await _aw(db.insert_document("raw", {"text": "before"}))
    await _aw(db.bulk_upsert("raw", [{"_id": rid, **RAW}]))


async def _w_insert_many(db):
    await _aw(db.ensure_table("raw"))
    await _aw(db.insert_many("raw", [dict(RAW)]))


async def _w_versioned_new(db):
    await _aw(db.upsert_with_version("raw", None, dict(RAW), None))


async def _w_versioned_existing(db):
    rid, version = await _aw(db.upsert_with_version("raw", None, {"text": "before"}, None))
    await _aw(db.upsert_with_version("raw", rid, dict(RAW), version))


async def _w_insert_many_versioned(db):
    await _aw(db.ensure_table("raw", versioned=True))
    await _aw(db.insert_many_with_version("raw", [dict(RAW)]))


_RAW_WRITES = {
    "insert_document": _w_insert,
    "upsert_document-new": _w_upsert_new,
    "upsert_document-existing": _w_upsert_existing,
    "bulk_upsert-new": _w_bulk_new,
    "bulk_upsert-existing": _w_bulk_existing,
    "insert_many": _w_insert_many,
    "upsert_with_version-new": _w_versioned_new,
    "upsert_with_version-existing": _w_versioned_existing,
    "insert_many_with_version": _w_insert_many_versioned,
}


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("write", list(_RAW_WRITES))
def test_a_raw_document_write_stores_the_form_save_stores(is_async, write):
    async def steps(rows):
        await _RAW_WRITES[write](rows.db)
        return await rows.documents("raw")

    assert _run(steps, is_async) == [RAW_STORED]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_raw_write_of_a_kind_with_no_stored_form_is_refused_as_before(is_async):
    async def steps(rows):
        with pytest.raises(TypeError, match="^Object of type Decimal is not JSON serializable$"):
            await _aw(rows.db.insert_document("raw", {"d": Decimal("1.10")}))
        return await rows.sql("SELECT count(*) FROM raw")

    assert _run(steps, is_async) == [(0,)]


# ---- promoted columns --------------------------------------------------------------------

_PROMOTED = {"stamp": "TEXT"}
PROMOTED_RAW = {"stamp": WHEN, **RAW}


async def _p_upsert_new(db):
    await _aw(db.upsert_document_promoted("raw", None, dict(PROMOTED_RAW), ["stamp"]))


async def _p_upsert_existing(db):
    rid = await _aw(db.upsert_document_promoted("raw", None, {"text": "before"}, ["stamp"]))
    await _aw(db.upsert_document_promoted("raw", rid, dict(PROMOTED_RAW), ["stamp"]))


async def _p_insert_many(db):
    await _aw(db.insert_many_promoted("raw", [dict(PROMOTED_RAW)], ["stamp"]))


async def _p_versioned_new(db):
    await _aw(db.upsert_with_version_promoted("raw", None, dict(PROMOTED_RAW), None, ["stamp"]))


async def _p_versioned_existing(db):
    rid, version = await _aw(db.upsert_with_version_promoted("raw", None, {"text": "before"}, None, ["stamp"]))
    await _aw(db.upsert_with_version_promoted("raw", rid, dict(PROMOTED_RAW), version, ["stamp"]))


async def _p_insert_many_versioned(db):
    await _aw(db.insert_many_with_version_promoted("raw", [dict(PROMOTED_RAW)], ["stamp"]))


_PROMOTED_WRITES = {
    "upsert_document_promoted-new": (_p_upsert_new, False),
    "upsert_document_promoted-existing": (_p_upsert_existing, False),
    "insert_many_promoted": (_p_insert_many, False),
    "upsert_with_version_promoted-new": (_p_versioned_new, True),
    "upsert_with_version_promoted-existing": (_p_versioned_existing, True),
    "insert_many_with_version_promoted": (_p_insert_many_versioned, True),
}


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("write", list(_PROMOTED_WRITES))
def test_a_promoted_column_write_stores_the_form_save_stores(is_async, write):
    call, versioned = _PROMOTED_WRITES[write]

    async def steps(rows):
        await _aw(rows.db.ensure_table("raw", promoted=_PROMOTED, versioned=versioned))
        await call(rows.db)
        return await rows.sql("SELECT stamp, typeof(stamp) FROM raw"), await rows.documents("raw")

    column, documents = _run(steps, is_async)
    assert column == [(WHEN_TEXT, "text")]
    assert documents == [RAW_STORED]


class SvPromoted(SQLerModel):
    __promoted__ = {"stamp": "TEXT"}
    name: str = ""
    stamp: Optional[datetime] = None


class SvAsyncPromoted(AsyncSQLerModel):
    __promoted__ = {"stamp": "TEXT"}
    name: str = ""
    stamp: Optional[datetime] = None


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_promoted_datetime_has_one_form_in_the_column_and_the_document(is_async):
    model = SvAsyncPromoted if is_async else SvPromoted

    async def steps(rows):
        model.set_db(rows.db, "sv_promoted")  # one table name: each run has its own database
        first = await _aw(model(name="a", stamp=WHEN).save())
        await _aw(model(name="b", stamp=WHEN + timedelta(days=1)).save())
        column = await rows.sql("SELECT stamp FROM sv_promoted ORDER BY _id")
        document = await _aw(rows.db.find_document("sv_promoted", first._id))
        found = [e.name for e in await _aw(model.query().filter(F("stamp") == WHEN).all())]
        await _aw(model.query().filter(F("name") == "b").update(stamp=WHEN_UTC))
        after = await rows.sql("SELECT stamp FROM sv_promoted ORDER BY _id")
        moved = [e.name for e in await _aw(model.query().filter(F("stamp") == WHEN_UTC).all())]
        return column, document["stamp"], found, after, moved

    column, in_document, found, after, moved = _run(steps, is_async)
    assert column == [(WHEN_TEXT,), ("2026-11-15T09:30:00",)]
    assert in_document == WHEN_TEXT
    assert found == ["a"]
    assert after == [(WHEN_TEXT,), (WHEN_UTC_TEXT,)]
    assert moved == ["b"]


class SvFlagged(SQLerModel):
    __promoted__ = {"done": "INTEGER"}
    name: str = ""
    done: bool = False


class SvAsyncFlagged(AsyncSQLerModel):
    __promoted__ = {"done": "INTEGER"}
    name: str = ""
    done: bool = False


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("call", list(_UPDATES))
def test_an_update_of_a_promoted_bool_binds_what_save_binds(is_async, call):
    """A column of its own takes a bool as SQLite's 1, from save() and from an update alike;
    only a field inside the document takes JSON's own true."""
    model = SvAsyncFlagged if is_async else SvFlagged

    async def steps(rows):
        model.set_db(rows.db, "sv_flagged")
        await _aw(model(name="saved", done=True).save())
        await _aw(model(name="updated").save())
        await _aw(model(name="left").save())
        await _aw(_UPDATES[call](model.query().filter(F("name") == "updated"), done=True))
        columns = await rows.sql("SELECT done, typeof(done) FROM sv_flagged ORDER BY _id")
        found = sorted(m.name for m in await _aw(model.query().filter(F("done") == True).all()))  # noqa: E712
        return columns, found

    columns, found = _run(steps, is_async)
    assert columns == [(1, "integer"), (1, "integer"), (0, "integer")]
    assert found == ["saved", "updated"]


# ---- a timezone-aware datetime, every way in ----------------------------------------------


def test_a_timezone_aware_datetime_has_one_form_every_way_in():
    tokyo = datetime(2026, 11, 14, 18, 30, 0, tzinfo=timezone(timedelta(hours=9)))
    text = "2026-11-14T18:30:00+09:00"
    db = SQLerDB.in_memory(shared=False)
    try:
        SvEvent.set_db(db, "sv_events")
        SvEvent(name="saved", when=tokyo).save()
        SvEvent(name="updated").save()
        SvEvent.query().filter(F("name") == "updated").update(when=tokyo)
        db.insert_document("sv_events", {"name": "raw", "when": tokyo})
        db.bulk_upsert("sv_events", [{"name": "bulk", "when": tokyo}])
        rows = db.adapter.execute("SELECT json_extract(data, '$.when') FROM sv_events ORDER BY _id").fetchall()
        assert [r[0] for r in rows] == [text] * 4
        assert _names(F("when") == tokyo) == ["bulk", "raw", "saved", "updated"]
        assert SvEvent.query().filter(F("name") == "updated").first().when == tokyo
    finally:
        db.close()
