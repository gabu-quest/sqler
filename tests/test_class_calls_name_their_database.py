"""Every class call of every model base and mixin can name its database, so a class that
``set_db()`` never bound can do everything a bound class does.

``WAYS`` is the one table: for each public class-level callable (found by introspection of
every base and mixin) how it is called on an unbound class, ``db=`` (a call that is not a
query), ``using(db)`` (a query) or ``none`` (it needs no database), with the binding calls
themselves (``set_db``, ``bind``, ``db``) as ``binding``. A callable missing from the table
fails ``test_every_class_callable_has_a_way``. Each row is exercised on an unbound class with
rows in a database (``test_each_way_works_on_an_unbound_class``), and on a class bound to
another database, which it leaves as it was (``test_each_way_leaves_the_bound_database``).
The async twins run the same table in
``tests/async/test_async_class_calls_name_their_database.py``.

The recipes are ``async`` so one table serves both: a sync call's result is used as it is,
an async call's is awaited (``_v``). The databases are files, read back with the standard
``sqlite3`` module, so what a call wrote is seen apart from sqler.
"""

import asyncio
import inspect
import json
import re
import sqlite3
import types
import warnings
from dataclasses import dataclass

import pytest

import sqler
from sqler import F, SQLerDB
from sqler.exceptions import NotBoundError
from sqler.export import (
    import_csv,
    import_csv_string,
    import_json,
    import_json_string,
    import_jsonl,
    import_jsonl_string,
)
from sqler.fts import SearchableMixin
from sqler.models._table_names import DATABASE_RULE
from sqler.models.lite.base import SQLerLiteModelBase
from sqler.models.msgspec.base import SQLerMsgspecModelBase

NAMES = ["apple", "banana", "cherry"]  # the rows every exercise starts with, ids 1, 2, 3
RULE = re.escape(DATABASE_RULE)

# ----- the subjects: every model base and mixin the package exports -----------------------

_MIXIN_MODULES = ("sqler.models.mixins", "sqler.tracking", "sqler.fts")


def is_model_base(cls: type) -> bool:
    """A model base of sqler: a class of the package that builds models from data."""
    return cls.__module__.startswith("sqler.") and hasattr(cls, "model_validate")


def _subjects() -> list[type]:
    """Every exported model base and mixin (a class of a mixin module whose name says it is
    one)."""
    found = []
    for name in sqler.__all__:
        obj = getattr(sqler, name)
        if not isinstance(obj, type):
            continue
        if is_model_base(obj) or (
            obj.__module__ in _MIXIN_MODULES and name.endswith(("Mixin", "TrackedModel"))
        ):
            found.append(obj)
    return found


def class_callables(cls: type) -> set[str]:
    """The public class-level callables sqler defines on ``cls`` or its bases (a classmethod or
    staticmethod of a class in a ``sqler`` module; pydantic's and msgspec's own are not)."""
    names = set()
    for name in dir(cls):
        if name.startswith("_"):
            continue
        for klass in cls.__mro__:
            if name in vars(klass):
                attr = vars(klass)[name]
                if isinstance(attr, (classmethod, staticmethod)) and klass.__module__.startswith("sqler"):
                    names.add(name)
                break
    return names


# ----- helpers the recipes use ------------------------------------------------------------


async def _v(value):
    """``value``, awaited when it is awaitable: a sync call's result or an async call's."""
    return await value if inspect.isawaitable(value) else value


def _names(rows) -> list[str]:
    return [row.name for row in rows]


async def _all_names(cls, db) -> list[str]:
    return _names(await _v(cls.using(db).order_by("_id").all()))


def _master(path) -> list[tuple[str, str]]:
    """What the file at ``path`` holds besides SQLite's own tables: ``(type, name)``."""
    with sqlite3.connect(path) as con:
        return sorted(
            con.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )


def _snapshot(path) -> list:
    """Everything in the file at ``path``: each object and the rows of each table."""
    with sqlite3.connect(path) as con:
        objects = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = [name for kind, name, _ in objects if kind == "table"]
        return [objects, {t: con.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}]


def _db_path(db) -> str:
    return db.adapter.path


async def _soft_deleted_damson(cls, db) -> None:
    """A fourth row, ``damson`` (id 4), saved and soft-deleted through the same instance."""
    damson = await _v(cls(name="damson").save(db=db))
    await _v(damson.soft_delete(db=db))


async def _search_ready(cls, db) -> None:
    cls.create_search_index(db=db)


# ----- the one table ------------------------------------------------------------------------


