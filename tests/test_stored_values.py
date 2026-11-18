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
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, NamedTuple, Optional

import pydantic
import pytest
from pairwise import all_pairs

import sqler
from sqler import AsyncSQLerDB, SQLerDB, SQLerModel, SQLerSafeModel, UnstorableValueError
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.msgspec.async_model import AsyncSQLerMsgspecModel
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
PATH_TEXT = (
    "a\\b" if os.sep == "\\" else "a/b"
)  # a path is stored as str(path): the writing OS's form


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


class Priced(pydantic.BaseModel):
    price: Decimal


class Lasted(pydantic.BaseModel):
    took: timedelta


class Slot(pydantic.BaseModel):
    at: datetime
    tags: set[str] = set()
    inner: Optional[Inner] = None


# A dict with the two key names of a reference and more is data, stored and read whole; its
# ``_table`` names the table these tests save to, and ``_id`` the row saved first
WITH_REF_KEYS = {"_table": "t", "_id": 1, "label": "kept", "n": 3}

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
    Kind(
        "deep",
        None,
        [{"d": [DAY, (AT,)], "u": {UID}}],
        [{"d": [DAY_TEXT, [AT_TEXT]], "u": [UID_TEXT]}],
        [],
    ),
    Kind(
        "reference_keys_and_more",
        dict,
        dict(WITH_REF_KEYS),
        WITH_REF_KEYS,
        {"_table": "t", "_id": 1, "n": 4},
    ),
    Kind("nested_model", Inner, Inner(day=DAY), {"day": DAY_TEXT}, Inner(day=date(2026, 11, 15))),
    Kind("decimal", Decimal, Decimal("1.1"), 1.1, Decimal("2.5")),
    Kind("decimal_whole", Decimal, Decimal("5"), 5, Decimal("6")),
    Kind("decimal_trailing_zeros", Decimal, Decimal("5.00"), 5, Decimal("6")),
    Kind(
        "list_of_decimal", list[Decimal], [Decimal("1.1"), Decimal("2")], [1.1, 2], [Decimal("3")]
    ),
    Kind(
        "dict_of_decimal",
        dict[str, Decimal],
        {"p": Decimal("1.1")},
        {"p": 1.1},
        {"p": Decimal("2")},
    ),
    Kind(
        "set_of_decimal", set[Decimal], {Decimal("2.5"), Decimal("1.1")}, [1.1, 2.5], {Decimal("4")}
    ),
    Kind("tuple_of_decimal", None, (Decimal("1.1"), Decimal("2")), [1.1, 2], ()),
    Kind(
        "nested_model_decimal",
        Priced,
        Priced(price=Decimal("1.1")),
        {"price": 1.1},
        Priced(price=Decimal("2")),
    ),
    Kind("timedelta", timedelta, timedelta(minutes=90), 5400, timedelta(minutes=91)),
    Kind(
        "timedelta_fraction",
        timedelta,
        timedelta(minutes=90, microseconds=1),
        5400.000001,
        timedelta(seconds=1),
    ),
    Kind("timedelta_negative", timedelta, -timedelta(seconds=2), -2, timedelta(seconds=2)),
    Kind("list_of_timedelta", list[timedelta], [timedelta(hours=1)], [3600], [timedelta(hours=2)]),
    Kind(
        "dict_of_timedelta",
        dict[str, timedelta],
        {"t": timedelta(hours=1)},
        {"t": 3600},
        {"t": timedelta(hours=2)},
    ),
    Kind(
        "set_of_timedelta",
        set[timedelta],
        {timedelta(hours=2), timedelta(hours=1)},
        [3600, 7200],
        {timedelta(hours=3)},
    ),
    Kind("tuple_of_timedelta", None, (timedelta(hours=1), timedelta(seconds=1)), [3600, 1], ()),
    Kind(
        "nested_model_timedelta",
        Lasted,
        Lasted(took=timedelta(hours=1)),
        {"took": 3600},
        Lasted(took=timedelta(hours=2)),
    ),
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
    "async-msgspec": Base(AsyncSQLerMsgspecModel, _msgspec, True, False),
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
        return [
            r[0]
            for r in await self.sql(
                f"SELECT json_extract(data, '$.{field}') FROM {table} ORDER BY _id"
            )
        ]

    async def documents(self, table: str) -> list[dict]:
        return [json.loads(r[0]) for r in await self.sql(f"SELECT data FROM {table} ORDER BY _id")]


def _text(plain):
    """What ``json_extract`` gives for the stored ``plain``: a scalar as it is, else JSON text."""
    return (
        plain if isinstance(plain, (str, int, float)) else json.dumps(plain, separators=(",", ":"))
    )


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
@pytest.mark.parametrize(
    "kind",
    [k for k in KINDS if k.name in {"time", "uuid", "set", "nested_model"}],
    ids=lambda k: k.name,
)
def test_save_many_stores_the_same_form(base, kind):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, kind.ann)
        cls.set_db(rows.db, "t")
        await _aw(cls.save_many([cls(v=kind.value), cls(v=kind.value)]))
        return await rows.stored("t")

    assert _run(steps, spec.is_async) == [_text(kind.plain)] * 2


@pytest.mark.parametrize("base", list(BASES))
@pytest.mark.parametrize(
    "value, kind_name",
    [(object(), "object"), (range(2), "range")],
    ids=["object", "range"],
)
def test_a_kind_with_no_stored_form_is_refused_naming_the_field(base, value, kind_name):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        with pytest.raises(
            TypeError, match=rf"^Object of type {kind_name} is not JSON serializable \(at v\)$"
        ):
            await _aw(cls(v=value).save())
        return await rows.sql("SELECT count(*) FROM t")

    assert _run(steps, spec.is_async) == [(0,)]


