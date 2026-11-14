"""A stored reference is followed only at a typed position, on every way in, sync and async.

A typed position is a field whose annotation names a model class (``Optional[User]``,
``list[User]``, ``dict[str, User]``), and in it only a reference into a table of a class the
field admits is followed. A ``{"_table", "_id"}`` dict anywhere else (a ``dict``, ``list`` or
``Any`` field, at any depth, inside a followed row too) is content: it is given back as the
dict it is, and no statement is sent for it.

The rows are written with ``insert_document``, as a visitor's input would be stored; the
statements are counted with ``query_logger.capture()``.
"""

import asyncio
import dataclasses
import inspect
import re
import warnings
from typing import Any, Optional

import pytest
from pydantic import Field as PydField
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
    F,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerSafeModel,
)
from sqler.logging import query_logger

# ---- one set of classes per base -----------------------------------------------------

BASES = {
    "pydantic": (SQLerModel, False, False),
    "safe": (SQLerSafeModel, False, False),
    "lite": (SQLerLiteModel, True, False),
    "lite-safe": (SQLerLiteSafeModel, True, False),
    "async-pydantic": (AsyncSQLerModel, False, True),
    "async-safe": (AsyncSQLerSafeModel, False, True),
    "async-lite": (AsyncSQLerLiteModel, True, True),
    "async-lite-safe": (AsyncSQLerLiteSafeModel, True, True),
}


def _make(key: str, base: type, lite: bool, name: str, fields: dict) -> type:
    """A model class of ``base`` with ``fields`` (``{name: (annotation, default)}``); a
    default that is a type (``dict``, ``list``) is a factory."""
    ns: dict = {"__tablename__": f"rp_{key.replace('-', '_')}_{name.lower()}", "__annotations__": {}}
    for field_name, (annotation, default) in fields.items():
        ns["__annotations__"][field_name] = annotation
        if isinstance(default, type):
            ns[field_name] = (dataclasses.field if lite else PydField)(default_factory=default)
        else:
            ns[field_name] = default
    cls = type(f"Rp{''.join(p.title() for p in key.split('-'))}{name}", (base,), ns)
    return dataclasses.dataclass(cls) if lite else cls


def _classes(key: str) -> dict:
    base, lite, _ = BASES[key]

    def make(name, fields):
        return _make(key, base, lite, name, fields)

    secret = make("Secret", {"owner": (str, ""), "token": (str, "")})
    team = make("Team", {"name": (str, "")})
    user = make("User", {"name": (str, ""), "extra": (dict, dict), "team": (Optional[team], None)})
    comment = make(
        "Comment",
        {
            "author": (str, ""),
            "extra": (dict, dict),
            "anything": (Any, None),
            "items": (list, list),
            "writer": (Optional[user], None),
            "writers": (list[user], list),
            "by_key": (dict[str, user], dict),
        },
    )
    plain = make("Plain", {"name": (str, ""), "extra": (dict, dict)})
    return {"secret": secret, "team": team, "user": user, "comment": comment, "plain": plain}


CLASSES = {key: _classes(key) for key in BASES}
WAYS = ["from_id", "from_ids", "refresh", "all", "first"]


# ---- helpers ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _empty_query_log():
    """The query log keeps at most 1000 entries and ``capture()`` reads from its length, so a
    full log captures nothing: start and leave each test with it empty."""
    query_logger.clear()
    yield
    query_logger.clear()


async def aw(x: Any) -> Any:
    return await x if inspect.isawaitable(x) else x


