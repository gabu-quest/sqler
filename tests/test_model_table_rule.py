"""One rule for the table a model class uses, read the same by every route.

A class that was never bound with ``set_db()`` and whose rows are written with
``save(db=db)`` must give the same table name from ``_resolve_binding``,
``using``, ``FTSIndex.table``, ``as_ref``, the stored reference of a parent that
holds it, and the registry.
"""

import gc
import warnings
import weakref
from dataclasses import dataclass
from typing import Any, Optional

import msgspec  # noqa: F401  (the msgspec base needs it installed)
import pytest
import pytest_asyncio
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerModel,
    FTSIndex,
    SQLerDB,
    SQLerLiteModel,
    SQLerModel,
    SQLerMsgspecModel,
    registry,
)
from sqler.exceptions import InvalidTableNameError
from sqler.models.ref import as_ref

# ----- classes that are never bound -------------------------------------------------


class TrPyItem(SQLerModel):
    name: str = ""


class TrPyNamed(SQLerModel):
    __tablename__ = "tr_py_named"
    name: str = ""


class TrPyHolder(SQLerModel):
    name: str = ""
    item: Optional[TrPyNamed] = None


class TrAsItem(AsyncSQLerModel):
    __tablename__ = "tr_as_items"
    name: str = ""


class TrAsHolder(AsyncSQLerModel):
    name: str = ""
    item: Optional[TrAsItem] = None


@dataclass
class TrLiteItem(SQLerLiteModel):
    __tablename__ = "tr_lite_items"
    name: str = ""


@dataclass
class TrLiteHolder(SQLerLiteModel):
    name: str = ""
    item: Optional[TrLiteItem] = None


@dataclass
class TrALiteItem(AsyncSQLerLiteModel):
    __tablename__ = "tr_alite_items"
    name: str = ""


@dataclass
class TrALiteHolder(AsyncSQLerLiteModel):
    name: str = ""
    item: Optional[TrALiteItem] = None


class TrMsItem(SQLerMsgspecModel):
    __tablename__ = "tr_ms_items"
    name: str = ""


class TrMsHolder(SQLerMsgspecModel):
    name: str = ""
    item: Optional[TrMsItem] = None


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    try:
        yield d
    finally:
        d.close()


@pytest_asyncio.fixture
async def adb():
    d = AsyncSQLerDB.in_memory(shared=False)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


def _sync_routes(cls, db):
    return {
        "resolve_binding": cls._resolve_binding(db)[1],
        "using": cls.using(db)._query._table,
        "fts": FTSIndex(cls, ["name"]).table,
    }


@pytest.mark.parametrize(
    "item_cls,holder_cls,expected",
    [
        (TrPyNamed, TrPyHolder, "tr_py_named"),
        (TrLiteItem, TrLiteHolder, "tr_lite_items"),
        (TrMsItem, TrMsHolder, "tr_ms_items"),
    ],
    ids=["pydantic", "lite", "msgspec"],
)
def test_unbound_sync_class_has_one_table_on_every_route(db, item_cls, holder_cls, expected):
    item = item_cls(name="it").save(db=db)
    holder = holder_cls(name="h", item=item).save(db=db)

    routes = _sync_routes(item_cls, db)
    holder_table = holder_cls._resolve_binding(db)[1]
    stored = db.find_document(holder_table, holder._id)
    routes["stored_ref"] = stored["item"]["_table"]

    assert routes == {name: expected for name in routes}
    assert registry.resolve(expected) is item_cls
    assert db.find_document(expected, item._id)["name"] == "it"