# ---- is_dirty -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base, kind", [pytest.param(b, k, id=f"{b}-{k.name}") for b in DIRTY_BASES for k in KINDS]
)
def test_is_dirty_compares_stored_forms(base, kind):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, kind.ann)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=kind.value).save())
        loaded = await _aw(cls.from_id(saved._id))
        clean = (
            saved.is_dirty(),
            saved.get_dirty_fields(),
            loaded.is_dirty(),
            loaded.get_dirty_fields(),
        )
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
        child = dataclass(
            type("SvKid", (child_base,), {"__annotations__": {"label": str}, "label": ""})
        )
        parent = dataclass(
            type(
                "SvPar",
                (parent_base,),
                {"__annotations__": {"one": Any, "items": Any}, "one": None, "items": None},
            )
        )
    else:
        child = type(
            "SvKid", (child_base,), {"__annotations__": {"label": str}, "label": ""}, kw_only=True
        )
        parent = type(
            "SvPar",
            (parent_base,),
            {"__annotations__": {"one": Any, "items": Any}, "one": None, "items": None},
            kw_only=True,
        )
    return parent, child


_FAMILIES = {
    "pydantic": (SQLerModel, SQLerModel, _pydantic, False),
    "async": (AsyncSQLerModel, AsyncSQLerModel, _pydantic, True),
    "async-holds-sync": (AsyncSQLerModel, SQLerModel, _pydantic, True),
    "lite": (SQLerLiteModel, SQLerLiteModel, _lite, False),
    "async-lite": (AsyncSQLerLiteModel, AsyncSQLerLiteModel, _lite, True),
    "msgspec": (SQLerMsgspecModel, SQLerMsgspecModel, _msgspec, False),
    "async-msgspec": (AsyncSQLerMsgspecModel, AsyncSQLerMsgspecModel, _msgspec, True),
    "async-msgspec-holds-sync": (AsyncSQLerMsgspecModel, SQLerMsgspecModel, _msgspec, True),
}


@pytest.mark.parametrize("family", list(_FAMILIES))
def test_a_reference_and_a_stored_reference_beside_a_date(family):
    parent_base, child_base, make, is_async = _FAMILIES[family]

    async def steps(rows):
        parent, child = _family(parent_base, child_base, make)
        if family.endswith("holds-sync"):
            child.set_db(SQLerDB.in_memory(shared=False), "sv_kids")
        else:
            child.set_db(rows.db, "sv_kids")
        parent.set_db(rows.db, "sv_pars")
        kid = await _aw(child(label="k").save())
        held = {"_table": "sv_kids", "_id": kid._id, "label": "data, not a reference"}
        await _aw(parent(one=kid, items=[kid, held, DAY, {"deep": [kid, AT]}]).save())
        return kid._id, (await rows.documents("sv_pars"))[0]

    kid_id, doc = _run(steps, is_async)
    ref = {"_table": "sv_kids", "_id": 1}
    assert kid_id == 1
    held = {**ref, "label": "data, not a reference"}
    assert doc == {"one": ref, "items": [ref, held, DAY_TEXT, {"deep": [ref, AT_TEXT]}]}


@pytest.mark.parametrize("base", list(BASES))
def test_a_dict_with_the_reference_keys_and_more_is_read_back_whole(base):
    """The dict names this very table and the row saved: a load does not follow it."""
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, dict)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=dict(WITH_REF_KEYS)).save())
        return saved._id, (await _aw(cls.from_id(saved._id))).v

    assert _run(steps, spec.is_async) == (1, WITH_REF_KEYS)


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
    SvEvent(
        name="b", when=WHEN + timedelta(days=1), day=later, days=[later], slots=[{"day": later}]
    ).save()
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
    ids=[
        "eq",
        "ne",
        "ge",
        "le",
        "gt",
        "lt",
        "between",
        "in_list",
        "isin",
        "date-eq",
        "date-between",
        "contains",
        "list-isin",
        "any",
    ],
)
def test_a_filter_finds_what_save_stored(events, expression, names):
    assert _names(expression()) == names


@pytest.mark.parametrize(
    "kind",
    [
        k
        for k in KINDS
        if k.name
        in {
            "datetime_tz",
            "date",
            "time",
            "uuid",
            "path",
            "enum",
            "str_enum",
            "int_enum",
            "enum_of_date",
            "decimal",
            "decimal_whole",
        }
    ],
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
    for expression in (
        F("x") == value,
        F("x") >= value,
        F("x").contains(value),
        F("xs").any()["x"] == value,
    ):
        assert len(expression.params) == 1
        assert expression.params[0] is value
    low_high = F("x").between(value, value).params
    assert len(low_high) == 2 and low_high[0] is value and low_high[1] is value
    for many in (F("x").in_list([value, None]).params, F("x").isin([value, None]).params):
        assert len(many) == 2 and many[0] is value and many[1] is None


def test_a_filter_on_a_kind_with_no_stored_form_is_refused_as_before(events):
    with pytest.raises(sqlite3.ProgrammingError, match="type 'object' is not supported"):
        SvEvent.query().filter(F("extra") == object()).count()


def test_an_async_filter_finds_what_save_stored():
    async def steps(rows):
        SvAsyncEvent.set_db(rows.db, "sv_async_events")
        await SvAsyncEvent(name="a", when=WHEN, day=DAY, extra=UID).save()
        await SvAsyncEvent(
            name="b", when=WHEN + timedelta(days=1), day=DAY + timedelta(days=1)
        ).save()
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
    (
        "reference_keys_and_more",
        dict(WITH_REF_KEYS),
        '{"_table":"t","_id":1,"label":"kept","n":3}',
        "object",
    ),
    ("text", "plain", "plain", "text"),
    ("int", 5, 5, "integer"),
    ("decimal", Decimal("1.1"), 1.1, "real"),
    ("decimal_whole", Decimal("5.00"), 5, "integer"),
    ("timedelta_whole", timedelta(minutes=90), 5400, "integer"),
    ("timedelta_fraction", timedelta(seconds=1, microseconds=500000), 1.5, "real"),
    ("true", True, 1, "true"),  # json_extract gives 1 for a JSON true; the type tells them apart
    ("false", False, 0, "false"),
]