class World:
    def __init__(self, key: str):
        self.key = key
        self.c = CLASSES[key]
        self.t = {n: cls.__tablename__ for n, cls in self.c.items()}
        self.db: Any = None

    async def open(self) -> "World":
        if BASES[self.key][2]:
            self.db = AsyncSQLerDB.in_memory(shared=False)
            await self.db.connect()
        else:
            self.db = SQLerDB.in_memory(shared=False)
        safe = "safe" in self.key
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for cls in self.c.values():
                cls.set_db(self.db)
        if BASES[self.key][2]:
            for table in self.t.values():
                if safe:
                    await self.db._ensure_versioned_table(table)
                else:
                    await self.db._ensure_table(table)
        return self

    async def close(self) -> None:
        await aw(self.db.close())

    def ref(self, name: str, rid: Any) -> dict:
        return {"_table": self.t[name], "_id": rid}

    async def put(self, name: str, doc: dict) -> int:
        return await aw(self.db.insert_document(self.t[name], doc))

    async def stored(self, name: str, rid: int) -> str:
        """The row's JSON text; a safe base's ``_version`` (which every save moves) cut out."""
        cur = await aw(self.db.adapter.execute(f"SELECT data FROM {self.t[name]} WHERE _id = ?", [rid]))
        text = (await aw(cur.fetchone()))[0]
        await aw(cur.close())
        return re.sub(r',"_version":\d+}$', "}", text)

    async def load(self, name: str, rid: int, way: str) -> Any:
        cls = self.c[name]
        if way == "refresh":
            inst = await aw(cls.query().filter(F("_id") == rid).resolve(False).first())
            return await aw(inst.refresh())
        if way == "from_id":
            return await aw(cls.from_id(rid))
        if way == "from_ids":
            return (await aw(cls.from_ids([rid])))[0]
        if way == "all":
            return (await aw(cls.query().filter(F("_id") == rid).all()))[0]
        return await aw(cls.query().filter(F("_id") == rid).first())


def run(key: str, scenario) -> Any:
    async def main():
        world = await World(key).open()
        try:
            return await scenario(world)
        finally:
            await world.close()

    return asyncio.run(main())


def selects_from(sent: list, table: str) -> int:
    return sum(1 for q in sent if q.sql.lstrip().upper().startswith("SELECT") and f"FROM {table}" in q.sql)


def shape(x: Any) -> Any:
    """A dict as it is; a model as ``(class kind, _id, name)``."""
    if x is None or isinstance(x, dict):
        return x
    kind = "User" if type(x).__name__.endswith("User") else type(x).__name__
    return (kind, getattr(x, "_id", None), x.name)


# ---- p32: a reference in an untyped field -------------------------------------------


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_reference_in_an_untyped_field_is_given_back_and_its_table_is_not_read(key, way):
    async def scenario(w):
        await w.put("secret", {"owner": "admin", "token": "s3cr3t"})
        uid = await w.put("user", {"name": "u"})
        ref = w.ref("secret", 1)
        cid = await w.put(
            "comment",
            {"author": "visitor", "extra": ref, "anything": ref, "items": [ref], "writer": w.ref("user", uid)},
        )
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        return (
            (got.extra, got.anything, got.items, shape(got.writer)),
            selects_from(sent, w.t["secret"]),
            selects_from(sent, w.t["user"]),
        )

    ref = {"_table": f"rp_{key.replace('-', '_')}_secret", "_id": 1}
    assert run(key, scenario) == ((ref, ref, [ref], ("User", 1, "u")), 0, 1)


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_reference_deep_in_a_dict_and_in_a_list_of_dicts_is_given_back(key, way):
    async def scenario(w):
        await w.put("secret", {"owner": "admin", "token": "s3cr3t"})
        ref = w.ref("secret", 1)
        extra = {"a": {"b": {"c": ref}}}
        items = [{"x": ref}, {"y": [ref]}]
        cid = await w.put("comment", {"extra": extra, "items": items})
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        return got.extra == extra, got.items == items, selects_from(sent, w.t["secret"])

    assert run(key, scenario) == (True, True, 0)


# ---- inside a followed row ----------------------------------------------------------


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_followed_child_keeps_the_reference_in_its_own_untyped_field(key, way):
    async def scenario(w):
        await w.put("secret", {"owner": "admin", "token": "s3cr3t"})
        uid = await w.put("user", {"name": "u", "extra": w.ref("secret", 1)})
        cid = await w.put("comment", {"writer": w.ref("user", uid)})
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        return shape(got.writer), got.writer.extra, selects_from(sent, w.t["secret"])

    ref = {"_table": f"rp_{key.replace('-', '_')}_secret", "_id": 1}
    assert run(key, scenario) == (("User", 1, "u"), ref, 0)


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_followed_child_follows_its_own_typed_field(key, way):
    async def scenario(w):
        tid = await w.put("team", {"name": "t"})
        uid = await w.put("user", {"name": "u", "team": w.ref("team", tid)})
        cid = await w.put("comment", {"writer": w.ref("user", uid)})
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        team = got.writer.team
        return (type(team).__name__.endswith("Team"), team._id, team.name), selects_from(sent, w.t["team"])

    assert run(key, scenario) == ((True, 1, "t"), 1)


