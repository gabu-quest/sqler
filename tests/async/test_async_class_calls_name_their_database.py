"""The async twins of ``tests/test_class_calls_name_their_database.py``: the same table
(``WAYS``), run on every async base and async mixin, plus the async import and the mistakes
of a database of the other kind."""

import json
import re
import sqlite3
import warnings

import pytest
import pytest_asyncio
from test_class_calls_name_their_database import (  # tests/ is on the import path
    ALL_ROWS,
    WAYS,
    WRONG_KIND_CALLS,
    _all_names,
    _bases,
    _binding,
    _db_path,
    _identity,
    _names,
    _snapshot,
    _v,
    expected_of,
    is_async,
    make_class,
    seed,
    subject_cases,
    wrong_kind_cases,
    wrong_kind_message,
)

import sqler
from sqler import AsyncSQLerDB, SQLerDB
from sqler.exceptions import NotBoundError
from sqler.export import async_import_jsonl
from sqler.models._table_names import DATABASE_RULE

RULE = re.escape(DATABASE_RULE)


@pytest_asyncio.fixture
async def two_dbs(tmp_path):
    made = [
        AsyncSQLerDB.on_disk(str(tmp_path / "given.db")),
        AsyncSQLerDB.on_disk(str(tmp_path / "bound.db")),
    ]
    for db in made:
        await db.connect()
    yield made
    for db in made:
        await db.close()


def test_the_async_bases_are_exercised():
    """Every async base has its rows of the table run here."""
    names = sorted({c.values[0].__name__ for c in subject_cases(want_async=True)})
    assert names == [
        "AsyncSQLerLiteModel",
        "AsyncSQLerLiteSafeModel",
        "AsyncSQLerModel",
        "AsyncSQLerMsgspecModel",
        "AsyncSQLerSafeModel",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("own_table", [True, False], ids=["tablename", "default_table"])
@pytest.mark.parametrize(("base", "mixin", "name"), subject_cases(want_async=True))
async def test_each_way_works_on_an_unbound_class(base, mixin, name, own_table, two_dbs):
    db, _ = two_dbs
    cls, table = make_class(base, mixin, own_table=own_table)
    await seed(cls, db)
    assert await WAYS[name][1](cls, db, table) == expected_of(name, table)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base", "mixin", "name"),
    [c for c in subject_cases(want_async=True) if WAYS[c.values[2]][0] in ("db=", "using(db)")],
)
async def test_each_way_leaves_the_bound_database(base, mixin, name, two_dbs):
    """A class bound to one database, called with another, changes only the given one."""
    db, bound = two_dbs
    cls, table = make_class(base, mixin, own_table=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(bound)
    await cls(name="zucchini").save()
    before = _snapshot(_db_path(bound))
    await seed(cls, db)
    assert await WAYS[name][1](cls, db, table) == expected_of(name, table)
    assert _snapshot(_db_path(bound)) == before


@pytest.mark.parametrize("base", [b for b in _bases() if is_async(b)], ids=lambda b: b.__name__)
def test_the_set_db_warning_of_every_async_base_says_the_rule(base, tmp_path):
    cls, _ = make_class(base, None, own_table=True)
    with pytest.warns(DeprecationWarning) as caught:
        cls.set_db(AsyncSQLerDB.on_disk(str(tmp_path / "x.db")))
    assert [str(w.message) for w in caught] == [
        f"{cls.__name__}.set_db() is deprecated. {DATABASE_RULE} set_db() will be removed in a future version."
    ]


UNBOUND_CALLS = {
    "add_index": lambda cls: cls.add_index("name"),
    "ensure_index": lambda cls: cls.ensure_index("name"),
    "validate_references": lambda cls: cls.validate_references(),
    "from_id": lambda cls: cls.from_id(1),
    "count": lambda cls: cls.count(),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(UNBOUND_CALLS))
async def test_an_unbound_class_without_db_raises_the_unbound_error_naming_the_rule(name):
    cls, _ = make_class(sqler.AsyncSQLerModel, None, own_table=True)
    with pytest.raises(NotBoundError, match=f"Model {cls.__name__} is not bound. {RULE}"):
        await UNBOUND_CALLS[name](cls)


@pytest.mark.parametrize("name", ["active", "with_deleted", "only_deleted"])
def test_an_unbound_soft_delete_queryset_without_db_raises_the_unbound_error(name):
    cls, _ = make_class(sqler.AsyncSQLerModel, sqler.AsyncSoftDeleteMixin, own_table=True)
    with pytest.raises(NotBoundError, match=f"Model {cls.__name__} is not bound. {RULE}"):
        getattr(cls, name)()


def test_every_async_base_is_given_every_call_of_the_other_kind():
    """Five async bases; ``validate_references`` is on the two pydantic ones, ``bind`` on the
    two lite ones and the msgspec one: 5 * 14 + 2 + 3 cases."""
    assert len(wrong_kind_cases(want_async=True)) == 75


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "bound"), [("sync_db", False), ("str", True)], ids=["sync_db-unbound", "str-bound"]
)
@pytest.mark.parametrize(("base", "name"), wrong_kind_cases(want_async=True))
async def test_an_async_model_given_a_database_it_cannot_use_raises_before_anything_runs(
    base, name, given, bound, two_dbs
):
    """The ``TypeError`` names the class, the type given and the database the class takes;
    the async database, the saved instance and the class binding are as they were."""
    right, _ = two_dbs
    cls, _ = make_class(base, None, own_table=True)
    if bound:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(right)
    kept = await cls(name="apple").save(db=right)
    wrong = SQLerDB.in_memory(shared=False) if given == "sync_db" else "x"
    before = (_snapshot(_db_path(right)), _identity(kept), _binding(cls))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with pytest.raises(TypeError) as caught:
                await _v(WRONG_KIND_CALLS[name](cls, kept, wrong))
    finally:
        if given == "sync_db":
            wrong.close()
    given_type = "SQLerDB" if given == "sync_db" else "str"
    assert str(caught.value) == wrong_kind_message(cls, "AsyncSQLerDB", given_type)
    assert (_snapshot(_db_path(right)), _identity(kept), _binding(cls)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["active", "with_deleted", "only_deleted"])
