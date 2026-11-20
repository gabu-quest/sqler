"""Tests for SQLer msgspec models (Struct-based, C-level speed).

These tests verify that msgspec models provide API compatibility with
the dataclass-based lite models while using msgspec Structs.
"""

import asyncio
import inspect
import types
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import msgspec
import pytest
import pytest_asyncio

from sqler import (
    MSGSPEC_AVAILABLE,
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerMsgspecModel,
    NotBoundError,
    SQLerDB,
    SQLerLiteModel,
    SQLerMsgspecModel,
    SQLerMsgspecModelBase,
    is_msgspec_model,
)
from sqler.cache import CacheAwareModel, async_cached_query, cached_query, configure_cache
from sqler.exceptions import UnstorableValueError
from sqler.models.mixins import (
    AsyncAuditLogMixin,
    AsyncAuditMixin,
    AsyncFullMixin,
    AsyncHooksMixin,
    AuditLogMixin,
    AuditMixin,
    FullMixin,
    HooksMixin,
)
from sqler.query import F
from sqler.tracking import (
    AsyncPartialUpdateMixin,
    AsyncTrackedModel,
    PartialUpdateMixin,
    TrackedModel,
)

pytestmark = pytest.mark.skipif(not MSGSPEC_AVAILABLE, reason="msgspec not installed")


class TestCompatibilityHelpers:
    """Test msgspec compatibility detection helpers."""

    def test_msgspec_available(self):
        """msgspec should be available in test environment."""
        assert MSGSPEC_AVAILABLE is True

    def test_is_msgspec_model(self):
        """is_msgspec_model should correctly identify msgspec models."""

        class MsgUser(SQLerMsgspecModel):
            name: str

        assert is_msgspec_model(MsgUser) is True
        assert is_msgspec_model(SQLerMsgspecModel) is True
        assert is_msgspec_model(str) is False

    def test_is_msgspec_model_not_lite(self):
        """is_msgspec_model should not match lite models."""
        from sqler import SQLerLiteModel, is_lite_model

        assert is_lite_model(SQLerMsgspecModel) is False
        assert is_msgspec_model(SQLerLiteModel) is False