def test_unbound_default_name_is_the_plural_on_every_route(db):
    TrPyItem(name="x").save(db=db)
    routes = _sync_routes(TrPyItem, db)
    routes["as_ref"] = as_ref(TrPyItem(name="y").save(db=db))["_table"]
    assert routes == {name: "trpyitems" for name in routes}
    assert registry.resolve("trpyitems") is TrPyItem


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item_cls,holder_cls,expected",
    [
        (TrAsItem, TrAsHolder, "tr_as_items"),
        (TrALiteItem, TrALiteHolder, "tr_alite_items"),
    ],
    ids=["async-pydantic", "async-lite"],
)
async def test_unbound_async_class_has_one_table_on_every_route(adb, item_cls, holder_cls, expected):
    item = item_cls(name="it")
    await item.save(db=adb)
    holder = holder_cls(name="h", item=item)
    await holder.save(db=adb)

    routes = {
        "resolve_binding": item_cls._resolve_binding(adb)[1],
        "using": item_cls.using(adb)._query._table,
        "fts": FTSIndex(item_cls, ["name"]).table,
    }
    holder_table = holder_cls._resolve_binding(adb)[1]
    stored = await adb.find_document(holder_table, holder._id)
    routes["stored_ref"] = stored["item"]["_table"]

    assert routes == {name: expected for name in routes}
    assert registry.resolve(expected) is item_cls


def test_bound_class_then_using_another_db_reads_its_bound_table(db):
    """set_db(db, "a") binds the class to table ``a``; using(db2) reads ``a`` in db2."""

    class TrRebound(SQLerModel):
        name: str = ""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        TrRebound.set_db(db, "tr_a")
    db2 = SQLerDB.in_memory(shared=False)
    try:
        TrRebound(name="in-db2").save(db=db2)
        assert TrRebound.using(db2)._query._table == "tr_a"
        assert [r.name for r in TrRebound.using(db2).all()] == ["in-db2"]
        assert FTSIndex(TrRebound, ["name"]).table == "tr_a"
        assert TrRebound.count() == 0  # the bound db saw no write
    finally:
        db2.close()


def test_bad_tablename_on_a_never_bound_class_is_refused_before_sql(db):
    class TrBadName(SQLerModel):
        __tablename__ = "x; DROP TABLE y"
        name: str = ""

    with pytest.raises(InvalidTableNameError):
        TrBadName(name="a").save(db=db)
    with pytest.raises(InvalidTableNameError):
        TrBadName.using(db)
    with pytest.raises(InvalidTableNameError):
        FTSIndex(TrBadName, ["name"]).table


# ----- a class that is only defined never displaces a bound one ---------------------
#
# A bound class holds its table in the registry. A later class with the same table
# (defined elsewhere, e.g. inside a function) must not take its place: a stored
# reference keeps reading back as the bound class. The holder's field is untyped
# (``Any``) so that pydantic does not hide the registry's answer; the lite bases use
# a string annotation, which they resolve through the registry.


def _bind(cls, db, table=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db, table)


def _define_later_pydantic():
    class RdPyAuthor(SQLerModel):
        nick: str = "later"

    return RdPyAuthor


def _define_later_lite():
    @dataclass
    class RdLiteAuthor(SQLerLiteModel):
        nick: str = "later"

    return RdLiteAuthor


def _define_later_async_pydantic():
    class RdAsAuthor(AsyncSQLerModel):
        nick: str = "later"

    return RdAsAuthor


def _define_later_async_lite():
    @dataclass
    class RdALiteAuthor(AsyncSQLerLiteModel):
        nick: str = "later"

    return RdALiteAuthor


def test_later_definition_keeps_the_bound_pydantic_class(db):
    class RdPyAuthor(SQLerModel):
        name: str = ""

    class RdPyPost(SQLerModel):
        title: str = ""
        author: Any = None

    _bind(RdPyAuthor, db)
    _bind(RdPyPost, db)
    author = RdPyAuthor(name="ann").save()
    post = RdPyPost(title="t", author=author).save()
    bound = RdPyAuthor

    later = _define_later_pydantic()

    assert registry.resolve("rdpyauthors") is bound
    got = RdPyPost.from_id(post._id)
    assert type(got.author) is bound
    assert got.author.name == "ann"
    post.refresh()
    assert type(post.author) is bound
    assert post.author.name == "ann"
    # an untyped field read by a query holds the bound row's data
    assert RdPyPost.query().first().author == {"name": "ann", "_id": author._id}
    assert later is not bound