async def test_an_async_soft_delete_queryset_given_a_sync_database_raises_as_using_does(
    name, tmp_path
):
    """A soft-delete queryset is a query: given a sync database it raises as ``using(db)``
    does, when it is asked for, before it reads anything."""
    sync_db = SQLerDB.on_disk(str(tmp_path / "sync.db"))
    try:
        cls, _ = make_class(sqler.AsyncSQLerModel, sqler.AsyncSoftDeleteMixin, own_table=True)
        with pytest.raises(TypeError) as caught:
            getattr(cls, name)(db=sync_db)
        assert str(caught.value) == wrong_kind_message(cls, "AsyncSQLerDB", "SQLerDB")
    finally:
        sync_db.close()


@pytest.mark.asyncio
async def test_an_async_model_saved_with_a_sync_database_writes_no_row(tmp_path):
    sync_db = SQLerDB.on_disk(str(tmp_path / "sync.db"))
    try:
        cls, table = make_class(sqler.AsyncSQLerModel, None, own_table=True)
        sync_db.ensure_table(table)
        note = cls(name="x")
        with pytest.raises(TypeError) as caught:
            await note.save(db=sync_db)
        assert str(caught.value) == wrong_kind_message(cls, "AsyncSQLerDB", "SQLerDB")
        assert note._id is None
        with sqlite3.connect(_db_path(sync_db)) as raw:
            assert raw.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    finally:
        sync_db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [b for b in _bases() if is_async(b)], ids=lambda b: b.__name__)
async def test_set_db_none_leaves_an_async_class_unbound(base, two_dbs):
    """``set_db(None)`` is not a database of the other kind: it unbinds the class."""
    right, _ = two_dbs
    cls, _ = make_class(base, None, own_table=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(right)
        cls.set_db(None)
    assert cls._db is None
    with pytest.raises(NotBoundError, match=f"Model {cls.__name__} is not bound. {RULE}"):
        await cls.from_id(1)


class _OwnAsyncDB(AsyncSQLerDB):
    """A database class of the program's own, on ``AsyncSQLerDB``."""


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [b for b in _bases() if is_async(b)], ids=lambda b: b.__name__)
async def test_a_subclass_of_the_async_database_is_taken_as_the_database_is(base, tmp_path):
    db = _OwnAsyncDB.on_disk(str(tmp_path / "own.db"))
    await db.connect()
    try:
        cls, _ = make_class(base, None, own_table=True)
        await seed(cls, db)
        assert _names(await cls.using(db).order_by("_id").all()) == ["apple", "banana", "cherry"]
        assert (await cls.from_id(2, db=db)).name == "banana"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(db)
        assert cls._db is db
        assert await cls.count() == 3
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [b for b in _bases() if is_async(b)], ids=lambda b: b.__name__)
async def test_the_async_import_given_db_writes_there_and_not_to_the_bound_database(
    base, two_dbs, tmp_path
):
    db, bound = two_dbs
    cls, _ = make_class(base, None, own_table=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(bound)
    await cls(name="zucchini").save()
    before = _snapshot(_db_path(bound))
    await seed(cls, db)
    path = tmp_path / "r.jsonl"
    path.write_text('{"name": "damson"}\n', encoding="utf-8")
    result = await async_import_jsonl(cls, path, db=db)
    assert (result.count, result.succeeded, result.failed, result.errors) == (1, 1, 0, [])
    assert await _all_names(cls, db) == ALL_ROWS
    assert _snapshot(_db_path(bound)) == before


class AccCity(sqler.AsyncSQLerModel):
    __tablename__ = "acc_cities"
    name: str


class AccResident(sqler.AsyncSQLerModel):
    __tablename__ = "acc_residents"
    name: str
    city: AccCity | None = None


@pytest.mark.asyncio
async def test_the_async_update_import_looks_for_the_referenced_rows_in_the_given_database(
    two_dbs, tmp_path
):
    """As the sync import does: the resident class bound to A, the city class to none; B's
    resident and the city it refers to are read from B."""
    b, a = two_dbs
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        AccResident.set_db(a)
    try:
        await AccResident(name="in a").save()
        before = _snapshot(_db_path(a))
        kyoto = await AccCity(name="Kyoto").save(db=b)
        await AccResident(name="in b", city=kyoto).save(db=b)
        path = tmp_path / "r.jsonl"
        path.write_text('{"_id": 1, "name": "renamed"}\n', encoding="utf-8")
        result = await async_import_jsonl(AccResident, path, update_existing=True, db=b)
        assert (result.count, result.succeeded, result.failed, result.errors) == (1, 1, 0, [])
        with sqlite3.connect(_db_path(b)) as con:
            rows = con.execute("SELECT _id, data FROM acc_residents").fetchall()
        assert [(i, json.loads(d)) for i, d in rows] == [
            (1, {"name": "renamed", "city": {"_table": "acc_cities", "_id": 1}})
        ]
        assert _snapshot(_db_path(a)) == before
    finally:
        AccResident._db = None
        AccResident._table = None