@pytest.mark.parametrize(
    *all_pairs(
        ("name, value, text, json_type", _UPDATE_VALUES, [u[0] for u in _UPDATE_VALUES]),
        ("call", list(_UPDATES)),
        ("is_async", [False, True], ["sync", "async"]),
    )
)
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
    "price": Decimal("1.1"),
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
    "price": 1.1,
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
def test_a_raw_write_of_a_kind_with_no_stored_form_is_refused_naming_the_place(is_async):
    async def steps(rows):
        doc = {"lines": [{"d": 1}, {"d": 2}, {"d": object()}]}
        with pytest.raises(
            TypeError,
            match=r"^Object of type object is not JSON serializable \(at lines\[2\]\.d\)$",
        ):
            await _aw(rows.db.insert_document("raw", doc))
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
    rid, version = await _aw(
        db.upsert_with_version_promoted("raw", None, {"text": "before"}, None, ["stamp"])
    )
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
        rows = db.adapter.execute(
            "SELECT json_extract(data, '$.when') FROM sv_events ORDER BY _id"
        ).fetchall()
        assert [r[0] for r in rows] == [text] * 4
        assert _names(F("when") == tokyo) == ["bulk", "raw", "saved", "updated"]
        assert SvEvent.query().filter(F("name") == "updated").first().when == tokyo
    finally:
        db.close()


# ---- Decimal ----------------------------------------------------------------------------
# A Decimal is stored as the JSON number it equals; one a number cannot hold exactly is refused.

_DECIMAL_TEXT = [
    ("1.1", "1.1"),
    ("5", "5"),
    ("-0.25", "-0.25"),
    ("5.00", "5"),  # a whole value is the integer: 5.00 and 5 are one number and one stored form
    ("9999999999999.99", "9999999999999.99"),
    ("-0", "0"),
    ("1E+2", "100"),
    ("0.000001", "1e-06"),
]
_REFUSED_DECIMALS = ["NaN", "Infinity", "-Infinity", "0.1234567890123456789", str(2**63), "1e400"]


@pytest.mark.parametrize("base", list(BASES))
@pytest.mark.parametrize("given, text", _DECIMAL_TEXT, ids=[d[0] for d in _DECIMAL_TEXT])
def test_a_decimal_is_stored_as_a_number_in_the_row_text(base, given, text):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, Decimal)
        cls.set_db(rows.db, "t")
        await _aw(cls(v=Decimal(given)).save())
        return await rows.sql("SELECT data FROM t")

    [(data,)] = _run(steps, spec.is_async)
    assert data == f'{{"v":{text}}}'


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("given, text", _DECIMAL_TEXT, ids=[d[0] for d in _DECIMAL_TEXT])
def test_a_decimal_in_a_document_write_is_a_number_in_the_row_text(is_async, given, text):
    async def steps(rows):
        await _aw(rows.db.insert_document("raw", {"v": Decimal(given), "xs": [Decimal(given)]}))
        return await rows.sql("SELECT data FROM raw")

    [(data,)] = _run(steps, is_async)
    assert data.count(text) == 2 and f'"{text}"' not in data


@pytest.mark.parametrize(
    *all_pairs(
        ("given, text", _DECIMAL_TEXT, [d[0] for d in _DECIMAL_TEXT]),
        ("call", list(_UPDATES)),
        ("is_async", [False, True], ["sync", "async"]),
    )
)
def test_a_decimal_in_an_update_is_a_number(is_async, call, given, text):
    model = SvAsyncEvent if is_async else SvEvent

    async def steps(rows):
        model.set_db(rows.db, _TABLE[model])
        await _aw(model(name="a").save())
        await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), extra=Decimal(given)))
        return await rows.sql(
            f"SELECT json_type(data, '$.extra'), json_extract(data, '$.extra') FROM {_TABLE[model]}"
        )

    [(kind, value)] = _run(steps, is_async)
    assert kind in {"integer", "real"}
    assert value == float(Decimal(given))


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_decimal_in_a_promoted_column_is_a_number(is_async):
    async def steps(rows):
        await _aw(rows.db.ensure_table("raw", promoted={"price": "REAL"}))
        await _aw(
            rows.db.upsert_document_promoted(
                "raw", None, {"price": Decimal("1.1"), "n": 1}, ["price"]
            )
        )
        with pytest.raises(TypeError, match="a number cannot hold it"):
            await _aw(
                rows.db.upsert_document_promoted("raw", None, {"price": Decimal("NaN")}, ["price"])
            )
        return await rows.sql("SELECT price, typeof(price) FROM raw")

    assert _run(steps, is_async) == [(1.1, "real")]


@pytest.mark.parametrize("given", _REFUSED_DECIMALS)
@pytest.mark.parametrize("base", list(BASES))
def test_a_decimal_a_number_cannot_hold_is_refused_on_save(base, given):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        await _aw(cls(v=1).save())
        with pytest.raises(
            TypeError,
            match=rf"^Decimal {re.escape(str(Decimal(given)))} cannot be stored: a number cannot hold it \(at v\)$",
        ):
            await _aw(cls(v=Decimal(given)).save())
        return await rows.sql("SELECT count(*) FROM t")

    assert _run(steps, spec.is_async) == [(1,)]


@pytest.mark.parametrize("given", _REFUSED_DECIMALS)
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_decimal_a_number_cannot_hold_is_refused_in_a_document_and_names_the_place(
    is_async, given
):
    async def steps(rows):
        doc = {"lines": [{"price": 1}, {"price": 2}, {"price": Decimal(given)}]}
        with pytest.raises(TypeError, match=r"a number cannot hold it \(at lines\[2\]\.price\)$"):
            await _aw(rows.db.insert_document("raw", doc))
        with pytest.raises(TypeError, match=r"\(at price\)$"):
            await _aw(rows.db.insert_document("raw", {"price": Decimal(given)}))
        return await rows.sql("SELECT count(*) FROM raw")

    assert _run(steps, is_async) == [(0,)]