# ---- a typed position admits only its classes ---------------------------------------

# what a typed field gives for a reference into a table of a class it does not admit: the
# lite bases keep the dict; pydantic validates the dict as the field's class (fields at
# their defaults, no row id)
NOT_ADMITTED = {
    "pydantic": ("User", None, ""),
    "safe": ("User", None, ""),
    "lite": "ref",
    "lite-safe": "ref",
    "async-pydantic": ("User", None, ""),
    "async-safe": ("User", None, ""),
    "async-lite": "ref",
    "async-lite-safe": "ref",
}


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_typed_field_holding_a_reference_into_another_table_reads_nothing_from_it(key, way):
    async def scenario(w):
        await w.put("secret", {"owner": "admin", "token": "s3cr3t"})
        cid = await w.put("comment", {"writer": w.ref("secret", 1)})
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        return shape(got.writer), selects_from(sent, w.t["secret"])

    ref = {"_table": f"rp_{key.replace('-', '_')}_secret", "_id": 1}
    want = ref if NOT_ADMITTED[key] == "ref" else NOT_ADMITTED[key]
    assert run(key, scenario) == (want, 0)


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_list_and_a_dict_of_children_with_one_entry_into_another_table(key, way):
    async def scenario(w):
        await w.put("secret", {"owner": "admin", "token": "s3cr3t"})
        uid = await w.put("user", {"name": "u"})
        cid = await w.put(
            "comment",
            {
                "writers": [w.ref("user", uid), w.ref("secret", 1)],
                "by_key": {"a": w.ref("user", uid), "b": w.ref("secret", 1)},
            },
        )
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        return (
            [shape(x) for x in got.writers],
            {k: shape(v) for k, v in got.by_key.items()},
            selects_from(sent, w.t["secret"]),
            selects_from(sent, w.t["user"]),
        )

    ref = {"_table": f"rp_{key.replace('-', '_')}_secret", "_id": 1}
    other = ref if NOT_ADMITTED[key] == "ref" else NOT_ADMITTED[key]
    user = ("User", 1, "u")
    assert run(key, scenario) == ([user, other], {"a": user, "b": other}, 0, 1)


# ---- a model put in an untyped field ------------------------------------------------


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_model_put_in_an_untyped_field_reads_back_as_its_reference_and_saves_unchanged(key, way):
    async def scenario(w):
        user = await aw(w.c["user"](name="u").save())
        comment = await aw(w.c["comment"](author="a", anything=user, extra={"m": user}).save())
        before = await w.stored("comment", comment._id)
        got = await w.load("comment", comment._id, way)
        dirty = got.is_dirty() if hasattr(got, "is_dirty") else "no is_dirty"
        values = (got.anything, got.extra)
        await aw(got.save())
        return values, dirty, await w.stored("comment", comment._id) == before

    ref = {"_table": f"rp_{key.replace('-', '_')}_user", "_id": 1}
    dirty = "no is_dirty" if key in ("async-pydantic", "async-safe") else False
    assert run(key, scenario) == ((ref, {"m": ref}), dirty, True)


@pytest.mark.parametrize("key", list(BASES))
def test_refresh_gives_an_untyped_field_that_held_a_model_its_stored_reference(key):
    async def scenario(w):
        user = await aw(w.c["user"](name="u").save())
        comment = await aw(w.c["comment"](author="a", anything=user).save())
        held = comment.anything is user
        await aw(comment.refresh())
        return held, comment.anything

    ref = {"_table": f"rp_{key.replace('-', '_')}_user", "_id": 1}
    assert run(key, scenario) == (True, ref)


# ---- shapes that are never a reference ----------------------------------------------