class TestSQLerMsgspecModelBase:
    """Test the base class functionality."""

    def test_model_validate(self):
        """model_validate should create instance from dict."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        user = User.model_validate({"name": "Alice", "age": 30})
        assert user.name == "Alice"
        assert user.age == 30

    def test_model_validate_with_id(self):
        """model_validate should handle _id field."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User.model_validate({"name": "Bob", "_id": 42})
        assert user.name == "Bob"
        assert user._id == 42

    def test_model_validate_ignores_extra(self):
        """model_validate should ignore unknown fields."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User.model_validate({"name": "Carol", "unknown_field": "value"})
        assert user.name == "Carol"
        assert not hasattr(user, "unknown_field")

    def test_model_dump(self):
        """model_dump should serialize to dict."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        user = User(name="Dave", age=25)
        data = user.model_dump()
        assert data == {"name": "Dave", "age": 25}

    def test_model_dump_excludes_private(self):
        """model_dump should exclude fields starting with underscore."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User(name="Eve")
        user._id = 100
        data = user.model_dump()
        assert "_id" not in data
        assert "_snapshot" not in data
        assert data == {"name": "Eve"}

    def test_model_dump_exclude_none(self):
        """model_dump with exclude_none should skip None values."""

        class User(SQLerMsgspecModelBase):
            name: str
            email: Optional[str] = None

        user = User(name="Frank", email=None)
        data = user.model_dump(exclude_none=True)
        assert "email" not in data
        assert data == {"name": "Frank"}

    def test_model_dump_exclude_set(self):
        """model_dump with exclude should skip specified fields."""

        class User(SQLerMsgspecModelBase):
            name: str
            secret: str

        user = User(name="Grace", secret="hidden")
        data = user.model_dump(exclude={"secret"})
        assert "secret" not in data
        assert data == {"name": "Grace"}

    def test_model_fields(self):
        """model_fields should return field metadata."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        mf = User.model_fields
        assert "name" in mf
        assert "age" in mf
        assert "_id" not in mf
        assert "_snapshot" not in mf

    def test_get_field_names(self):
        """get_field_names should return public field names."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        names = User.get_field_names()
        assert names == {"name", "age"}

    def test_datetime_serialization(self):
        """model_dump should serialize datetime to ISO format."""

        class Event(SQLerMsgspecModelBase):
            name: str
            when: datetime

        now = datetime(2024, 1, 15, 10, 30, 0)
        event = Event(name="Meeting", when=now)
        data = event.model_dump()
        assert data["when"] == "2024-01-15T10:30:00"

    def test_repr(self):
        """__repr__ should show class name and public fields."""

        class User(SQLerMsgspecModelBase):
            name: str
            age: int

        user = User(name="Alice", age=30)
        assert repr(user) == "User(name='Alice', age=30)"

    def test_repr_with_id(self):
        """__repr__ should show _id when set."""

        class User(SQLerMsgspecModelBase):
            name: str

        user = User(name="Alice")
        user._id = 5
        assert repr(user) == "User(_id=5, name='Alice')"

    def test_default_values(self):
        """Struct fields with defaults should work."""

        class Item(SQLerMsgspecModelBase):
            name: str
            count: int = 0
            tags: list[str] = []

        item = Item(name="test")
        assert item.name == "test"
        assert item.count == 0
        assert item.tags == []

    def test_list_field_roundtrip(self):
        """Lists should serialize and deserialize correctly."""

        class Item(SQLerMsgspecModelBase):
            tags: list[str] = []

        item = Item.model_validate({"tags": ["a", "b", "c"]})
        assert item.tags == ["a", "b", "c"]
        data = item.model_dump()
        assert data["tags"] == ["a", "b", "c"]


class TestMsgspecBaseValues:
    def test_date_serialization(self):
        """model_dump should serialize date to ISO format."""

        class Event(SQLerMsgspecModelBase):
            name: str
            day: date

        event = Event(name="Holiday", day=date(2024, 3, 15))
        data = event.model_dump()
        assert data["day"] == "2024-03-15"
        assert data["name"] == "Holiday"


# =============================================================================
# persistence: each case a row over the sync base (``SQLerMsgspecModel`` on a ``SQLerDB``)
# and the async one (``AsyncSQLerMsgspecModel`` on an ``AsyncSQLerDB``), in memory and on a
# file; the two answer alike
# =============================================================================

BASES = {"sync": SQLerMsgspecModel, "async": AsyncSQLerMsgspecModel}


async def aw(value):
    return await value if inspect.isawaitable(value) else value


async def open_db(kind: str, path=None):
    if kind == "async":
        db = AsyncSQLerDB.on_disk(str(path)) if path else AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        return db
    return SQLerDB.on_disk(str(path)) if path else SQLerDB.in_memory(shared=False)


def bind(cls, db, table=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db, table)


async def sql(db, statement: str, params=()) -> list:
    """The rows of ``statement``, run with SQL of the test's own, committed."""
    cur = await aw(db.adapter.execute(statement, list(params)))
    found = [tuple(r) for r in await aw(cur.fetchall())]
    await aw(cur.close())
    await aw(db.adapter.auto_commit())
    return found


class Kit:
    def __init__(self, kind, base, db, path):
        self.kind, self.base, self.db, self.path = kind, base, db, path


@pytest_asyncio.fixture(
    params=[(k, s) for k in BASES for s in ("memory", "file")], ids=lambda p: f"{p[0]}-{p[1]}"
)
async def kit(request, tmp_path):
    kind, storage = request.param
    path = tmp_path / "ms.db" if storage == "file" else None
    db = await open_db(kind, path)
    try:
        yield Kit(kind, BASES[kind], db, path)
    finally:
        await aw(db.close())