@pytest.mark.parametrize(
    *all_pairs(
        ("is_async", [False, True], ["sync", "async"]),
        ("call", list(_UPDATES)),
        ("given", _REFUSED_DECIMALS),
    )
)
def test_a_decimal_a_number_cannot_hold_is_refused_in_an_update(is_async, call, given):
    model = SvAsyncEvent if is_async else SvEvent

    async def steps(rows):
        model.set_db(rows.db, _TABLE[model])
        await _aw(model(name="a").save())
        with pytest.raises(TypeError, match=r"a number cannot hold it"):
            await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), extra=Decimal(given)))
        return await rows.stored(_TABLE[model], "extra")

    assert _run(steps, is_async) == [None]


@pytest.mark.parametrize("given", _REFUSED_DECIMALS)
def test_a_decimal_a_number_cannot_hold_is_refused_in_a_filter(events, given):
    value = Decimal(given)
    for expression in (
        lambda: F("extra") == value,
        lambda: F("extra") >= value,
        lambda: F("extra").between(0, value),
        lambda: F("extra").isin([1, value]),
    ):
        with pytest.raises(TypeError, match=rf"{re.escape(str(value))}.*a number cannot hold it"):
            expression()


_PRICES = ["0.1", "0.3", "1.05", "1.1", "2", "-0.25", "10", "9.99"]


@pytest.fixture
def prices():
    db = SQLerDB.in_memory(shared=False)
    SvEvent.set_db(db, "sv_events")
    for text in _PRICES:
        SvEvent(name=text, extra=Decimal(text)).save()
    yield db
    db.close()


def _priced(expression) -> list[str]:
    return sorted(e.name for e in SvEvent.query().filter(expression).all())


@pytest.mark.parametrize("make", [Decimal, float, int], ids=["decimal", "float", "int"])
def test_a_filter_by_a_number_compares_numbers(prices, make):
    # 10 > 9.99 and 2 > 1.1: a text comparison would order them the other way
    one = Decimal("1.1")
    if make is Decimal:
        assert _priced(F("extra") == one) == ["1.1"]
        assert _priced(F("extra") != one) == sorted(set(_PRICES) - {"1.1"})
        assert _priced(F("extra") < one) == ["-0.25", "0.1", "0.3", "1.05"]
        assert _priced(F("extra") <= one) == ["-0.25", "0.1", "0.3", "1.05", "1.1"]
        assert _priced(F("extra") > one) == ["10", "2", "9.99"]
        assert _priced(F("extra") >= one) == ["1.1", "10", "2", "9.99"]
        assert _priced(F("extra").between(Decimal("1.1"), Decimal("9.99"))) == ["1.1", "2", "9.99"]
        assert _priced(F("extra").isin([Decimal("0.1"), Decimal("10"), Decimal("7")])) == [
            "0.1",
            "10",
        ]
        assert _priced(F("extra").in_list([Decimal("0.3")])) == ["0.3"]
    else:
        assert _priced(F("extra") > make(2)) == ["10", "9.99"]
        assert _priced(F("extra") >= make(2)) == ["10", "2", "9.99"]
        assert _priced(F("extra") == make(2)) == ["2"]
        assert _priced(F("extra").between(make(0), make(2))) == ["0.1", "0.3", "1.05", "1.1", "2"]


def test_a_decimal_sum_is_found_by_the_decimal_it_equals():
    db = SQLerDB.in_memory(shared=False)
    try:
        SvEvent.set_db(db, "sv_events")
        SvEvent(name="s", extra=Decimal("0.1") + Decimal("0.2")).save()
        assert _priced(F("extra") == Decimal("0.3")) == ["s"]
    finally:
        db.close()


def _typed(base_cls, **ann):
    kw = {"kw_only": True} if base_cls in (SQLerMsgspecModel, AsyncSQLerMsgspecModel) else {}
    return type("SvTyped", (base_cls,), {"__annotations__": ann, **{k: None for k in ann}}, **kw)


@pytest.mark.parametrize(
    "base", ["pydantic", "safe", "msgspec", "async", "async-safe", "async-msgspec"]
)
def test_a_decimal_field_loads_as_the_decimal_saved(base):
    spec = BASES[base]

    async def steps(rows):
        cls = _typed(spec.cls, v=Optional[Decimal], w=Optional[Decimal])
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=Decimal("0.1") + Decimal("0.2"), w=None).save())
        loaded = await _aw(cls.from_id(saved._id))
        again = await _aw(loaded.save())
        return loaded.v, type(loaded.v), loaded.w, await rows.sql("SELECT data FROM t"), again._id

    loaded, kind, none, data, _ = _run(steps, spec.is_async)
    assert (loaded, kind, none) == (Decimal("0.3"), Decimal, None)
    assert data[0][0].count("0.3") == 1


@pytest.mark.parametrize("base", ["pydantic", "safe", "lite", "lite-safe", "msgspec"])
def test_a_decimal_model_is_clean_after_load_and_saves_the_same_bytes(base):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, Decimal)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=Decimal("1.10")).save())
        before = await rows.documents("t")
        loaded = await _aw(cls.from_id(saved._id))
        clean = loaded.is_dirty()
        await _aw(loaded.save())
        after = await rows.documents("t")
        for doc in (before[0], after[0]):
            doc.pop("_version", None)  # a safe model counts its saves
        return clean, before == after

    assert _run(steps, spec.is_async) == (False, True)


class SubDecimal(Decimal):
    pass


@pytest.mark.parametrize("base", list(BASES))
def test_a_decimal_subclass_is_stored_as_a_number(base):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        await _aw(cls(v=[SubDecimal("1.1")]).save())
        return await rows.sql(
            "SELECT json_extract(data, '$.v[0]'), json_type(data, '$.v[0]') FROM t"
        )

    assert _run(steps, spec.is_async) == [(1.1, "real")]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_decimal_as_a_dict_key_is_refused_as_any_non_text_key_is(is_async):
    async def steps(rows):
        with pytest.raises(
            TypeError, match="keys must be str, int, float, bool or None, not decimal.Decimal"
        ):
            await _aw(rows.db.insert_document("raw", {"d": {Decimal("1.1"): 1}}))
        return await rows.sql("SELECT count(*) FROM raw")

    assert _run(steps, is_async) == [(0,)]


