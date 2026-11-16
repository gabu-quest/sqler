"""``is_dirty()`` and ``get_dirty_fields()``: a model nobody changed says so, one somebody
changed says so, on every base that has them and every way a model is loaded or saved.

``is_dirty()`` is False when ``save()`` would write what it would have written when the model
was loaded or last saved; ``get_dirty_fields()`` names the top-level fields that differ.
"""

import asyncio
import inspect
import math
import warnings
from dataclasses import dataclass, field
from typing import Optional

import msgspec
import pytest
from pairwise import all_pairs
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
    SQLerDB,
    SQLerModel,
    SQLerSafeModel,
)
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.msgspec.model import SQLerMsgspecModel

# ---- one set of classes per base: a child, a parent without and one with a reference field


class DtPydChild(SQLerModel):
    name: str = ""


class DtPydPlain(SQLerModel):
    title: str = ""
    n: int = 0
    tags: list[str] = []
    meta: dict = {}
    grid: list[list[int]] = []
    x: float = 0.0
    big: int = 0
    opt: Optional[str] = None


class DtPydLinked(SQLerModel):
    title: str = ""
    n: int = 0
    one: Optional[DtPydChild] = None


class DtPydSafeChild(SQLerSafeModel):
    name: str = ""


class DtPydSafePlain(SQLerSafeModel):
    title: str = ""
    n: int = 0
    tags: list[str] = []
    meta: dict = {}
    grid: list[list[int]] = []
    x: float = 0.0
    big: int = 0
    opt: Optional[str] = None


class DtPydSafeLinked(SQLerSafeModel):
    title: str = ""
    n: int = 0
    one: Optional[DtPydSafeChild] = None


@dataclass
class DtLiteChild(SQLerLiteModel):
    name: str = ""


@dataclass
class DtLitePlain(SQLerLiteModel):
    title: str = ""
    n: int = 0
    tags: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    grid: list = field(default_factory=list)
    x: float = 0.0
    big: int = 0
    opt: Optional[str] = None


@dataclass
class DtLiteLinked(SQLerLiteModel):
    title: str = ""
    n: int = 0
    one: Optional[DtLiteChild] = None


@dataclass
class DtLiteSafeChild(SQLerLiteSafeModel):
    name: str = ""


@dataclass
class DtLiteSafePlain(SQLerLiteSafeModel):
    title: str = ""
    n: int = 0
    tags: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    grid: list = field(default_factory=list)
    x: float = 0.0
    big: int = 0
    opt: Optional[str] = None


@dataclass
class DtLiteSafeLinked(SQLerLiteSafeModel):
    title: str = ""
    n: int = 0
    one: Optional[DtLiteSafeChild] = None


@dataclass
class DtALiteChild(AsyncSQLerLiteModel):
    name: str = ""


@dataclass
class DtALitePlain(AsyncSQLerLiteModel):
    title: str = ""
    n: int = 0
    tags: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    grid: list = field(default_factory=list)
    x: float = 0.0
    big: int = 0
    opt: Optional[str] = None


@dataclass
class DtALiteLinked(AsyncSQLerLiteModel):
    title: str = ""
    n: int = 0
    one: Optional[DtALiteChild] = None


@dataclass
class DtALiteSafeChild(AsyncSQLerLiteSafeModel):
    name: str = ""


@dataclass
class DtALiteSafePlain(AsyncSQLerLiteSafeModel):
    title: str = ""
    n: int = 0
    tags: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    grid: list = field(default_factory=list)
    x: float = 0.0
    big: int = 0
    opt: Optional[str] = None


@dataclass
class DtALiteSafeLinked(AsyncSQLerLiteSafeModel):
    title: str = ""
    n: int = 0
    one: Optional[DtALiteSafeChild] = None


class DtMsgPlain(SQLerMsgspecModel, kw_only=True):
    title: str = ""
    n: int = 0
    tags: list = msgspec.field(default_factory=list)
    meta: dict = msgspec.field(default_factory=dict)
    grid: list = msgspec.field(default_factory=list)
    x: float = 0.0
    big: int = 0
    opt: Optional[str] = None