async def r_from_id(cls, db, table):
    return (await _v(cls.from_id(2, db=db))).name


async def r_from_ids(cls, db, table):
    return _names(await _v(cls.from_ids([1, 3], db=db)))


async def r_save_many(cls, db, table):
    await _v(cls.save_many([cls(name="damson")], db=db))
    return await _all_names(cls, db)


async def r_asave_many(cls, db, table):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        await _v(cls.asave_many([cls(name="damson")], db=db))
    return await _all_names(cls, db)


async def r_first_or_create(cls, db, table):
    found, created = await _v(cls.first_or_create({"name": "banana"}, db=db))
    return (found.name, found._id, created)


async def r_add_index(cls, db, table):
    await _v(cls.add_index("name", db=db))
    return [name for kind, name in _master(_db_path(db)) if kind == "index"]


async def r_ensure_index(cls, db, table):
    await _v(cls.ensure_index("name", db=db))
    return [name for kind, name in _master(_db_path(db)) if kind == "index"]


async def r_validate_references(cls, db, table):
    await _v(db.insert_document(table, {"name": "elder", "ref": {"_table": table, "_id": 99}}))
    broken = await _v(cls.validate_references(db=db))
    return [(b.table, b.row_id, b.path, b.target_table, b.target_id) for b in broken]


async def r_active(cls, db, table):
    await _soft_deleted_damson(cls, db)
    return _names(await _v(cls.active(db=db).order_by("_id").all()))


async def r_with_deleted(cls, db, table):
    await _soft_deleted_damson(cls, db)
    return _names(await _v(cls.with_deleted(db=db).order_by("_id").all()))


async def r_only_deleted(cls, db, table):
    await _soft_deleted_damson(cls, db)
    return _names(await _v(cls.only_deleted(db=db).all()))


async def r_create_search_index(cls, db, table):
    index = cls.create_search_index(db=db)
    return _names(index.search("banana"))


async def r_search(cls, db, table):
    await _search_ready(cls, db)
    return _names(cls.search("banana OR cherry", db=db))


async def r_search_ranked(cls, db, table):
    await _search_ready(cls, db)
    return [hit.model.name for hit in cls.search_ranked("cherry", db=db)]


async def r_search_count(cls, db, table):
    await _search_ready(cls, db)
    return cls.search_count("apple OR banana", db=db)


async def r_rebuild_search_index(cls, db, table):
    await _search_ready(cls, db)
    return cls.rebuild_search_index(db=db)


async def r_drop_search_index(cls, db, table):
    await _search_ready(cls, db)
    cls.drop_search_index(db=db)
    return [name for kind, name in _master(_db_path(db)) if name.endswith("_fts")]


async def r_query(cls, db, table):
    return _names(await _v(cls.using(db).order_by("_id").all()))


async def r_first(cls, db, table):
    return (await _v(cls.using(db).order_by("_id").first())).name


async def r_count(cls, db, table):
    return await _v(cls.using(db).count())


async def r_filter(cls, db, table):
    return _names(await _v(cls.using(db).filter(F("name") == "banana").all()))


async def r_ref(cls, db, table):
    built = cls.ref("home").field("city") == "Kyoto"
    return (re.search(r"FROM (\w+) r ", built.sql).group(1), built.params)


async def r_model_validate(cls, db, table):
    return cls.model_validate({"name": "kiwi", "_id": 7}).name


async def r_get_field_names(cls, db, table):
    return sorted(cls.get_field_names() - {"deleted_at", "created_at", "updated_at", "created_by", "updated_by"})


async def r_current_user(cls, db, table):
    cls.set_current_user("auditor")
    try:
        return cls.get_current_user()
    finally:
        cls.set_current_user(None)


async def r_current_user_getter(cls, db, table):
    cls.set_current_user_getter(lambda: "from getter")
    try:
        return cls.get_current_user()
    finally:
        cls.set_current_user_getter(None)


async def r_set_db(cls, db, table):
    with pytest.warns(DeprecationWarning, match=RULE):
        cls.set_db(db)
    return _names(await _v(cls.query().order_by("_id").all()))


async def r_bind(cls, db, table):
    with pytest.warns(DeprecationWarning, match=RULE):
        cls.bind(db)
    return _names(await _v(cls.query().order_by("_id").all()))


async def r_db(cls, db, table):
    with pytest.raises(NotBoundError, match=RULE):
        cls.db()
    return "NotBoundError"


ALL_ROWS = NAMES + ["damson"]