def test_a_refused_value_raises_the_sqler_error_with_its_place_and_value():
    bad = Decimal("0.1234567890123456789")

    async def steps(rows):
        with pytest.raises(UnstorableValueError) as err:
            await _aw(rows.db.insert_document("raw", {"lines": [0, {"price": bad}]}))
        return err.value

    err = _run(steps, False)
    assert (err.field, err.value) == ("lines[1].price", bad)
    assert (
        str(err) == f"Decimal {bad} cannot be stored: a number cannot hold it (at lines[1].price)"
    )
    assert (isinstance(err, TypeError), isinstance(err, sqler.SQLerError)) == (True, True)
    assert "UnstorableValueError" in sqler.__all__


def test_a_refused_dict_key_is_not_given_the_place_of_another_value():
    """The place names the value that was refused, never a different one further on."""

    async def steps(rows):
        with pytest.raises(TypeError) as err:
            await _aw(rows.db.insert_document("raw", {"d": {Decimal("1.1"): 1}, "x": object()}))
        return str(err.value), await rows.sql("SELECT count(*) FROM raw")

    message, count = _run(steps, False)
    assert message == "keys must be str, int, float, bool or None, not decimal.Decimal"
    assert count == [(0,)]


@pytest.mark.parametrize("base", ["lite", "lite-safe", "async-lite", "async-lite-safe"])
def test_a_lite_base_loads_a_decimal_as_the_number_stored(base):
    """A lite base decodes nothing by annotation: the field comes back as the JSON number."""
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, Decimal)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=Decimal("1.10")).save())
        loaded = await _aw(cls.from_id(saved._id))
        return loaded.v, type(loaded.v)

    assert _run(steps, spec.is_async) == (1.1, float)


def test_a_set_of_decimals_is_stored_in_one_order():
    async def steps(rows):
        await _aw(
            rows.db.insert_document(
                "raw", {"s": {Decimal("2.5"), Decimal("1.1"), Decimal("10"), Decimal("-1")}}
            )
        )
        return await rows.stored("raw", "s")

    assert [json.loads(t) for t in _run(steps, False)] == [[-1, 1.1, 2.5, 10]]


@pytest.mark.parametrize("base", ["safe", "async-safe"])
def test_a_decimal_beside_a_rebased_counter_is_saved_as_a_number(base):
    spec = BASES[base]

    async def steps(rows):
        cls = _typed(spec.cls, price=Decimal, count=int)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(price=Decimal("1.1"), count=0).save())
        one = await _aw(cls.from_id(saved._id))
        two = await _aw(cls.from_id(saved._id))
        one.count += 1
        await _aw(one.save())
        two.count += 1
        await _aw(two.save())
        return await rows.sql(
            "SELECT json_extract(data, '$.count'), json_type(data, '$.price') FROM t"
        )

    assert _run(steps, spec.is_async) == [(2, "real")]


@pytest.mark.parametrize("base", ["safe", "async-safe"])
@pytest.mark.parametrize("number", [float, Decimal], ids=["float", "decimal"])
def test_a_decimal_counter_field_behaves_as_a_float_one_does(base, number):
    spec = BASES[base]

    async def steps(rows):
        cls = _typed(spec.cls, price=number)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(price=number("1")).save())
        one = await _aw(cls.from_id(saved._id))
        two = await _aw(cls.from_id(saved._id))
        one.price = number("2")
        await _aw(one.save())
        two.price = number("3")
        try:
            await _aw(two.save())
        except Exception as err:
            return type(err).__name__
        return "saved"

    assert _run(steps, spec.is_async) == "StaleVersionError"


# ---- timedelta --------------------------------------------------------------------------
# A timedelta is stored as its number of seconds; one a number cannot hold exactly is refused.

_TD_REFUSED = [
    timedelta.max,
    timedelta.min + timedelta(microseconds=1),
    timedelta(days=999999999, microseconds=1),
]


class SubDelta(timedelta):
    pass


@pytest.mark.parametrize(
    "given, text",
    [
        (timedelta(0), "0"),
        (-timedelta(microseconds=1), "-1e-06"),
        (timedelta(microseconds=1), "1e-06"),
        (timedelta(days=999999999), "86399999913600"),
        (timedelta.min, "-86399999913600"),
        (SubDelta(seconds=90), "90"),
    ],
    ids=["zero", "negative_microsecond", "microsecond", "days_999999999", "min", "subclass"],
)
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_timedelta_in_a_document_write_is_a_number_in_the_row_text(is_async, given, text):
    async def steps(rows):
        await _aw(rows.db.insert_document("raw", {"v": given, "xs": [given]}))
        return await rows.sql("SELECT data FROM raw")

    [(data,)] = _run(steps, is_async)
    assert data.count(text) == 2


@pytest.mark.parametrize("given", _TD_REFUSED, ids=["max", "min_plus_one", "days_and_microsecond"])
@pytest.mark.parametrize("base", list(BASES))
def test_a_timedelta_no_number_holds_is_refused_on_save(base, given):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        await _aw(cls(v=1).save())
        with pytest.raises(
            UnstorableValueError,
            match=rf"^timedelta {re.escape(str(given))} cannot be stored: a number of seconds cannot hold it \(at v\)$",
        ):
            await _aw(cls(v=given).save())
        return await rows.sql("SELECT count(*) FROM t")

    assert _run(steps, spec.is_async) == [(1,)]


@pytest.mark.parametrize("given", _TD_REFUSED, ids=["max", "min_plus_one", "days_and_microsecond"])
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_timedelta_no_number_holds_is_refused_in_a_document_and_names_the_place(is_async, given):
    async def steps(rows):
        with pytest.raises(
            UnstorableValueError,
            match=r"a number of seconds cannot hold it \(at lines\[1\]\.took\)$",
        ):
            await _aw(rows.db.insert_document("raw", {"lines": [{"took": 1}, {"took": given}]}))
        return await rows.sql("SELECT count(*) FROM raw")

    assert _run(steps, is_async) == [(0,)]