def test_later_definition_keeps_the_bound_lite_class(db):
    @dataclass
    class RdLiteAuthor(SQLerLiteModel):
        name: str = ""

    @dataclass
    class RdLitePost(SQLerLiteModel):
        title: str = ""
        author: "Optional[RdLiteAuthor]" = None

    _bind(RdLiteAuthor, db)
    _bind(RdLitePost, db)
    author = RdLiteAuthor(name="ann").save()
    post = RdLitePost(title="t", author=author).save()
    bound = RdLiteAuthor

    later = _define_later_lite()

    assert registry.resolve("rdliteauthors") is bound
    got = RdLitePost.from_id(post._id)
    assert type(got.author) is bound
    assert got.author.name == "ann"
    post.refresh()
    assert type(post.author) is bound
    queried = RdLitePost.query().first()
    assert type(queried.author) is bound
    assert queried.author.name == "ann"
    assert later is not bound


@pytest.mark.asyncio
async def test_later_definition_keeps_the_bound_async_pydantic_class(adb):
    class RdAsAuthor(AsyncSQLerModel):
        name: str = ""

    class RdAsPost(AsyncSQLerModel):
        title: str = ""
        author: Any = None

    _bind(RdAsAuthor, adb)
    _bind(RdAsPost, adb)
    author = await RdAsAuthor(name="ann").save()
    post = await RdAsPost(title="t", author=author).save()
    bound = RdAsAuthor

    later = _define_later_async_pydantic()

    assert registry.resolve("rdasauthors") is bound
    got = await RdAsPost.from_id(post._id)
    assert type(got.author) is bound
    assert got.author.name == "ann"
    await post.refresh()
    assert type(post.author) is bound
    assert (await RdAsPost.query().first()).author == {"name": "ann", "_id": author._id}
    assert later is not bound


@pytest.mark.asyncio
async def test_later_definition_keeps_the_bound_async_lite_class(adb):
    @dataclass
    class RdALiteAuthor(AsyncSQLerLiteModel):
        name: str = ""

    @dataclass
    class RdALitePost(AsyncSQLerLiteModel):
        title: str = ""
        author: "Optional[RdALiteAuthor]" = None

    _bind(RdALiteAuthor, adb)
    _bind(RdALitePost, adb)
    author = await RdALiteAuthor(name="ann").save()
    post = await RdALitePost(title="t", author=author).save()
    bound = RdALiteAuthor

    later = _define_later_async_lite()

    assert registry.resolve("rdaliteauthors") is bound
    got = await RdALitePost.from_id(post._id)
    assert type(got.author) is bound
    assert got.author.name == "ann"
    await post.refresh()
    assert type(post.author) is bound
    queried = await RdALitePost.query().first()
    assert type(queried.author) is bound
    assert later is not bound


def test_later_definition_garbage_collected_leaves_the_bound_class(db):
    class RdGcAuthor(SQLerModel):
        name: str = ""

    _bind(RdGcAuthor, db)
    bound = RdGcAuthor

    def define():
        class RdGcAuthor(SQLerModel):
            nick: str = ""

        return weakref.ref(RdGcAuthor)

    ref = define()
    gc.collect()
    assert ref() is None
    assert registry.resolve("rdgcauthors") is bound


def test_subclass_defined_after_the_bind_keeps_the_bound_class(db):
    class RdSubUser(SQLerModel):
        name: str = ""

    _bind(RdSubUser, db, "rd_sub_users")

    class RdSubAdmin(RdSubUser):
        level: int = 0

    assert registry.resolve("rd_sub_users") is RdSubUser


def test_later_definition_replaces_an_unbound_holder():
    class RdUnboundThing(SQLerModel):
        name: str = ""

    first = RdUnboundThing

    def define():
        class RdUnboundThing(SQLerModel):
            name: str = ""

        return RdUnboundThing

    second = define()
    assert registry.resolve("rdunboundthings") is second
    assert second is not first


def test_set_db_wins_over_a_bound_holder(db):
    class RdWinner(SQLerModel):
        name: str = ""

    _bind(RdWinner, db)

    def define():
        class RdWinner(SQLerModel):
            name: str = ""

        return RdWinner

    other = define()
    assert registry.resolve("rdwinners") is RdWinner
    _bind(other, db)
    assert registry.resolve("rdwinners") is other