# name -> (way, recipe, expected); ``expected`` is a value, or a function of the table
WAYS = {
    "from_id": ("db=", r_from_id, "banana"),
    "from_ids": ("db=", r_from_ids, ["apple", "cherry"]),
    "save_many": ("db=", r_save_many, ALL_ROWS),
    "asave_many": ("db=", r_asave_many, ALL_ROWS),
    "first_or_create": ("db=", r_first_or_create, ("banana", 2, False)),
    "add_index": ("db=", r_add_index, lambda t: [f"idx_{t}_name"]),
    "ensure_index": ("db=", r_ensure_index, lambda t: [f"idx_{t}_name"]),
    "validate_references": ("db=", r_validate_references, lambda t: [(t, 4, "ref", t, 99)]),
    "active": ("db=", r_active, NAMES),
    "with_deleted": ("db=", r_with_deleted, ALL_ROWS),
    "only_deleted": ("db=", r_only_deleted, ["damson"]),
    "create_search_index": ("db=", r_create_search_index, ["banana"]),
    "search": ("db=", r_search, ["banana", "cherry"]),
    "search_ranked": ("db=", r_search_ranked, ["cherry"]),
    "search_count": ("db=", r_search_count, 2),
    "rebuild_search_index": ("db=", r_rebuild_search_index, 3),
    "drop_search_index": ("db=", r_drop_search_index, []),
    "query": ("using(db)", r_query, NAMES),
    "all": ("using(db)", r_query, NAMES),
    "using": ("using(db)", r_query, NAMES),
    "first": ("using(db)", r_first, "apple"),
    "count": ("using(db)", r_count, 3),
    "filter": ("using(db)", r_filter, ["banana"]),
    "ref": ("none", r_ref, ("homes", ["Kyoto"])),
    "model_validate": ("none", r_model_validate, "kiwi"),
    "get_field_names": ("none", r_get_field_names, ["name"]),
    "set_current_user": ("none", r_current_user, "auditor"),
    "get_current_user": ("none", r_current_user, "auditor"),
    "set_current_user_getter": ("none", r_current_user_getter, "from getter"),
    "set_db": ("binding", r_set_db, NAMES),
    "bind": ("binding", r_bind, NAMES),
    "db": ("binding", r_db, "NotBoundError"),
}


def expected_of(name: str, table: str):
    expected = WAYS[name][2]
    return expected(table) if callable(expected) else expected


# ----- the classes the rows run on ------------------------------------------------------


def is_async(cls: type) -> bool:
    return any(k.__name__.startswith("Async") for k in cls.__mro__ if k.__module__.startswith("sqler"))


def _bases() -> list[type]:
    """The model bases a class is made from (the shared parents are not: no class derives
    from them directly)."""
    return [
        c
        for c in _subjects()
        if is_model_base(c) and c not in (SQLerLiteModelBase, SQLerMsgspecModelBase)
    ]


def _mixins() -> list[type]:
    return [c for c in _subjects() if not is_model_base(c)]


def _fits(mixin: type, base: type) -> bool:
    """Whether ``mixin`` is made for ``base``: the same kind (sync or async; the timestamp and
    audit-free mixins serve both), the search mixin on the pydantic sync bases, the change
    tracking mixins on pydantic."""
    if mixin is SearchableMixin:
        return issubclass(base, sqler.SQLerModel)
    if mixin.__module__ == "sqler.tracking":
        if not issubclass(base, (sqler.SQLerModel, sqler.AsyncSQLerModel)):
            return False
    if mixin is sqler.TimestampMixin:
        return True
    return is_async(mixin) == is_async(base)


_counter = iter(range(1_000_000))


def make_class(base: type, mixin: type | None, *, own_table: bool):
    """A new model class on ``base`` (and ``mixin``) with one field, ``name``: with a
    ``__tablename__`` of its own, or the default table of its class name."""
    n = next(_counter)
    cls_name = f"Cc{n}{base.__name__}{mixin.__name__ if mixin else ''}"
    bases = (mixin, base) if mixin is not None else (base,)
    lite = issubclass(base, SQLerLiteModelBase)
    msgspec_ = issubclass(base, SQLerMsgspecModelBase)

    def body(ns):
        ns["__module__"] = __name__
        ns["__annotations__"] = {"name": str}
        if lite:
            ns["name"] = ""
        if own_table:
            ns["__tablename__"] = f"cc_rows_{n}"

    cls = types.new_class(cls_name, bases, {"tag": False} if msgspec_ else {}, body)
    if lite:
        cls = dataclass(cls)
    table = f"cc_rows_{n}" if own_table else sqler.models._table_names._default_table_name(cls_name)
    return cls, table