@pytest.mark.parametrize(
    *all_pairs(
        ("is_async", [False, True], ["sync", "async"]),
        ("call", list(_UPDATES)),
        ("given", _TD_REFUSED, ["max", "min_plus_one", "days_and_microsecond"]),
    )
)
def test_a_timedelta_no_number_holds_is_refused_in_an_update(is_async, call, given):
    model = SvAsyncEvent if is_async else SvEvent

    async def steps(rows):
        model.set_db(rows.db, _TABLE[model])
        await _aw(model(name="a").save())
        with pytest.raises(UnstorableValueError, match="a number of seconds cannot hold it"):
            await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), extra=given))
        return await rows.stored(_TABLE[model], "extra")

    assert _run(steps, is_async) == [None]


@pytest.mark.parametrize("given", _TD_REFUSED, ids=["max", "min_plus_one", "days_and_microsecond"])
def test_a_filter_by_a_timedelta_no_number_holds_raises_the_sqler_error(events, given):
    for expression in (
        lambda: F("extra") == given,
        lambda: F("extra") < given,
        lambda: F("extra").between(timedelta(0), given),
        lambda: F("extra").isin([timedelta(0), given]),
        lambda: F("extra").in_list([given]),
    ):
        with pytest.raises(UnstorableValueError, match="a number of seconds cannot hold it"):
            expression()


@pytest.fixture
def took():
    db = SQLerDB.in_memory(shared=False)
    SvEvent.set_db(db, "sv_events")
    for name, delta in (
        ("three", timedelta(minutes=3)),
        ("ninety", timedelta(minutes=90)),
        ("day", timedelta(days=1)),
    ):
        SvEvent(name=name, extra=delta).save()
    yield db
    db.close()


def test_a_filter_by_a_timedelta_compares_the_seconds(took):
    five = timedelta(minutes=5)
    assert _priced(F("extra") > five) == ["day", "ninety"]
    assert _priced(F("extra") >= timedelta(minutes=3)) == ["day", "ninety", "three"]
    assert _priced(F("extra") < five) == ["three"]
    assert _priced(F("extra") == timedelta(seconds=5400)) == ["ninety"]
    assert _priced(F("extra") != timedelta(seconds=5400)) == ["day", "three"]
    assert _priced(F("extra").between(five, timedelta(hours=2))) == ["ninety"]
    assert _priced(F("extra").isin([timedelta(minutes=3), timedelta(days=1), timedelta(0)])) == [
        "day",
        "three",
    ]
    assert _priced(F("extra").in_list([timedelta(minutes=90)])) == ["ninety"]
    assert (
        SvEvent.query().filter(F("extra") > five).order_by("extra", desc=True).first().name == "day"
    )


def test_a_timedelta_sum_and_average_are_numbers(took):
    seconds = 180 + 5400 + 86400
    assert SvEvent.query().sum("extra") == seconds
    assert SvEvent.query().avg("extra") == seconds / 3


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_an_async_filter_by_a_timedelta_finds_the_row(is_async):
    model = SvAsyncEvent if is_async else SvEvent

    async def steps(rows):
        model.set_db(rows.db, _TABLE[model])
        for name, minutes in (("a", 3), ("b", 90)):
            await _aw(model(name=name, extra=timedelta(minutes=minutes)).save())
        found = await _aw(model.query().filter(F("extra") > timedelta(minutes=5)).all())
        return [r.name for r in found]

    assert _run(steps, is_async) == ["b"]


@pytest.mark.parametrize("base", ["pydantic", "safe", "msgspec", "async", "async-safe"])
@pytest.mark.parametrize(
    "given",
    [
        timedelta(minutes=90, microseconds=1),
        timedelta(0),
        -timedelta(microseconds=1),
        timedelta(days=999999999),
    ],
    ids=["fraction", "zero", "negative_microsecond", "days_999999999"],
)
def test_a_timedelta_field_loads_as_the_timedelta_saved(base, given):
    spec = BASES[base]

    async def steps(rows):
        cls = _typed(spec.cls, v=Optional[timedelta])
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=given).save())
        loaded = await _aw(cls.from_id(saved._id))
        return loaded.v, type(loaded.v)

    assert _run(steps, spec.is_async) == (given, timedelta)


@pytest.mark.parametrize("base", ["pydantic", "safe", "lite", "lite-safe", "msgspec"])
def test_a_timedelta_model_is_clean_after_load_and_saves_the_same_bytes(base):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, timedelta)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=timedelta(minutes=90, microseconds=1)).save())
        before = await rows.documents("t")
        loaded = await _aw(cls.from_id(saved._id))
        clean = loaded.is_dirty()
        await _aw(loaded.save())
        after = await rows.documents("t")
        for doc in (before[0], after[0]):
            doc.pop("_version", None)
        return clean, before == after

    assert _run(steps, spec.is_async) == (False, True)


@pytest.mark.parametrize("base", ["lite", "lite-safe", "async-lite", "async-lite-safe"])
def test_a_lite_base_loads_a_timedelta_as_the_number_stored(base):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, timedelta)
        cls.set_db(rows.db, "t")
        saved = await _aw(cls(v=timedelta(minutes=90)).save())
        loaded = await _aw(cls.from_id(saved._id))
        return loaded.v, type(loaded.v)

    assert _run(steps, spec.is_async) == (5400, int)


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_timedelta_in_a_promoted_column_is_a_number(is_async):
    async def steps(rows):
        await _aw(rows.db.ensure_table("raw", promoted={"took": "REAL"}))
        await _aw(
            rows.db.upsert_document_promoted(
                "raw", None, {"took": timedelta(seconds=90), "n": 1}, ["took"]
            )
        )
        with pytest.raises(UnstorableValueError, match="a number of seconds cannot hold it"):
            await _aw(
                rows.db.upsert_document_promoted("raw", None, {"took": timedelta.max}, ["took"])
            )
        return await rows.sql("SELECT took, typeof(took) FROM raw")

    assert _run(steps, is_async) == [(90.0, "real")]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_timedelta_as_a_dict_key_is_refused_as_any_non_text_key_is(is_async):
    async def steps(rows):
        with pytest.raises(
            TypeError, match="keys must be str, int, float, bool or None, not datetime.timedelta"
        ):
            await _aw(rows.db.insert_document("raw", {"d": {timedelta(1): 1}}))
        return await rows.sql("SELECT count(*) FROM raw")

    assert _run(steps, is_async) == [(0,)]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_timedelta_in_a_set_beside_a_decimal_is_stored_in_one_order(is_async):
    async def steps(rows):
        await _aw(rows.db.insert_document("raw", {"v": {timedelta(seconds=5), Decimal("1.5")}}))
        return await rows.documents("raw")

    assert [doc["v"] for doc in _run(steps, is_async)] == [[1.5, 5]]