BAD = [
    pytest.param("rp_nowhere; DROP TABLE x", 1, id="table-not-a-name"),
    pytest.param("USER", "1", id="id-str"),
    pytest.param("USER", True, id="id-bool"),
    pytest.param("USER", 2**63, id="id-past-int64"),
]


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("table,rid", BAD)
@pytest.mark.parametrize("key", list(BASES))
def test_a_dict_that_is_not_a_usable_reference_is_never_followed(key, table, rid, way):
    async def scenario(w):
        await w.put("user", {"name": "u"})
        bad = {"_table": w.t["user"] if table == "USER" else table, "_id": rid}
        cid = await w.put("comment", {"extra": {"k": bad}, "items": [bad]})
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        return got.extra == {"k": bad}, got.items == [bad], selects_from(sent, w.t["user"])

    assert run(key, scenario) == (True, True, 0)


# ---- a class with no typed field pays nothing ---------------------------------------

# statements of one read of a row of a class with no typed field, its table already known:
# the row's own SELECT ("refresh" also counts the query that loads the instance it refreshes)
STATEMENTS_OF_A_PLAIN_ROW: dict = {"from_id": 1, "from_ids": 1, "refresh": 2, "all": 1, "first": 1}


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_class_with_no_typed_field_sends_the_same_statements_with_a_reference_or_without(key, way):
    async def scenario(w):
        await w.put("secret", {"owner": "admin", "token": "s3cr3t"})
        with_ref = await w.put("plain", {"name": "r", "extra": w.ref("secret", 1)})
        without = await w.put("plain", {"name": "n", "extra": {}})
        await w.load("plain", without, way)  # the table's columns are learned once, here
        with query_logger.capture() as plain_sent:
            await w.load("plain", without, way)
        with query_logger.capture() as ref_sent:
            got = await w.load("plain", with_ref, way)
        sql = [q.sql for q in ref_sent]
        return len(sql), sql == [q.sql for q in plain_sent], selects_from(ref_sent, w.t["secret"]), got.extra

    ref = {"_table": f"rp_{key.replace('-', '_')}_secret", "_id": 1}
    count, same, secret_reads, extra = run(key, scenario)
    assert (same, secret_reads, extra) == (True, 0, ref)
    assert count == STATEMENTS_OF_A_PLAIN_ROW[way]


# ---- a forward reference in a lite class is a typed position -------------------------


@pytest.mark.parametrize("base", [SQLerLiteModel, AsyncSQLerLiteModel], ids=["lite", "async-lite"])
def test_a_lite_class_naming_itself_in_quotes_follows_the_reference(base):
    """``Optional["Category"]`` in a class defined in a function, as the lite tour writes it."""

    @dataclasses.dataclass
    class RpCategory(base):
        __tablename__ = f"rp_category_{base.__name__.lower()}"
        name: str = ""
        parent: Optional["RpCategory"] = None

    key = "async-lite" if base is AsyncSQLerLiteModel else "lite"

    async def scenario(w):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            RpCategory.set_db(w.db)
        if BASES[key][2]:
            await w.db._ensure_table(RpCategory.__tablename__)
        root = await aw(RpCategory(name="root").save())
        child = await aw(RpCategory(name="child", parent=root).save())
        got = [await aw(RpCategory.from_id(child._id)), (await aw(RpCategory.query().all()))[1]]
        return [(type(g.parent).__name__, g.parent._id, g.parent.name) for g in got]

    assert run(key, scenario) == [("RpCategory", 1, "root"), ("RpCategory", 1, "root")]


# ---- a msgspec class whose annotation names a class that is not there yet ------------


def test_a_msgspec_class_naming_a_class_not_defined_yet_follows_nothing_until_it_is():
    """As for the pydantic and the lite bases: no answer is kept while a name is not found."""
    from sqler.models import _refs
    from sqler.models.msgspec.model import SQLerMsgspecModel

    class RpMsHolder(SQLerMsgspecModel, kw_only=True):
        name: str = ""
        child: Optional["RpMsLater"] = None  # noqa: F821 - defined below, after the first read

    db = SQLerDB.in_memory()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        RpMsHolder.set_db(db, "rp_ms_holder")
    before = (_refs.typed_fields(RpMsHolder), RpMsHolder.query().all())

    class RpMsLater(SQLerMsgspecModel, kw_only=True):
        name: str = ""

    globals()["RpMsLater"] = RpMsLater
    try:
        after = _refs.typed_fields(RpMsHolder)
    finally:
        del globals()["RpMsLater"]
    db.close()

    assert before == ({}, [])
    assert after == {"child": (RpMsLater,)}