# kind: (child, plain, linked, async)
KINDS = {
    "pyd": (DtPydChild, DtPydPlain, DtPydLinked, False),
    "pydsafe": (DtPydSafeChild, DtPydSafePlain, DtPydSafeLinked, False),
    "lite": (DtLiteChild, DtLitePlain, DtLiteLinked, False),
    "litesafe": (DtLiteSafeChild, DtLiteSafePlain, DtLiteSafeLinked, False),
    "alite": (DtALiteChild, DtALitePlain, DtALiteLinked, True),
    "alitesafe": (DtALiteSafeChild, DtALiteSafePlain, DtALiteSafeLinked, True),
    "msgspec": (None, DtMsgPlain, None, False),
}
LINKED_KINDS = [k for k, v in KINDS.items() if v[2] is not None]
WAYS = ("new+save", "from_id", "from_ids", "all", "first", "refresh", "load+save")


async def aw(value):
    return await value if inspect.isawaitable(value) else value


async def bind(kind, tag):
    """Bind the classes of ``kind`` to a fresh in-memory database; returns the database."""
    child, plain, linked, is_async = KINDS[kind]
    if is_async:
        db = AsyncSQLerDB.in_memory()
        await db.connect()
    else:
        db = SQLerDB.in_memory()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for cls, name in ((child, "child"), (plain, "plain"), (linked, "linked")):
            if cls is not None:
                cls.set_db(db, f"dt_{kind}_{name}_{tag}")
    return db


async def close(db):
    await aw(db.close())


async def load(cls, row, way):
    """``row`` (saved) as the way ``way`` gives it."""
    if way == "new+save":
        return row
    if way == "from_id":
        return await aw(cls.from_id(row._id))
    if way == "from_ids":
        got = await aw(cls.from_ids([row._id]))
        assert len(got) == 1
        return got[0]
    if way == "all":
        got = await aw(cls.query().all())
        assert len(got) == 1
        return got[0]
    if way == "first":
        return await aw(cls.query().first())
    if way == "refresh":
        got = await aw(cls.from_id(row._id))
        return await aw(got.refresh())
    assert way == "load+save"
    got = await aw(cls.from_id(row._id))
    return await aw(got.save())


def plain_row(cls):
    return cls(
        title="p", n=1, tags=["a", "b"], meta={"k": {"deep": 1}, "s": "v"}, grid=[[1, 2], [3]]
    )