# ---- bytes ------------------------------------------------------------------------------
# JSON has no binary value, so a document refuses bytes; a promoted BLOB column holds them.

BLOB = b"\x00\xffhi"
_BYTES_MESSAGE = (
    "bytes cannot be stored in a JSON document: promote the field to a BLOB column, or store text"
)


class SubBytes(bytes):
    pass


@pytest.mark.parametrize(
    "value",
    [b"", b"ab", bytearray(b"ab"), memoryview(b"ab"), SubBytes(b"ab")],
    ids=["empty", "bytes", "bytearray", "memoryview", "subclass"],
)
@pytest.mark.parametrize("base", list(BASES))
def test_bytes_in_a_document_are_refused_saying_where_they_go(base, value):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        with pytest.raises(UnstorableValueError) as err:
            await _aw(cls(v=value).save())
        return str(err.value), await rows.sql("SELECT count(*) FROM t")

    assert _run(steps, spec.is_async) == (f"{_BYTES_MESSAGE} (at v)", [(0,)])


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_bytes_nested_in_a_document_name_their_place(is_async):
    async def steps(rows):
        with pytest.raises(UnstorableValueError) as err:
            await _aw(rows.db.insert_document("raw", {"lines": [{"raw": 1}, {"raw": b"hi"}]}))
        return str(err.value), err.value.field, err.value.value

    assert _run(steps, is_async) == (f"{_BYTES_MESSAGE} (at lines[1].raw)", "lines[1].raw", b"hi")


@pytest.mark.parametrize("call", list(_UPDATES))
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_bytes_in_an_update_of_a_document_field_are_refused(is_async, call):
    model = SvAsyncEvent if is_async else SvEvent

    async def steps(rows):
        model.set_db(rows.db, _TABLE[model])
        await _aw(model(name="a").save())
        with pytest.raises(
            UnstorableValueError, match="^bytes cannot be stored in a JSON document"
        ):
            await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), extra=b"hi"))
        return await rows.stored(_TABLE[model], "extra")

    assert _run(steps, is_async) == [None]


@dataclass
class _LiteBlob(SQLerLiteModel):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


@dataclass
class _LiteAsyncBlob(AsyncSQLerLiteModel):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


class _MsgspecBlob(SQLerMsgspecModel, kw_only=True):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


class _MsgspecAsyncBlob(AsyncSQLerMsgspecModel, kw_only=True):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


class BlobRow(SQLerModel):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


class BlobSafeRow(SQLerSafeModel):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


class BlobAsyncRow(AsyncSQLerModel):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


class BlobAsyncSafeRow(AsyncSQLerSafeModel):
    __promoted__ = {"blob": "BLOB"}
    name: str = ""
    blob: Optional[bytes] = None


_BLOB_MODELS = {
    "pydantic": (BlobRow, False),
    "safe": (BlobSafeRow, False),
    "async": (BlobAsyncRow, True),
    "async-safe": (BlobAsyncSafeRow, True),
    "lite": (_LiteBlob, False),
    "async-lite": (_LiteAsyncBlob, True),
    "msgspec": (_MsgspecBlob, False),
    "async-msgspec": (_MsgspecAsyncBlob, True),
}


@pytest.mark.parametrize("call", list(_UPDATES))
@pytest.mark.parametrize("base", list(_BLOB_MODELS))
def test_a_promoted_blob_field_keeps_the_exact_bytes(base, call):
    model, is_async = _BLOB_MODELS[base]

    async def steps(rows):
        model.set_db(rows.db, "blobs")
        row = await _aw(model(name="a", blob=BLOB).save())
        loaded = await _aw(model.from_id(row._id))
        found = await _aw(model.query().filter(F("blob") == BLOB).all())
        await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), blob=b"\x01\x00"))
        after = await _aw(model.from_id(row._id))
        column = await rows.sql("SELECT blob, typeof(blob) FROM blobs")
        return loaded.blob, [r.name for r in found], after.blob, column

    assert _run(steps, is_async) == (BLOB, ["a"], b"\x01\x00", [(b"\x01\x00", "blob")])


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_promoted_blob_column_takes_bytes_in_a_document_write(is_async):
    async def steps(rows):
        await _aw(rows.db.ensure_table("raw", promoted={"blob": "BLOB"}))
        await _aw(rows.db.upsert_document_promoted("raw", None, {"blob": BLOB, "n": 1}, ["blob"]))
        return await rows.sql("SELECT blob, typeof(blob) FROM raw")

    assert _run(steps, is_async) == [(BLOB, "blob")]


def _promoted(spec):
    """A model of the base with ``v`` in the document and ``col`` as a promoted column."""
    attrs = {
        "__annotations__": {"v": Any, "col": Any},
        "v": None,
        "col": None,
        "__promoted__": {"col": "TEXT"},
    }
    if spec.cls in (SQLerMsgspecModel, AsyncSQLerMsgspecModel):
        return type("SvP", (spec.cls,), attrs, kw_only=True)
    cls = type("SvP", (spec.cls,), attrs)
    return cls if spec.validates else dataclass(cls)


def _nan():
    return Decimal("NaN")


REFUSED = [
    pytest.param(lambda: BLOB, f"{_BYTES_MESSAGE}", id="bytes"),
    pytest.param(_nan, "Decimal NaN cannot be stored: a number cannot hold it", id="decimal"),
    pytest.param(
        lambda: timedelta.max,
        f"timedelta {timedelta.max} cannot be stored: a number of seconds cannot hold it",
        id="timedelta",
    ),
    pytest.param(object, "Object of type object is not JSON serializable", id="object"),
]