def subject_cases(want_async: bool) -> list:
    """``(base, mixin, name)`` for every row of the table each class has: the base's own
    callables on the plain class, a mixin's on the mixin over each base it fits."""
    cases = []
    for base in _bases():
        if is_async(base) != want_async:
            continue
        for name in sorted(class_callables(base)):
            cases.append(pytest.param(base, None, name, id=f"{base.__name__}.{name}"))
        for mixin in _mixins():
            if not _fits(mixin, base):
                continue
            for name in sorted(class_callables(mixin)):
                cases.append(pytest.param(base, mixin, name, id=f"{mixin.__name__}+{base.__name__}.{name}"))
    return cases


async def seed(cls, db) -> None:
    for name in NAMES:
        await _v(cls(name=name).save(db=db))


# ----- the table holds every callable ------------------------------------------------------


def test_every_class_callable_has_a_way():
    found = {name for cls in _subjects() for name in class_callables(cls)}
    assert sorted(found - set(WAYS)) == []
    assert sorted(set(WAYS) - found) == []


def test_the_subjects_are_every_base_and_mixin():
    assert sorted(c.__name__ for c in _subjects()) == sorted(
        [
            "SQLerModel", "SQLerSafeModel", "AsyncSQLerModel", "AsyncSQLerSafeModel",
            "SQLerLiteModel", "SQLerLiteModelBase", "SQLerLiteSafeModel",
            "AsyncSQLerLiteModel", "AsyncSQLerLiteSafeModel",
            "SQLerMsgspecModel", "SQLerMsgspecModelBase",
            "TimestampMixin", "SoftDeleteMixin", "AsyncSoftDeleteMixin", "HooksMixin",
            "AsyncHooksMixin", "FullMixin", "AsyncFullMixin", "AuditMixin", "AsyncAuditMixin",
            "AuditLogMixin", "AsyncAuditLogMixin", "SearchableMixin",
            "TrackedModel", "AsyncTrackedModel", "PartialUpdateMixin", "AsyncPartialUpdateMixin",
            "DiffMixin",
        ]
    )


def test_every_way_is_one_of_four():
    assert sorted({way for way, _, _ in WAYS.values()}) == ["binding", "db=", "none", "using(db)"]


# ``create_search_index(db=None)`` takes its database positionally as well: it did before
# the other calls took ``db=``, and a caller's ``create_search_index(db)`` keeps working.
POSITIONAL_DB = {"create_search_index"}


@pytest.mark.parametrize("name", sorted(n for n, (way, _, _) in WAYS.items() if way == "db="))
def test_each_db_call_takes_db_keyword_only_defaulting_to_none(name):
    """``db`` is keyword-only and defaults to ``None`` wherever a class has the call (a mixin
    that passes its keywords on, ``**kwargs``, takes it as the call it wraps does)."""
    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD if name in POSITIONAL_DB else inspect.Parameter.KEYWORD_ONLY
    owners = [cls for cls in _subjects() if name in class_callables(cls)]
    assert owners != []
    for cls in owners:
        params = inspect.signature(getattr(cls, name)).parameters
        if "db" not in params:
            assert [p.kind for p in params.values()][-1] == inspect.Parameter.VAR_KEYWORD, cls
            continue
        assert (params["db"].kind, params["db"].default) == (kind, None), cls


# ----- each row on an unbound class, and on a class bound to another database -------------


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def two_dbs(tmp_path):
    made = [SQLerDB.on_disk(str(tmp_path / "given.db")), SQLerDB.on_disk(str(tmp_path / "bound.db"))]
    yield made
    for db in made:
        db.close()


@pytest.mark.parametrize("own_table", [True, False], ids=["tablename", "default_table"])
@pytest.mark.parametrize(("base", "mixin", "name"), subject_cases(want_async=False))
def test_each_way_works_on_an_unbound_class(base, mixin, name, own_table, two_dbs):
    db, _ = two_dbs
    cls, table = make_class(base, mixin, own_table=own_table)
    _run(seed(cls, db))
    got = _run(WAYS[name][1](cls, db, table))
    assert got == expected_of(name, table)