# ---- 1. a model nobody changed says so -------------------------------------------------


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("kind", list(KINDS))
def test_unchanged_model_without_reference_field_is_not_dirty(kind, way):
    async def scenario():
        db = await bind(kind, "u")
        plain = KINDS[kind][1]
        row = await aw(plain_row(plain).save())
        got = await load(plain, row, way)
        out = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (False, set())


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("kind", LINKED_KINDS)
def test_unchanged_model_with_reference_set_is_not_dirty(kind, way):
    async def scenario():
        db = await bind(kind, "r")
        child, _, linked, _ = KINDS[kind]
        c = await aw(child(name="c").save())
        row = await aw(linked(title="p", n=1, one=c).save())
        got = await load(linked, row, way)
        out = (got.one.name, got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == ("c", False, set())


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("kind", LINKED_KINDS)
def test_child_reached_through_typed_reference_is_not_dirty(kind, way):
    async def scenario():
        db = await bind(kind, "c")
        child, _, linked, _ = KINDS[kind]
        c = await aw(child(name="c").save())
        row = await aw(linked(title="p", n=1, one=c).save())
        got = await load(linked, row, way)
        out = (got.one._id, got.one.is_dirty())
        got.one.name = "changed"
        out += (got.one.get_dirty_fields(), got.is_dirty())
        await close(db)
        return out

    # editing the child changes the child, not the reference its parent would write
    assert asyncio.run(scenario()) == (1, False, {"name"}, False)


@pytest.mark.parametrize("kind", LINKED_KINDS)
def test_reference_to_another_row_is_dirty(kind):
    async def scenario():
        db = await bind(kind, "o")
        child, _, linked, _ = KINDS[kind]
        a = await aw(child(name="same").save())
        b = await aw(child(name="same").save())
        row = await aw(linked(title="p", n=1, one=a).save())
        got = await aw(linked.from_id(row._id))
        got.one = b
        out = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (True, {"one"})


@pytest.mark.parametrize("kind", list(KINDS))
def test_new_instance_never_saved_is_dirty_in_every_field(kind):
    plain = KINDS[kind][1]
    inst = plain_row(plain)
    assert (inst.is_dirty(), inst.get_dirty_fields()) == (
        True,
        {"title", "n", "tags", "meta", "grid", "x", "big", "opt"},
    )


# ---- 2. a change made in place counts --------------------------------------------------


def _append(inst, v):
    inst.tags.append(f"x{v}")
    return "tags"


def _set_key(inst, v):
    inst.meta["new"] = v
    return "meta"


def _set_nested_key(inst, v):
    inst.meta["k"]["deep"] = v
    return "meta"


def _list_in_list(inst, v):
    inst.grid[0].append(v)
    return "grid"


EDITS = {
    "list append": _append,
    "dict key": _set_key,
    "nested dict key": _set_nested_key,
    "list in list": _list_in_list,
}


@pytest.mark.parametrize(*all_pairs(("kind", list(KINDS)), ("way", WAYS), ("edit", list(EDITS))))
def test_change_made_in_place_is_dirty_until_saved_and_seen_again(kind, way, edit):
    async def scenario():
        db = await bind(kind, "e")
        plain = KINDS[kind][1]
        row = await aw(plain_row(plain).save())
        got = await load(plain, row, way)
        name = EDITS[edit](got, 7)
        first = (got.is_dirty(), got.get_dirty_fields())
        await aw(got.save())
        saved = (got.is_dirty(), got.get_dirty_fields())
        EDITS[edit](got, 8)
        again = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return name, first, saved, again

    name, first, saved, again = asyncio.run(scenario())
    assert (first, saved, again) == ((True, {name}), (False, set()), (True, {name}))


# ---- 3. values that are the same, and values that compare oddly ------------------------


@pytest.mark.parametrize("kind", list(KINDS))
def test_field_set_to_an_equal_value_is_not_dirty(kind):
    async def scenario():
        db = await bind(kind, "q")
        plain = KINDS[kind][1]
        row = await aw(plain_row(plain).save())
        got = await aw(plain.from_id(row._id))
        got.n = got.n
        got.tags = ["a", "b"]
        got.meta = {"k": {"deep": 1}, "s": "v"}
        out = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (False, set())


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("kind", list(KINDS))
def test_large_int_unicode_and_none_unchanged_are_not_dirty(kind, way):
    async def scenario():
        db = await bind(kind, "v")
        plain = KINDS[kind][1]
        row = plain_row(plain)
        row.big = 10**30
        row.title = "日本語 ✓ \u0000 \U0001f600"
        row.opt = None
        row = await aw(row.save())
        got = await load(plain, row, way)
        out = (got.big, got.title, got.opt, got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (10**30, "日本語 ✓ \u0000 \U0001f600", None, False, set())


@pytest.mark.parametrize("kind", list(KINDS))
def test_nan_unchanged_after_save_is_not_dirty(kind):
    # SQLite stores NaN as null, so only the saved instance still holds it
    async def scenario():
        db = await bind(kind, "n")
        plain = KINDS[kind][1]
        row = plain_row(plain)
        row.x = float("nan")
        await aw(row.save())
        out = (math.isnan(row.x), row.is_dirty(), row.get_dirty_fields())
        row.x = 1.5
        out += (row.get_dirty_fields(),)
        await close(db)
        return out

    assert asyncio.run(scenario()) == (True, False, set(), {"x"})


# ---- 4. promoted columns, another table, save_many ---------------------------------------


class DtPromoted(SQLerModel):
    __promoted__ = {"status": "TEXT"}
    status: str = "new"
    tags: list[str] = []


@pytest.mark.parametrize("way", WAYS)
def test_model_with_promoted_column_unchanged_is_not_dirty(way):
    async def scenario():
        db = SQLerDB.in_memory()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            DtPromoted.set_db(db, "dt_promoted")
        row = DtPromoted(status="open", tags=["a"]).save()
        got = await load(DtPromoted, row, way)
        out = (got.is_dirty(),)
        got.tags.append("b")
        out += (got.get_dirty_fields(),)
        db.close()
        return out

    assert asyncio.run(scenario()) == (False, {"tags"})


@pytest.mark.parametrize("kind", ["pyd", "pydsafe", "lite", "litesafe", "msgspec"])
def test_model_loaded_through_using_another_table_is_not_dirty(kind):
    db = asyncio.run(bind(kind, "t"))
    plain = KINDS[kind][1]
    plain_row(plain).save(db=db)  # the class's own table
    db.adapter.execute(
        f"CREATE TABLE dt_{kind}_other (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL"
        + (", _version INTEGER NOT NULL DEFAULT 0" if "safe" in kind else "")
        + ")"
    )
    db.adapter.execute(
        f"INSERT INTO dt_{kind}_other (data) VALUES (?)",
        ['{"title": "o", "n": 2, "tags": ["t"], "meta": {}, "grid": [], "x": 0.0, "big": 0,'
         ' "opt": null}'],
    )
    db.adapter.commit()
    got = plain.using(db, f"dt_{kind}_other").all()
    assert len(got) == 1
    first = (got[0].title, got[0].is_dirty())
    got[0].tags.append("u")
    second = got[0].get_dirty_fields()
    got[0].save()
    third = got[0].is_dirty()
    db.close()
    assert (first, second, third) == (("o", False), {"tags"}, False)


@pytest.mark.parametrize("kind", ["pyd", "pydsafe", "lite", "litesafe", "msgspec"])
def test_save_many_leaves_models_not_dirty(kind):
    db = asyncio.run(bind(kind, "m"))
    plain = KINDS[kind][1]
    rows = plain.save_many([plain_row(plain), plain_row(plain)])
    out = [(r.is_dirty(), r.get_dirty_fields()) for r in rows]
    rows[1].tags.append("z")
    out.append(rows[1].get_dirty_fields())
    db.close()
    assert out == [(False, set()), (False, set()), {"tags"}]


# ---- 5. the async pydantic bases keep a snapshot that shares nothing ---------------------


class DtAsyncPlain(AsyncSQLerModel):
    title: str = ""
    tags: list[str] = []
    meta: dict = {}


class DtAsyncSafePlain(AsyncSQLerSafeModel):
    title: str = ""
    tags: list[str] = []
    meta: dict = {}


@pytest.mark.parametrize("cls", [DtAsyncPlain, DtAsyncSafePlain])
def test_async_pydantic_snapshot_is_the_row_and_shares_nothing(cls):
    async def scenario():
        db = AsyncSQLerDB.in_memory()
        await db.connect()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(db, f"dt_{cls.__name__.lower()}")
        await cls(title="a", tags=["x"], meta={"k": {"d": 1}}).save()
        got = (await cls.query().all())[0]
        got.tags.append("y")
        got.meta["k"]["d"] = 2
        snap = got._snapshot
        await db.close()
        return snap

    assert asyncio.run(scenario()) == {"title": "a", "tags": ["x"], "meta": {"k": {"d": 1}}}


@pytest.mark.parametrize("way", ("save", "save_many"))
@pytest.mark.parametrize("cls", [DtAsyncPlain, DtAsyncSafePlain])
def test_async_pydantic_snapshot_after_a_save_is_what_was_written(cls, way):
    async def scenario():
        db = AsyncSQLerDB.in_memory()
        await db.connect()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(db, f"dt_{cls.__name__.lower()}_{way}")
        row = cls(title="a", tags=["x"], meta={"k": {"d": 1}})
        if way == "save":
            await row.save()
        else:
            await cls.save_many([row])
        row.tags.append("y")
        snap = row._snapshot
        await db.close()
        return snap

    assert asyncio.run(scenario()) == {"title": "a", "tags": ["x"], "meta": {"k": {"d": 1}}}


# ---- 6. the snapshot is data: nothing done to a child changes its parent's snapshot ------


@dataclass
class DtLiteMany(SQLerLiteModel):
    many: list[DtLiteChild] = field(default_factory=list)


@dataclass
class DtLiteSafeMany(SQLerLiteSafeModel):
    many: list[DtLiteSafeChild] = field(default_factory=list)


@dataclass
class DtALiteMany(AsyncSQLerLiteModel):
    many: list[DtALiteChild] = field(default_factory=list)


@dataclass
class DtALiteSafeMany(AsyncSQLerLiteSafeModel):
    many: list[DtALiteSafeChild] = field(default_factory=list)


class DtPydMany(SQLerModel):
    many: list[DtPydChild] = []


class DtPydSafeMany(SQLerSafeModel):
    many: list[DtPydSafeChild] = []


MANY = {
    "pyd": DtPydMany,
    "pydsafe": DtPydSafeMany,
    "lite": DtLiteMany,
    "litesafe": DtLiteSafeMany,
    "alite": DtALiteMany,
    "alitesafe": DtALiteSafeMany,
}


@pytest.mark.parametrize("way", ("from_id", "all"))
@pytest.mark.parametrize("kind", LINKED_KINDS)
def test_swapped_reference_is_dirty_after_the_old_child_loses_its_id(kind, way):
    async def scenario():
        db = await bind(kind, "s")
        child, _, linked, _ = KINDS[kind]
        a = await aw(child(name="a").save())
        b = await aw(child(name="b").save())
        row = await aw(linked(title="p", n=1, one=a).save())
        got = await load(linked, row, way)
        old = got.one
        got.one = b
        old._id = None  # what delete() leaves on an instance
        out = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (True, {"one"})


@pytest.mark.parametrize("way", ("from_id", "all"))
@pytest.mark.parametrize("kind", list(MANY))
def test_typed_list_with_a_removed_child_that_lost_its_id_is_dirty(kind, way):
    async def scenario():
        db = await bind(kind, "m")
        child = KINDS[kind][0]
        many = MANY[kind]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            many.set_db(db, f"dt_{kind}_many_m")
        a = await aw(child(name="a").save())
        b = await aw(child(name="b").save())
        row = await aw(many(many=[a, b]).save())
        got = await load(many, row, way)
        first = got.many.pop(0)
        first._id = None
        out = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (True, {"many"})


@pytest.mark.parametrize("kind", LINKED_KINDS)
def test_parent_whose_current_child_was_never_saved_is_dirty_without_raising(kind):
    async def scenario():
        db = await bind(kind, "n")
        child, _, linked, _ = KINDS[kind]
        a = await aw(child(name="a").save())
        row = await aw(linked(title="p", n=1, one=a).save())
        got = await aw(linked.from_id(row._id))
        got.one = child(name="new")
        out = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (True, {"title", "n", "one"})


@pytest.mark.parametrize("kind", ["alite", "alitesafe"])
def test_async_lite_model_answers_the_same_inside_and_outside_a_coroutine(kind):
    async def scenario():
        db = await bind(kind, "k")
        plain = KINDS[kind][1]
        row = await plain_row(plain).save()
        got = await plain.from_id(row._id)
        inside = [(got.is_dirty(), got.get_dirty_fields())]
        got.title = "changed"
        inside.append((got.is_dirty(), got.get_dirty_fields()))
        await close(db)
        return got, inside

    got, inside = asyncio.run(scenario())
    assert (got.is_dirty(), got.get_dirty_fields()) == (True, {"title"})
    assert inside == [(False, set()), (True, {"title"})]


# ---- 5. the snapshot of a loaded model is its stored row -------------------------------


async def _delete_row_with_sql(db, table, rid):
    """sqler's ``delete()`` refuses a referenced row: remove it with plain SQL."""
    await aw(db.adapter.execute(f"DELETE FROM {table} WHERE _id = ?", [rid]))
    await aw(db.adapter.auto_commit())


@pytest.mark.parametrize("kind", ["pyd", "pydsafe"])
def test_reference_to_a_row_deleted_with_sql_reads_none_and_is_dirty(kind):
    way = "from_id"
    async def scenario():
        db = await bind(kind, "g")
        child, _, linked, _ = KINDS[kind]
        c = await aw(child(name="c").save())
        row = await aw(linked(title="p", n=1, one=c).save())
        await _delete_row_with_sql(db, child._table, c._id)
        got = await load(linked, row, way)
        out = (got.one, got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    # save() would write null over the stored reference
    assert asyncio.run(scenario()) == (None, True, {"one"})


@pytest.mark.parametrize("kind", ["lite", "litesafe"])
def test_lite_reference_to_a_row_deleted_with_sql_reads_none_and_is_dirty(kind):
    async def scenario():
        db = await bind(kind, "h")
        child, _, linked, _ = KINDS[kind]
        c = await aw(child(name="c").save())
        row = await aw(linked(title="p", n=1, one=c).save())
        await _delete_row_with_sql(db, child._table, c._id)
        got = await load(linked, row, "all")
        out = (got.one, got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    # a typed reference to a row that is gone reads as None; save() would write null
    assert asyncio.run(scenario()) == (None, True, {"one"})


@pytest.mark.parametrize("kind", ["alite", "alitesafe"])
def test_async_lite_reference_to_a_row_deleted_with_sql_reads_none_and_is_dirty(kind):
    async def scenario():
        db = await bind(kind, "i")
        child, _, linked, _ = KINDS[kind]
        c = await aw(child(name="c").save())
        row = await aw(linked(title="p", n=1, one=c).save())
        await _delete_row_with_sql(db, child._table, c._id)
        got = await load(linked, row, "all")
        out = (got.one, got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    # the async lite reader gives None for a row that is gone; save() would write null
    assert asyncio.run(scenario()) == (None, True, {"one"})


@pytest.mark.parametrize("way", ("from_id", "from_ids", "all", "refresh"))
@pytest.mark.parametrize("kind", list(KINDS))
def test_row_with_a_key_the_class_has_no_field_for_is_dirty_in_that_key(kind, way):
    """``save()`` writes the whole document, so it would drop the key."""

    async def scenario():
        db = await bind(kind, "y")
        plain = KINDS[kind][1]
        row = await aw(plain_row(plain).save())
        await aw(
            db.adapter.execute(
                f"UPDATE dt_{kind}_plain_y SET data = json_set(data, '$.legacy', 1) WHERE _id = ?",
                [row._id],
            )
        )
        await aw(db.adapter.auto_commit())
        got = await load(plain, row, way)
        out = (got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    assert asyncio.run(scenario()) == (True, {"legacy"})


@pytest.mark.parametrize("note", [{}, {"note": "n"}])
@pytest.mark.parametrize("kind", ["lite", "alite"])
def test_stored_reference_in_an_untyped_field_after_all_is_not_dirty(kind, note):
    """An untyped field is never followed: ``all()`` hands it the stored value, which is what
    ``save()`` would write (a dict with a key besides ``_table`` and ``_id`` is data, stored
    whole)."""

    async def scenario():
        db = await bind(kind, "x")
        child, plain, _, _ = KINDS[kind]
        c = await aw(child(name="c").save())
        ref = {"_table": child._table, "_id": c._id, **note}
        await aw(plain(title="p", meta={"ref": ref}).save())
        got = (await aw(plain.query().all()))[0]
        out = (got.meta["ref"], got.is_dirty(), got.get_dirty_fields())
        await close(db)
        return out

    ref = {"_table": f"dt_{kind}_child_x", "_id": 1, **note}
    assert asyncio.run(scenario()) == (ref, False, set())