def _refusal(spec, make_doc, promoted=False):
    """The error a save of ``make_doc(cls)`` raises, and how many rows the table holds after it."""

    async def steps(rows):
        cls = _promoted(spec) if promoted else spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        with pytest.raises(UnstorableValueError) as err:
            await _aw(make_doc(cls).save())
        if not await rows.sql("SELECT 1 FROM sqlite_master WHERE name = 't'"):
            return err.value, 0
        return err.value, (await rows.sql("SELECT count(*) FROM t"))[0][0]

    return _run(steps, spec.is_async)


_PLACED = [
    pytest.param(lambda cls, bad: cls(v=bad), "v", id="field"),
    pytest.param(lambda cls, bad: cls(v=[{"price": 1}, {"price": bad}]), "v[1].price", id="nested"),
    pytest.param(lambda cls, bad: cls(v=[1, bad, bad]), "v[1]", id="second-after-good"),
    pytest.param(
        lambda cls, bad: cls(v={"a": {"b": [bad]}, "c": bad}), "v.a.b[0]", id="first-in-order"
    ),
]


@pytest.mark.parametrize(
    *all_pairs(("base", list(BASES)), ("build, place", _PLACED), ("make_bad, message", REFUSED))
)
def test_a_refused_value_in_a_model_save_names_its_place(base, build, place, make_bad, message):
    spec = BASES[base]
    bad = make_bad()
    error, count = _refusal(spec, lambda cls: build(cls, bad))
    assert (error.field, str(error), count) == (place, f"{message} (at {place})", 0)
    assert error.value is bad


@pytest.mark.parametrize("base", list(BASES))
def test_a_refused_value_after_a_saved_row_is_named_and_not_the_row(base):
    """A row of its own in the value is stored as a reference; the place is the refused
    value's, one entry further on."""
    spec = BASES[base]
    bad = Decimal("NaN")

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        row = cls(v=1)
        await _aw(row.save())
        with pytest.raises(UnstorableValueError) as err:
            await _aw(cls(v=[row, bad]).save())
        return err.value, (await rows.sql("SELECT count(*) FROM t"))[0][0]

    error, count = _run(steps, spec.is_async)
    assert (error.field, str(error), count) == (
        "v[1]",
        "Decimal NaN cannot be stored: a number cannot hold it (at v[1])",
        1,
    )
    assert error.value is bad


@pytest.mark.parametrize(
    "make_bad, message, inner, attr",
    [
        pytest.param(
            lambda: Decimal("1e30"),
            "Decimal 1E+30 cannot be stored: a number cannot hold it",
            Priced,
            "price",
            id="decimal",
        ),
        pytest.param(lambda: timedelta.max, REFUSED[2].values[1], Lasted, "took", id="timedelta"),
    ],
)
@pytest.mark.parametrize("base", list(BASES))
def test_a_refused_value_in_a_model_held_by_the_saved_model_names_the_holders_field(
    base, make_bad, message, inner, attr
):
    spec = BASES[base]
    held = inner(**{attr: make_bad()})
    error, count = _refusal(spec, lambda cls: cls(v=held))
    place = f"v.{attr}"
    assert (error.field, str(error), count) == (place, f"{message} (at {place})", 0)


@pytest.mark.parametrize("base", list(BASES))
def test_a_dict_key_that_is_not_text_is_jsons_own_error_with_no_place(base):
    spec = BASES[base]

    async def steps(rows):
        cls = spec.make(spec.cls, None)
        cls.set_db(rows.db, "t")
        with pytest.raises(TypeError) as err:
            await _aw(cls(v={(1, 2): 1}).save())
        return type(err.value), await rows.sql("SELECT count(*) FROM t")

    assert _run(steps, spec.is_async) == (TypeError, [(0,)])


@pytest.mark.parametrize("make_bad, message", REFUSED[1:3])
@pytest.mark.parametrize("base", list(BASES))
def test_a_refused_value_in_a_promoted_column_names_the_column(base, make_bad, message):
    spec = BASES[base]
    bad = make_bad()
    error, count = _refusal(spec, lambda cls: cls(col=bad), promoted=True)
    assert (error.field, str(error), count) == ("col", f"{message} (at col)", 0)
    assert error.value is bad


@pytest.mark.parametrize("make_bad, message", REFUSED[1:3])
@pytest.mark.parametrize("base", [b for b in BASES if "safe" in b])
def test_a_refused_value_in_a_promoted_column_of_a_versioned_save_names_the_column(
    base, make_bad, message
):
    spec = BASES[base]
    bad = make_bad()
    error, count = _refusal(spec, lambda cls: cls(col=bad), promoted=True)
    assert (error.field, str(error), count) == ("col", f"{message} (at col)", 0)


class TagRow(SQLerModel):
    __promoted__ = {"tags": "TEXT"}
    name: str = ""
    tags: Optional[list] = None


class TagAsyncRow(AsyncSQLerModel):
    __promoted__ = {"tags": "TEXT"}
    name: str = ""
    tags: Optional[list] = None


@pytest.mark.parametrize("call", list(_UPDATES))
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_bytes_in_a_list_for_a_promoted_text_column_are_refused_by_an_update(is_async, call):
    """An update writes a list into a column as JSON text, so it refuses bytes as a document does."""
    model = TagAsyncRow if is_async else TagRow

    async def steps(rows):
        model.set_db(rows.db, "tagged")
        await _aw(model(name="a").save())
        with pytest.raises(
            UnstorableValueError, match="^bytes cannot be stored in a JSON document"
        ) as err:
            await _aw(_UPDATES[call](model.query().filter(F("name") == "a"), tags=["x", b"hi"]))
        return err.value.value, await rows.sql(
            "SELECT json_extract(data, '$.name'), tags FROM tagged"
        )

    assert _run(steps, is_async) == (b"hi", [("a", None)])