@pytest.mark.parametrize(
    ("base", "mixin", "name"),
    [c for c in subject_cases(want_async=False) if WAYS[c.values[2]][0] in ("db=", "using(db)")],
)
def test_each_way_leaves_the_bound_database(base, mixin, name, two_dbs):
    """A class bound to one database, called with another, changes only the given one."""
    db, bound = two_dbs
    cls, table = make_class(base, mixin, own_table=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(bound)
    cls(name="zucchini").save()
    before = _snapshot(_db_path(bound))
    _run(seed(cls, db))
    got = _run(WAYS[name][1](cls, db, table))
    assert got == expected_of(name, table)
    assert _snapshot(_db_path(bound)) == before


# ----- hostile inputs ----------------------------------------------------------------------


UNBOUND_CALLS = {
    "add_index": lambda cls: cls.add_index("name"),
    "ensure_index": lambda cls: cls.ensure_index("name"),
    "validate_references": lambda cls: cls.validate_references(),
    "active": lambda cls: cls.active(),
    "with_deleted": lambda cls: cls.with_deleted(),
    "only_deleted": lambda cls: cls.only_deleted(),
    "from_id": lambda cls: cls.from_id(1),
    "count": lambda cls: cls.count(),
}


@pytest.mark.parametrize("name", sorted(UNBOUND_CALLS))
def test_an_unbound_class_without_db_raises_the_unbound_error_naming_the_rule(name):
    mixin = sqler.SoftDeleteMixin if name in ("active", "with_deleted", "only_deleted") else None
    cls, _ = make_class(sqler.SQLerModel, mixin, own_table=True)
    with pytest.raises(NotBoundError, match=f"Model {cls.__name__} is not bound. {RULE}"):
        UNBOUND_CALLS[name](cls)


def test_the_rule_names_using_for_queries_and_db_for_every_other_call():
    assert DATABASE_RULE == (
        "Use .using(db) for queries and db= for every other call "
        "(.save(db=db), .delete(db=db), Model.from_id(id, db=db), Model.add_index(field, db=db))."
    )


@pytest.mark.parametrize(
    "base", [b for b in _bases() if not is_async(b)], ids=lambda b: b.__name__
)
def test_the_set_db_warning_of_every_sync_base_says_the_rule(base, two_dbs):
    cls, _ = make_class(base, None, own_table=True)
    with pytest.warns(DeprecationWarning) as caught:
        cls.set_db(two_dbs[0])
    assert [str(w.message) for w in caught] == [
        f"{cls.__name__}.set_db() is deprecated. {DATABASE_RULE} set_db() will be removed in a future version."
    ]


@pytest.mark.parametrize("way", ["search", "search_ranked", "search_count", "rebuild_search_index"])
def test_a_search_on_a_database_without_the_index_raises_as_a_missing_index_does(way, two_dbs):
    """The class's index was made on A: a call on B uses no index of A and finds none on B."""
    a, b = two_dbs
    cls, table = make_class(sqler.SQLerModel, SearchableMixin, own_table=True)
    _run(seed(cls, a))
    _run(seed(cls, b))
    cls.create_search_index(db=a)
    calls = {
        "search": lambda: cls.search("banana", db=b),
        "search_ranked": lambda: cls.search_ranked("banana", db=b),
        "search_count": lambda: cls.search_count("banana", db=b),
        "rebuild_search_index": lambda: cls.rebuild_search_index(db=b),
    }
    with pytest.raises(sqlite3.OperationalError, match=f"no such table: (main\\.)?{table}_fts"):
        calls[way]()
    assert [n for _, n in _master(_db_path(b)) if n.endswith("_fts")] == []
    assert _names(cls.search("banana", db=a)) == ["banana"]


def test_dropping_the_index_of_another_database_keeps_the_class_index(two_dbs):
    a, b = two_dbs
    cls, table = make_class(sqler.SQLerModel, SearchableMixin, own_table=True)
    _run(seed(cls, a))
    _run(seed(cls, b))
    held = cls.create_search_index(db=a)
    cls.create_search_index(db=b)
    cls._fts_index = held
    cls.drop_search_index(db=b)
    assert cls._fts_index is held
    assert [n for _, n in _master(_db_path(a)) if n == f"{table}_fts"] == [f"{table}_fts"]
    assert [n for _, n in _master(_db_path(b)) if n.endswith("_fts")] == []
    cls.drop_search_index(db=a)
    assert cls._fts_index is None


SEARCHES = {
    "search": lambda cls, db: [(r.name, r._id) for r in cls.search("banana OR elder", db=db)],
    "search_ranked": lambda cls, db: [
        (hit.model.name, hit.model._id) for hit in cls.search_ranked("banana OR elder", db=db)
    ],
    "search_count": lambda cls, db: cls.search_count("banana OR elder", db=db),
}
ONLY_BANANA_OF_A = {"search": [("banana", 2)], "search_ranked": [("banana", 2)], "search_count": 1}


@pytest.mark.parametrize("way", sorted(SEARCHES))
def test_a_search_given_a_database_reads_it_when_the_class_index_is_of_another(way, two_dbs):
    """Both databases have the index and the class holds B's. A has apple, banana (id 2) and
    cherry; B has banana (id 1) and elder (id 2): a search of A finds A's banana alone."""
    a, b = two_dbs
    cls, _ = make_class(sqler.SQLerModel, SearchableMixin, own_table=True)
    _run(seed(cls, a))
    for name in ["banana", "elder"]:
        cls(name=name).save(db=b)
    cls.create_search_index(db=a)
    held = cls.create_search_index(db=b)
    assert SEARCHES[way](cls, a) == ONLY_BANANA_OF_A[way]
    assert cls._fts_index is held


@pytest.mark.parametrize("way", sorted(SEARCHES))
def test_a_class_that_made_no_index_searches_the_one_a_database_has(way, two_dbs):
    """A second class of the same table never made an index: given a database that has the
    table's index it searches it, and still holds none."""
    a, _ = two_dbs
    maker, table = make_class(sqler.SQLerModel, SearchableMixin, own_table=True)
    _run(seed(maker, a))
    maker.create_search_index(db=a)

    def body(ns):
        ns["__module__"] = __name__
        ns["__annotations__"] = {"name": str}
        ns["__tablename__"] = table

    reader = types.new_class(f"{maker.__name__}Reader", (SearchableMixin, sqler.SQLerModel), {}, body)
    assert SEARCHES[way](reader, a) == ONLY_BANANA_OF_A[way]
    assert reader._fts_index is None


def test_a_search_given_an_async_database_raises_as_an_index_on_one_does(two_dbs):
    cls, _ = make_class(sqler.SQLerModel, SearchableMixin, own_table=True)
    adb = sqler.AsyncSQLerDB.in_memory(shared=False)
    with pytest.raises(TypeError, match="a search index works on a SQLerDB"):
        cls.search("banana", db=adb)


# ----- a database of the other kind ---------------------------------------------------------


def wrong_kind_message(cls: type, takes: str, given: str) -> str:
    """The ``TypeError`` of a model given a database it cannot use, word for word."""
    article = {"SQLerDB": "a", "AsyncSQLerDB": "an"}[takes]
    return (
        f"{cls.__name__} takes {article} {takes} as its database, not an object of type {given}: "
        "a sync model reads and writes through a SQLerDB, an async model through an AsyncSQLerDB."
    )


# How each call is made with the database ``db``. ``kept`` is a saved instance (``apple``, id 1).
# Each call is awaited when it returns an awaitable, so one table serves both kinds.
WRONG_KIND_CALLS = {
    "save": lambda cls, kept, db: cls(name="damson").save(db=db),
    "save_saved": lambda cls, kept, db: kept.save(db=db),
    "delete": lambda cls, kept, db: kept.delete(db=db),
    "refresh": lambda cls, kept, db: kept.refresh(db=db),
    "from_id": lambda cls, kept, db: cls.from_id(1, db=db),
    "from_ids": lambda cls, kept, db: cls.from_ids([1], db=db),
    "from_ids_empty": lambda cls, kept, db: cls.from_ids([], db=db),
    "save_many": lambda cls, kept, db: cls.save_many([cls(name="damson")], db=db),
    "save_many_empty": lambda cls, kept, db: cls.save_many([], db=db),
    "first_or_create": lambda cls, kept, db: cls.first_or_create({"name": "damson"}, db=db),
    "add_index": lambda cls, kept, db: cls.add_index("name", db=db),
    "ensure_index": lambda cls, kept, db: cls.ensure_index("name", db=db),
    "validate_references": lambda cls, kept, db: cls.validate_references(db=db),
    "using": lambda cls, kept, db: cls.using(db),
    "set_db": lambda cls, kept, db: cls.set_db(db),
    "bind": lambda cls, kept, db: cls.bind(db),
}


def _method_of(name: str) -> str:
    return {"save_saved": "save", "from_ids_empty": "from_ids", "save_many_empty": "save_many"}.get(
        name, name
    )


def wrong_kind_cases(want_async: bool) -> list:
    """Every call of ``WRONG_KIND_CALLS`` a base of the kind has, on every base of the kind."""
    return [
        pytest.param(base, name, id=f"{base.__name__}-{name}")
        for base in _bases()
        if is_async(base) == want_async
        for name in sorted(WRONG_KIND_CALLS)
        if hasattr(base, _method_of(name))
    ]


def test_every_sync_base_is_given_every_call_of_the_other_kind():
    """Five sync bases; ``validate_references`` is on the two pydantic ones, ``bind`` on the
    lite and msgspec ones: 5 * 14 + 2 + 3 cases."""
    assert len(wrong_kind_cases(want_async=False)) == 75


def _binding(cls) -> tuple:
    return (cls._db, cls._table, cls.__dict__.get("__tablename__"))


def _identity(inst) -> tuple:
    home = vars(inst).get("_home")
    return (inst._id, inst.name, getattr(inst, "_version", None), home.db if home else None)


@pytest.mark.parametrize(
    ("given", "bound"), [("async_db", False), ("str", True)], ids=["async_db-unbound", "str-bound"]
)
@pytest.mark.parametrize(("base", "name"), wrong_kind_cases(want_async=False))
def test_a_sync_model_given_a_database_it_cannot_use_raises_before_anything_runs(
    base, name, given, bound, two_dbs
):
    """The ``TypeError`` names the class, the type given and the database the class takes;
    the sync database, the saved instance and the class binding are as they were."""
    right, _ = two_dbs
    cls, _ = make_class(base, None, own_table=True)
    if bound:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(right)
    kept = cls(name="apple").save(db=right)
    wrong = sqler.AsyncSQLerDB.in_memory(shared=False) if given == "async_db" else "x"
    before = (_snapshot(_db_path(right)), _identity(kept), _binding(cls))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(TypeError) as caught:
            _run(_v(WRONG_KIND_CALLS[name](cls, kept, wrong)))
    given_type = "AsyncSQLerDB" if given == "async_db" else "str"
    assert str(caught.value) == wrong_kind_message(cls, "SQLerDB", given_type)
    assert (_snapshot(_db_path(right)), _identity(kept), _binding(cls)) == before


def test_a_sync_model_saved_with_an_async_database_keeps_no_id_and_writes_nothing(two_dbs):
    right, _ = two_dbs
    cls, table = make_class(sqler.SQLerModel, None, own_table=True)
    right.ensure_table(table)
    note = cls(name="x")
    with pytest.raises(TypeError) as caught:
        note.save(db=sqler.AsyncSQLerDB.in_memory(shared=False))
    assert str(caught.value) == wrong_kind_message(cls, "SQLerDB", "AsyncSQLerDB")
    assert note._id is None
    assert vars(note).get("_home") is None
    with sqlite3.connect(_db_path(right)) as raw:
        assert raw.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


class _OwnDB(SQLerDB):
    """A database class of the program's own, on ``SQLerDB``."""


@pytest.mark.parametrize(
    "base", [b for b in _bases() if not is_async(b)], ids=lambda b: b.__name__
)
def test_a_subclass_of_the_sync_database_is_taken_as_the_database_is(base, tmp_path):
    db = _OwnDB.on_disk(str(tmp_path / "own.db"))
    try:
        cls, _ = make_class(base, None, own_table=True)
        _run(seed(cls, db))
        assert _names(cls.using(db).order_by("_id").all()) == NAMES
        assert cls.from_id(2, db=db).name == "banana"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(db)
        assert cls._db is db
        assert cls.count() == 3
    finally:
        db.close()


# ----- the imports ------------------------------------------------------------------------


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


IMPORTS = {
    "import_csv": lambda cls, db, tmp, **kw: import_csv(cls, _write(tmp, "r.csv", "_id,name\n,damson\n"), db=db, **kw),
    "import_csv_batched": lambda cls, db, tmp, **kw: import_csv(
        cls, _write(tmp, "r.csv", "_id,name\n,damson\n"), batch_size=10, db=db, **kw
    ),
    "import_json": lambda cls, db, tmp, **kw: import_json(cls, _write(tmp, "r.json", '[{"name": "damson"}]'), db=db, **kw),
    "import_jsonl": lambda cls, db, tmp, **kw: import_jsonl(cls, _write(tmp, "r.jsonl", '{"name": "damson"}\n'), db=db, **kw),
    "import_csv_string": lambda cls, db, tmp, **kw: import_csv_string(cls, "_id,name\n,damson\n", db=db, **kw),
    "import_json_string": lambda cls, db, tmp, **kw: import_json_string(cls, '[{"name": "damson"}]', db=db, **kw),
    "import_jsonl_string": lambda cls, db, tmp, **kw: import_jsonl_string(cls, '{"name": "damson"}', db=db, **kw),
}


def _import_cases() -> list:
    """Every import on every sync base, but a CSV import on a lite or msgspec model: a CSV
    import reads each field's ``annotation``, which only a pydantic field has, so it fails
    on those bases with or without ``db=``."""
    return [
        pytest.param(way, base, id=f"{way}-{base.__name__}")
        for way in sorted(IMPORTS)
        for base in _bases()
        if not is_async(base)
        and ("csv" not in way or issubclass(base, sqler.SQLerModel))
    ]


@pytest.mark.parametrize(("way", "base"), _import_cases())
def test_an_import_given_db_writes_there_and_not_to_the_bound_database(way, base, two_dbs, tmp_path):
    db, bound = two_dbs
    cls, _ = make_class(base, None, own_table=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(bound)
    cls(name="zucchini").save()
    before = _snapshot(_db_path(bound))
    _run(seed(cls, db))
    result = IMPORTS[way](cls, db, tmp_path)
    assert (result.count, result.succeeded, result.failed, result.errors) == (1, 1, 0, [])
    assert _run(_all_names(cls, db)) == ALL_ROWS
    assert _snapshot(_db_path(bound)) == before


@pytest.mark.parametrize("way", sorted(IMPORTS))
def test_an_import_on_an_unbound_class_writes_to_the_given_database(way, two_dbs, tmp_path):
    db, _ = two_dbs
    cls, _ = make_class(sqler.SQLerModel, None, own_table=False)
    _run(seed(cls, db))
    result = IMPORTS[way](cls, db, tmp_path)
    assert (result.count, result.succeeded, result.failed, result.errors) == (1, 1, 0, [])
    assert _run(_all_names(cls, db)) == ALL_ROWS


def test_an_import_on_an_unbound_class_without_db_records_the_unbound_error(tmp_path):
    cls, _ = make_class(sqler.SQLerModel, None, own_table=True)
    result = import_json_string(cls, '[{"name": "damson"}]')
    assert (result.succeeded, result.failed) == (0, 1)
    assert result.errors[0]["error_type"] == "NotBoundError"
    assert re.search(RULE, result.errors[0]["error"])


def test_a_batched_csv_import_on_an_unbound_class_without_db_raises_the_unbound_error(tmp_path):
    cls, _ = make_class(sqler.SQLerModel, None, own_table=True)
    with pytest.raises(NotBoundError, match=RULE):
        import_csv(cls, _write(tmp_path, "r.csv", "_id,name\n,damson\n"), batch_size=10)


class CcCity(sqler.SQLerModel):
    __tablename__ = "cc_cities"
    name: str


class CcResident(sqler.SQLerModel):
    __tablename__ = "cc_residents"
    name: str
    city: CcCity | None = None


UPDATES = {
    "import_json_string": lambda db: import_json_string(
        CcResident, '[{"_id": 1, "name": "renamed"}]', update_existing=True, db=db
    ),
    "import_jsonl_string": lambda db: import_jsonl_string(
        CcResident, '{"_id": 1, "name": "renamed"}', update_existing=True, db=db
    ),
    "import_csv_string": lambda db: import_csv_string(
        CcResident, "_id,name\n1,renamed\n", update_existing=True, db=db
    ),
}


@pytest.mark.parametrize("way", sorted(UPDATES))
def test_an_update_import_looks_for_the_referenced_rows_in_the_given_database(way, two_dbs):
    """The resident class is bound to A, the city class to none. B's resident 1 refers to
    B's city 1, which A does not have: the import reads the row, and the row it refers to,
    from B, so the reference is written back as it was. (A referenced class bound with
    ``set_db()`` has its rows read from its own database: the rule of every read.)"""
    b, a = two_dbs
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        CcResident.set_db(a)
    try:
        CcResident(name="in a").save()
        before = _snapshot(_db_path(a))
        kyoto = CcCity(name="Kyoto").save(db=b)
        CcResident(name="in b", city=kyoto).save(db=b)
        result = UPDATES[way](b)
        assert (result.count, result.succeeded, result.failed, result.errors) == (1, 1, 0, [])
        with sqlite3.connect(_db_path(b)) as con:
            rows = con.execute("SELECT _id, data FROM cc_residents").fetchall()
        assert [(i, json.loads(d)) for i, d in rows] == [
            (1, {"name": "renamed", "city": {"_table": "cc_cities", "_id": 1}})
        ]
        assert _snapshot(_db_path(a)) == before
    finally:
        CcResident._db = None
        CcResident._table = None