@pytest.mark.asyncio
class TestMsgspecPersistenceOnBothBases:
    async def test_basic_crud(self, kit):
        class User(kit.base):
            __tablename__ = "msg_users"
            name: str
            email: str

        bind(User, kit.db)
        user = User(name="Alice", email="alice@example.com")
        assert user._id is None
        await aw(user.save())
        loaded = await aw(User.from_id(user._id))
        assert (loaded._id, loaded.name, loaded.email) == (1, "Alice", "alice@example.com")

        user.name = "Alice Updated"
        await aw(user.save())
        assert (await aw(User.from_id(1))).name == "Alice Updated"

        await aw(user.delete())
        assert user._id is None
        assert await aw(User.from_id(1)) is None

    async def test_query_all_count_first_and_filter(self, kit):
        class Product(kit.base):
            __tablename__ = "msg_products"
            name: str
            price: float

        bind(Product, kit.db)
        assert await aw(Product.count()) == 0
        for name, price in (("Apple", 1.50), ("Banana", 0.75), ("Cherry", 3.00)):
            await aw(Product(name=name, price=price).save())

        assert await aw(Product.count()) == 3
        assert sorted(p.name for p in await aw(Product.all())) == ["Apple", "Banana", "Cherry"]
        assert sorted(p.name for p in await aw(Product.filter(F("price") > 1.00).all())) == [
            "Apple",
            "Cherry",
        ]
        first = await aw(Product.first())
        assert (first._id, first.name, first._snapshot) == (
            1,
            "Apple",
            {"name": "Apple", "price": 1.5},
        )

    async def test_from_ids_batch(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_batch_items"
            name: str

        bind(Item, kit.db)
        for name in ["A", "B", "C", "D"]:
            await aw(Item(name=name).save())
        assert [(i._id, i.name) for i in await aw(Item.from_ids([1, 3, 9]))] == [(1, "A"), (3, "C")]
        assert await aw(Item.from_ids([])) == []

    async def test_refresh_reads_the_row_again(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_refresh_items"
            value: int

        bind(Item, kit.db)
        item = await aw(Item(value=100).save())
        await sql(
            kit.db,
            "UPDATE msg_refresh_items SET data = ? WHERE _id = ?",
            ['{"value": 999}', item._id],
        )

        assert await aw(item.refresh()) is item
        assert (item.value, item._snapshot, item.is_dirty()) == (999, {"value": 999}, False)

    async def test_dirty_tracking(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_dirty_items"
            name: str
            value: int

        bind(Item, kit.db)
        item = Item(name="Test", value=1)
        assert item.is_dirty() is True
        await aw(item.save())
        assert item.is_dirty() is False
        item.value = 2
        assert item.get_dirty_fields() == {"value"}

    async def test_default_and_reserved_table_names(self, kit):
        class MsgCategory(kit.base):
            name: str

        class Table(kit.base):
            name: str

        bind(MsgCategory, kit.db)
        bind(Table, kit.db)
        assert (MsgCategory._table, Table._table) == ("msgcategories", "table_tbl")

    async def test_using_api(self, kit):
        class Task(kit.base):
            __tablename__ = "msg_tasks"
            title: str

        await aw(Task(title="First").save(db=kit.db))
        await aw(Task(title="Second").save(db=kit.db))
        assert sorted(t.title for t in await aw(Task.using(kit.db).all())) == ["First", "Second"]
        assert Task._db is None

    async def test_all_sets_the_id_and_snapshot_of_each_struct(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_meta_items"
            name: str
            value: int = 0

        bind(Item, kit.db)
        await aw(Item(name="A", value=10).save())
        await aw(Item(name="B", value=20).save())
        items = await aw(Item.query().order_by("name").all())
        assert [(i._id, i._snapshot) for i in items] == [
            (1, {"name": "A", "value": 10}),
            (2, {"name": "B", "value": 20}),
        ]

    async def test_as_dicts_bypass(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_dict_items"
            name: str
            value: int = 0

        bind(Item, kit.db)
        await aw(Item(name="X", value=42).save())
        assert await aw(Item.query().as_dicts()) == [{"_id": 1, "name": "X", "value": 42}]

    async def test_add_and_ensure_index(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_indexed"
            name: str = ""

        await aw(Item(name="a").save(db=kit.db))
        await aw(Item.add_index("name", unique=True, db=kit.db))
        await aw(Item.ensure_index("name", unique=True, db=kit.db))
        names = await sql(
            kit.db,
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'msg_indexed'",
        )
        assert names == [("idx_msg_indexed_name",)]

    async def test_first_or_create(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_foc"
            name: str = ""
            n: int = 0

        made, created = await aw(Item.first_or_create({"name": "a"}, {"n": 5}, db=kit.db))
        again, created_again = await aw(Item.first_or_create({"name": "a"}, {"n": 7}, db=kit.db))
        assert (made._id, made.n, created, again._id, again.n, created_again) == (
            1,
            5,
            True,
            1,
            5,
            False,
        )

    async def test_set_db_emits_one_deprecation_warning(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_warn_items"
            name: str

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Item.set_db(kit.db)
        assert [(w.category, str(w.message).split(".")[1]) for w in caught] == [
            (DeprecationWarning, "set_db() is deprecated")
        ]

    async def test_unbound_model_raises_not_bound_error(self, kit):
        class Ghost(kit.base):
            __tablename__ = "msg_ghost"
            name: str

        with pytest.raises(NotBoundError, match="^Model Ghost is not bound. Use .using"):
            await aw(Ghost.all())

    async def test_delete_and_refresh_of_an_unsaved_model_raise(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_unsaved"
            name: str

        with pytest.raises(ValueError, match="Cannot delete unsaved model"):
            await aw(Item(name="unsaved").delete(db=kit.db))
        with pytest.raises(ValueError, match=r"^Cannot refresh unsaved model \(missing _id\)$"):
            await aw(Item(name="unsaved").refresh(db=kit.db))

    async def test_refresh_of_a_deleted_row_raises_lookup_error(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_refresh_deleted"
            name: str

        item = await aw(Item(name="doomed").save(db=kit.db))
        await aw(kit.db.delete_document("msg_refresh_deleted", item._id))
        with pytest.raises(LookupError, match="^Row 1 not found for refresh$"):
            await aw(item.refresh())

    async def test_invalid_on_delete_raises_value_error(self, kit):
        class Item(kit.base):
            __tablename__ = "msg_bad_policy"
            name: str

        item = await aw(Item(name="test").save(db=kit.db))
        with pytest.raises(ValueError, match="on_delete must be"):
            await aw(item.delete_with_policy(on_delete="nuke"))
        assert await aw(Item.using(kit.db).count()) == 1

    async def test_a_reference_is_stored_and_loaded_as_its_model(self, kit):
        class Author(kit.base):
            __tablename__ = "msg_authors"
            name: str = ""

        class Post(kit.base):
            __tablename__ = "msg_posts"
            title: str = ""
            author: Optional[Author] = None

        author = await aw(Author(name="ann").save(db=kit.db))
        post = await aw(Post(title="t", author=author).save(db=kit.db))
        assert await aw(kit.db.find_document("msg_posts", post._id)) == {
            "_id": 1,
            "title": "t",
            "author": {"_table": "msg_authors", "_id": 1},
        }
        got = await aw(Post.from_id(1, db=kit.db))
        assert (type(got.author), got.author._id, got.author.name) == (Author, 1, "ann")


# ---- hostile inputs ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["memory", "file"])
async def test_eight_tasks_saving_new_rows_at_once_on_a_new_async_database(storage, tmp_path):
    """20 rounds, each a new model class on a new async database: all eight saves succeed."""
    got = []
    for round_ in range(20):
        cls = types.new_class(
            f"MsRace{round_}",
            (AsyncSQLerMsgspecModel,),
            {},
            lambda ns: ns.update(
                {"__tablename__": "ms_race", "__annotations__": {"n": int}, "n": 0}
            ),
        )
        db = await open_db("async", tmp_path / f"race{round_}.db" if storage == "file" else None)
        try:
            saved = await asyncio.gather(*(cls(n=i).save(db=db) for i in range(8)))
            got.append((sorted(s._id for s in saved), await cls.using(db).count()))
        finally:
            await db.close()
    assert got == [(list(range(1, 9)), 8)] * 20


@pytest.mark.asyncio
async def test_a_frozen_model_writes_its_row_then_refuses_its_id_on_both_bases(kit):
    """What a save of a frozen msgspec model does: the row is written, and setting the
    instance's ``_id`` is refused as msgspec refuses any assignment to a frozen Struct."""

    class Fz(kit.base, frozen=True):
        __tablename__ = "ms_frozen"
        name: str = ""

    with pytest.raises(AttributeError, match="^immutable type: 'Fz'$"):
        await aw(Fz(name="a").save(db=kit.db))
    assert [(m._id, m.name) for m in await aw(Fz.using(kit.db).all())] == [(1, "a")]


@pytest.mark.asyncio
async def test_a_kw_only_model_with_a_required_field_after_a_default(kit):
    class Kw(kit.base, kw_only=True):
        __tablename__ = "ms_kw"
        note: str = ""
        name: str

    saved = await aw(Kw(name="a").save(db=kit.db))
    got = await aw(Kw.from_id(saved._id, db=kit.db))
    assert (got._id, got.name, got.note) == (1, "a", "")


@pytest.mark.asyncio
async def test_a_camel_renamed_field_is_stored_under_its_python_name(kit):
    class Cm(kit.base, rename="camel"):
        __tablename__ = "ms_camel"
        first_name: str = ""

    saved = await aw(Cm(first_name="Ann").save(db=kit.db))
    assert await aw(kit.db.find_document("ms_camel", saved._id)) == {"_id": 1, "first_name": "Ann"}


def _renamed(base, shape):
    """A model whose ``first_name`` msgspec encodes under another name, in each way a class
    can say so."""
    if shape == "camel":

        class Cm(base, rename="camel"):
            __tablename__ = "ms_renamed"
            first_name: str = ""
            home_city: str = ""

    elif shape == "mapping":

        class Cm(base, rename={"first_name": "fn"}):
            __tablename__ = "ms_renamed"
            first_name: str = ""
            home_city: str = ""

    else:

        class Cm(base):
            __tablename__ = "ms_renamed"
            first_name: str = msgspec.field(default="", name="fn")
            home_city: str = ""

    return Cm


RENAMED = ["camel", "mapping", "field-name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", RENAMED)
async def test_a_renamed_field_loads_back_its_value(kit, shape):
    Cm = _renamed(kit.base, shape)
    saved = await aw(Cm(first_name="Ann", home_city="Oslo").save(db=kit.db))
    got = await aw(Cm.from_id(saved._id, db=kit.db))
    assert (got._id, got.first_name, got.home_city) == (1, "Ann", "Oslo")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", RENAMED)
async def test_a_loaded_model_with_a_renamed_field_saves_what_it_loaded(kit, shape):
    Cm = _renamed(kit.base, shape)
    await aw(Cm(first_name="Ann", home_city="Oslo").save(db=kit.db))
    got = await aw(Cm.from_id(1, db=kit.db))
    got.home_city = "Bergen"
    await aw(got.save(db=kit.db))
    assert await aw(kit.db.find_document("ms_renamed", 1)) == {
        "_id": 1,
        "first_name": "Ann",
        "home_city": "Bergen",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", RENAMED)
async def test_a_query_and_a_refresh_load_a_renamed_field(kit, shape):
    Cm = _renamed(kit.base, shape)
    held = await aw(Cm(first_name="Ann", home_city="Oslo").save(db=kit.db))
    await aw(Cm(first_name="Bo", home_city="Oslo").save(db=kit.db))
    found = await aw(Cm.using(kit.db).filter(F("first_name") == "Bo").all())
    await sql(
        kit.db, "UPDATE ms_renamed SET data = json_set(data, '$.first_name', 'Cy') WHERE _id = 1"
    )
    await aw(held.refresh(db=kit.db))
    assert ([(m._id, m.first_name) for m in found], held.first_name) == ([(2, "Bo")], "Cy")


class MsPoint(msgspec.Struct):
    x: int = 0
    y: int = 0


@pytest.mark.asyncio
async def test_a_datetime_a_decimal_and_a_struct_field_are_stored_and_loaded(kit):
    class Mv(kit.base):
        __tablename__ = "ms_values"
        when: datetime = datetime(2020, 1, 1)
        price: Decimal = Decimal("0")
        at: MsPoint = msgspec.field(default_factory=MsPoint)

    saved = await aw(
        Mv(when=datetime(2024, 5, 6, 7, 8, 9), price=Decimal("1.25"), at=MsPoint(3, 4)).save(
            db=kit.db
        )
    )
    assert await aw(kit.db.find_document("ms_values", saved._id)) == {
        "_id": 1,
        "when": "2024-05-06T07:08:09",
        "price": 1.25,
        "at": {"x": 3, "y": 4},
    }
    got = await aw(Mv.from_id(1, db=kit.db))
    assert (got.when, got.price, got.at) == (
        datetime(2024, 5, 6, 7, 8, 9),
        Decimal("1.25"),
        MsPoint(3, 4),
    )


@pytest.mark.asyncio
async def test_bytes_in_a_field_is_refused_with_its_path(kit):
    class Mb(kit.base):
        __tablename__ = "ms_bytes"
        name: str = ""
        raw: Optional[bytes] = None

    item = Mb(name="a", raw=b"\x00")
    with pytest.raises(
        UnstorableValueError, match=r"bytes cannot be stored in a JSON document.*\(at raw\)$"
    ) as caught:
        await aw(item.save(db=kit.db))
    assert (caught.value.field, item._id) == ("raw", None)


@pytest.mark.asyncio
async def test_a_subclass_with_more_fields_defined_after_its_base_was_used(kit):
    class MsBase(kit.base):
        __tablename__ = "ms_sub"
        name: str = ""

    await aw(MsBase(name="a").save(db=kit.db))
    (base,) = await aw(MsBase.using(kit.db).all())

    class MsSub(MsBase):
        extra: int = 0

    await aw(MsSub(name="b", extra=5).save(db=kit.db))
    subs = await aw(MsSub.using(kit.db).all())
    assert (base._id, base.name) == (1, "a")
    assert [(s._id, s.name, s.extra) for s in subs] == [(1, "a", 0), (2, "b", 5)]


@pytest.mark.asyncio
async def test_tablename_and_a_class_bound_with_another_table(kit):
    class Named(kit.base):
        __tablename__ = "ms_named"
        name: str = ""

    await aw(Named(name="own").save(db=kit.db))
    bind(Named, kit.db, "ms_other")
    await aw(Named(name="other").save())
    assert (Named._table, Named.__tablename__) == ("ms_other", "ms_other")
    assert await sql(kit.db, "SELECT json_extract(data, '$.name') FROM ms_named") == [("own",)]
    assert await sql(kit.db, "SELECT json_extract(data, '$.name') FROM ms_other") == [("other",)]


@pytest.mark.asyncio
async def test_a_save_in_a_transaction_that_rolls_back_leaves_no_row(kit):
    """The row is gone; the instance keeps the ``_id`` the insert gave it, as on every other
    base, sync and async (``SQLerLiteModel``, ``AsyncSQLerLiteModel``, ``AsyncSQLerModel``)."""

    class Tx(kit.base):
        __tablename__ = "ms_tx"
        name: str = ""

    item = Tx(name="x")
    with pytest.raises(RuntimeError, match="^undo$"):
        if kit.kind == "async":
            async with kit.db.transaction():
                await item.save(db=kit.db)
                raise RuntimeError("undo")
        else:
            with kit.db.transaction():
                item.save(db=kit.db)
                raise RuntimeError("undo")
    assert (item._id, await aw(Tx.using(kit.db).count())) == (1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(BASES))
async def test_a_row_saved_through_one_database_object_loads_through_a_second(kind, tmp_path):
    class Two(BASES[kind]):
        __tablename__ = "ms_two"
        name: str = ""

    first = await open_db(kind, tmp_path / "two.db")
    second = await open_db(kind, tmp_path / "two.db")
    try:
        saved = await aw(Two(name="a").save(db=first))
        got = await aw(Two.from_id(saved._id, db=second))
        assert (got._id, got.name) == (1, "a")
        got.name = "b"
        await aw(got.save())
        assert (await aw(Two.from_id(1, db=first))).name == "b"
    finally:
        await aw(first.close())
        await aw(second.close())


# ---- the database of the other kind is refused as on the lite pair ------------------


def _lite(base):
    return dataclass(
        types.new_class(
            "Wk", (base,), {}, lambda ns: ns.update({"__annotations__": {"name": str}, "name": ""})
        )
    )


def _msgspec(base):
    return types.new_class(
        "Wk", (base,), {}, lambda ns: ns.update({"__annotations__": {"name": str}, "name": ""})
    )


WRONG_KIND = {
    "lite-sync": (lambda: _lite(SQLerLiteModel), "async"),
    "lite-async": (lambda: _lite(AsyncSQLerLiteModel), "sync"),
    "msgspec-sync": (lambda: _msgspec(SQLerMsgspecModel), "async"),
    "msgspec-async": (lambda: _msgspec(AsyncSQLerMsgspecModel), "sync"),
}
CALLS = {
    "set_db": lambda cls, db: cls.set_db(db),
    "using": lambda cls, db: cls.using(db),
    "from_id": lambda cls, db: cls.from_id(1, db=db),
    "save": lambda cls, db: cls(name="a").save(db=db),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("call", list(CALLS))
@pytest.mark.parametrize("case", list(WRONG_KIND))
async def test_a_database_of_the_other_kind_is_refused_as_on_the_lite_pair(case, call):
    make, wrong = WRONG_KIND[case]
    cls = make()
    db = await open_db(wrong)
    try:
        with pytest.raises(TypeError) as caught:
            await aw(CALLS[call](cls, db))
    finally:
        await aw(db.close())
    takes, given = (
        ("an AsyncSQLerDB", "SQLerDB") if wrong == "sync" else ("a SQLerDB", "AsyncSQLerDB")
    )
    assert str(caught.value) == (
        f"Wk takes {takes} as its database, not an object of type {given}: a sync model reads and "
        "writes through a SQLerDB, an async model through an AsyncSQLerDB."
    )
    assert (cls._db, cls._table) == (None, None)


# ---- the mixins: the sync ones on the sync base, the async ones on the async base ----


def _with(kind: str, sync_mixins: tuple, async_mixins: tuple, name: str, fields: dict):
    """A class on the base of ``kind`` with its kind's mixins and ``fields`` ({name: default})."""
    mixins = async_mixins if kind == "async" else sync_mixins
    ns = {
        "__tablename__": f"ms_mx_{name}",
        "__annotations__": {k: type(v) for k, v in fields.items()},
        **fields,
    }
    return types.new_class(f"Mx{name}", (*mixins, BASES[kind]), {}, lambda body: body.update(ns))


@pytest.mark.asyncio
async def test_tracked_model_and_partial_update_write_the_changed_field_only(kit):
    cls = _with(
        kit.kind,
        (PartialUpdateMixin, TrackedModel),
        (AsyncPartialUpdateMixin, AsyncTrackedModel),
        "tracked",
        {"name": "", "note": ""},
    )
    item = await aw(cls(name="a", note="n").save(db=kit.db))
    assert (item.is_dirty, item.changed_fields) == (False, set())
    item.name = "b"
    assert (item.is_dirty, item.changed_fields, item.get_changes()) == (
        True,
        {"name"},
        {"name": ("a", "b")},
    )
    await sql(kit.db, "UPDATE ms_mx_tracked SET data = json_set(data, '$.note', 'elsewhere')")
    await aw(item.save_partial())
    assert item.is_dirty is False
    assert await aw(kit.db.find_document("ms_mx_tracked", 1)) == {
        "_id": 1,
        "name": "b",
        "note": "elsewhere",
    }


def test_the_sync_tracked_model_is_refused_on_the_async_base_as_on_the_async_lite_base():
    with pytest.raises(TypeError) as on_msgspec:
        types.new_class("MxRefused", (TrackedModel, AsyncSQLerMsgspecModel), {}, lambda ns: None)
    with pytest.raises(TypeError) as on_lite:
        types.new_class("MxRefused", (TrackedModel, AsyncSQLerLiteModel), {}, lambda ns: None)
    assert (
        str(on_msgspec.value)
        == str(on_lite.value)
        == (
            "MxRefused is an async model: use AsyncTrackedModel (and AsyncPartialUpdateMixin) in place "
            "of TrackedModel (and PartialUpdateMixin)"
        )
    )


@pytest.mark.asyncio
async def test_a_cached_query_is_fresh_after_a_save(kit):
    configure_cache()
    try:
        cls = _with(kit.kind, (CacheAwareModel,), (CacheAwareModel,), "cached", {"name": ""})
        runs = []
        if kit.kind == "async":

            @async_cached_query(table=cls, key="ms_names")
            async def names():
                runs.append(1)
                return sorted(m.name for m in await cls.using(kit.db).all())

        else:

            @cached_query(table=cls, key="ms_names")
            def names():
                runs.append(1)
                return sorted(m.name for m in cls.using(kit.db).all())

        await aw(cls(name="a").save(db=kit.db))
        first, again = await aw(names()), await aw(names())
        await aw(cls(name="b").save(db=kit.db))
        assert (first, again, await aw(names()), len(runs)) == (["a"], ["a"], ["a", "b"], 2)
    finally:
        configure_cache()


@pytest.mark.asyncio
async def test_timestamps_soft_delete_and_hooks_of_the_full_mixin(kit):
    cls = _with(kit.kind, (FullMixin,), (AsyncFullMixin,), "full", {"name": ""})
    item = await aw(cls(name="a").save(db=kit.db))
    assert (
        item._id,
        item.created_at == item.updated_at,
        item.created_at.tzinfo,
        item.is_deleted,
    ) == (1, True, timezone.utc, False)
    await aw(item.soft_delete())
    assert (
        item.is_deleted,
        await aw(cls.active(db=kit.db).count()),
        await aw(cls.only_deleted(db=kit.db).count()),
    ) == (True, 0, 1)
    await aw(item.restore())
    assert await aw(cls.active(db=kit.db).count()) == 1


@pytest.mark.asyncio
async def test_hooks_fire_in_order_and_a_veto_saves_nothing(kit):
    log: list = []
    if kit.kind == "async":

        class Hooked(AsyncHooksMixin, AsyncSQLerMsgspecModel):
            __tablename__ = "ms_mx_hooked"
            name: str = ""

            async def before_save(self) -> bool:
                log.append("before_save")
                return self.name != "veto"

            async def after_save(self) -> None:
                log.append("after_save")

            async def before_delete(self) -> bool:
                log.append("before_delete")
                return True

            async def after_delete(self) -> None:
                log.append("after_delete")

    else:

        class Hooked(HooksMixin, SQLerMsgspecModel):  # type: ignore[no-redef]
            __tablename__ = "ms_mx_hooked"
            name: str = ""

            def before_save(self) -> bool:
                log.append("before_save")
                return self.name != "veto"

            def after_save(self) -> None:
                log.append("after_save")

            def before_delete(self) -> bool:
                log.append("before_delete")
                return True

            def after_delete(self) -> None:
                log.append("after_delete")

    item = await aw(Hooked(name="a").save(db=kit.db))
    vetoed = Hooked(name="veto")
    with pytest.raises(
        RuntimeError, match="^before_save\\(\\) returned False, save aborted for Hooked$"
    ):
        await aw(vetoed.save(db=kit.db))
    await aw(item.delete())
    assert log == ["before_save", "after_save", "before_save", "before_delete", "after_delete"]
    assert (vetoed._id, await aw(Hooked.using(kit.db).count())) == (None, 0)


@pytest.mark.asyncio
async def test_audit_fields_and_the_audit_log(kit):
    mixin = AsyncAuditMixin if kit.kind == "async" else AuditMixin
    cls = _with(
        kit.kind,
        (AuditMixin, AuditLogMixin),
        (AsyncAuditMixin, AsyncAuditLogMixin),
        "audited",
        {"name": ""},
    )
    mixin.set_current_user("ann")
    try:
        item = await aw(cls(name="a").save(db=kit.db))
        mixin.set_current_user("bob")
        item.name = "b"
        await aw(item.save())
        logs = await aw(item.get_audit_log(db=kit.db))
    finally:
        mixin.set_current_user(None)
    assert (item.created_by, item.updated_by) == ("ann", "bob")
    assert [log["action"] for log in logs] == ["update", "create"]
    changes = logs[0]["changes"]
    assert sorted(changes) == ["name", "updated_at", "updated_by"]
    assert (changes["name"], changes["updated_by"]) == (
        {"old": "a", "new": "b"},
        {"old": "ann", "new": "bob"},
    )
    assert (logs[1]["changes"], logs[1]["snapshot"]["name"]) == (None, "a")


# ---- references cross the pair, as on the lite pair -------------------------------


def _pair(family: str):
    """``(sync child class, async holder class, async child class, sync holder class)`` of one
    family: each holder's ``child`` field is typed as the child of the other kind."""
    if family == "lite":

        @dataclass
        class XChild(SQLerLiteModel):
            __tablename__ = "x_children"
            name: str = ""

        @dataclass
        class XAChild(AsyncSQLerLiteModel):
            __tablename__ = "x_achildren"
            name: str = ""

        @dataclass
        class XAHolder(AsyncSQLerLiteModel):
            __tablename__ = "x_aholders"
            child: Optional[XChild] = None

        @dataclass
        class XHolder(SQLerLiteModel):
            __tablename__ = "x_holders"
            child: Optional[XAChild] = None

    else:

        class XChild(SQLerMsgspecModel):  # type: ignore[no-redef]
            __tablename__ = "x_children"
            name: str = ""

        class XAChild(AsyncSQLerMsgspecModel):  # type: ignore[no-redef]
            __tablename__ = "x_achildren"
            name: str = ""

        class XAHolder(AsyncSQLerMsgspecModel):  # type: ignore[no-redef]
            __tablename__ = "x_aholders"
            child: Optional[XChild] = None

        class XHolder(SQLerMsgspecModel):  # type: ignore[no-redef]
            __tablename__ = "x_holders"
            child: Optional[XAChild] = None

    return XChild, XAHolder, XAChild, XHolder


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["lite", "msgspec"])
async def test_an_async_model_stores_and_loads_a_reference_to_a_sync_model(family, tmp_path):
    child_cls, holder_cls, _, _ = _pair(family)
    sdb = SQLerDB.on_disk(str(tmp_path / "x.db"))
    adb = await open_db("async", tmp_path / "x.db")
    try:
        child = child_cls(name="kid").save(db=sdb)
        holder = await holder_cls(child=child).save(db=adb)
        stored = await adb.find_document("x_aholders", holder._id)
        got = await holder_cls.from_id(holder._id, db=adb)
    finally:
        await adb.close()
        sdb.close()
    assert stored == {"_id": 1, "child": {"_table": "x_children", "_id": 1}}
    assert (type(got.child), got.child._id, got.child.name) == (child_cls, 1, "kid")


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["lite", "msgspec"])
async def test_a_sync_model_refuses_a_reference_to_an_async_model(family, tmp_path):
    _, _, achild_cls, holder_cls = _pair(family)
    sdb = SQLerDB.on_disk(str(tmp_path / "x.db"))
    adb = await open_db("async", tmp_path / "x.db")
    try:
        child = await achild_cls(name="kid").save(db=adb)
        with pytest.raises(UnstorableValueError, match=r"is not JSON serializable \(at child\)$"):
            holder_cls(child=child).save(db=sdb)
        rows = sdb.find_documents("x_holders", [1])
    finally:
        await adb.close()
        sdb.close()
    assert rows == []
