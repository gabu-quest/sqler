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
import sqlite3
import sys
import types
import warnings
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Optional, Union

import pytest
from pairwise import all_pairs
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
from sqler.exceptions import ReferentialIntegrityError
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


# a field with no default
REQUIRED = object()


def _make(key: str, base: type, lite: bool, name: str, fields: dict) -> type:
    """A model class of ``base`` with ``fields`` (``{name: (annotation, default)}``); a
    default that is a type (``dict``, ``list``) is a factory, ``REQUIRED`` none."""
    ns: dict = {"__tablename__": f"rp_{key.replace('-', '_')}_{name.lower()}", "__annotations__": {}}
    for field_name, (annotation, default) in fields.items():
        ns["__annotations__"][field_name] = annotation
        if default is REQUIRED:
            continue
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
    admin = _make(key, user, lite, "Admin", {})
    holder = make(
        "Holder",
        {
            "one": (Optional[user], None),
            "many": (list[user], list),
            "by_key": (dict[str, user], dict),
            "either": (Optional[Union[dict, user]], None),
            "num": (Optional[Union[int, user]], None),
            "grid": (list[list[user]], list),
            "groups": (dict[str, list[user]], dict),
            "admin": (Optional[admin], None),
        },
    )
    strict = make("Strict", {"writer": (user, REQUIRED)})
    return {
        "secret": secret,
        "team": team,
        "user": user,
        "comment": comment,
        "plain": plain,
        "admin": admin,
        "holder": holder,
        "strict": strict,
    }


CLASSES = {key: _classes(key) for key in BASES}
WAYS = ["from_id", "from_ids", "refresh", "all", "first"]


# ---- helpers ------------------------------------------------------------------------


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


# ---- a reference in an untyped field ------------------------------------------------


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

@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_typed_field_holding_a_reference_into_another_table_reads_nothing_from_it(key, way):
    async def scenario(w):
        await w.put("secret", {"owner": "admin", "token": "s3cr3t"})
        cid = await w.put("comment", {"writer": w.ref("secret", 1)})
        with query_logger.capture() as sent:
            got = await w.load("comment", cid, way)
        return shape(got.writer), selects_from(sent, w.t["secret"])

    # it leads to no row of the field's class: ``None``, on every base
    assert run(key, scenario) == (None, 0)


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

    # the entry into another table leads to no row of the field's class: it is left out
    user = ("User", 1, "u")
    assert run(key, scenario) == ([user], {"a": user}, 0, 1)


# ---- a model put in an untyped field ------------------------------------------------


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_model_put_in_an_untyped_field_reads_back_as_its_reference_and_saves_unchanged(key, way):
    async def scenario(w):
        user = await aw(w.c["user"](name="u").save())
        comment = await aw(w.c["comment"](author="a", anything=user, extra={"m": user}).save())
        before = await w.stored("comment", comment._id)
        got = await w.load("comment", comment._id, way)
        dirty = got.is_dirty()
        values = (got.anything, got.extra)
        await aw(got.save())
        return values, dirty, await w.stored("comment", comment._id) == before

    ref = {"_table": f"rp_{key.replace('-', '_')}_user", "_id": 1}
    assert run(key, scenario) == ((ref, {"m": ref}), False, True)


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


@pytest.mark.parametrize(*all_pairs(("key", list(BASES)), ("table,rid", BAD), ("way", WAYS)))
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


# ---- where an annotation puts a model -------------------------------------------------


@dataclasses.dataclass
class RpShapeUser(SQLerLiteModel):
    __tablename__ = "rp_shape_user"
    name: str = ""


def _at(*admits, item=None, value=None, fixed=False):
    from sqler.models import _refs

    return _refs.Position(admits, item, value, fixed)


# names a module gives to an annotation: an assignment and a ``type`` statement
RpShapeUsers = list[RpShapeUser]
type RpShapePair = tuple[RpShapeUser, RpShapeUser]
type RpShapeTree = dict[str, RpShapeTree] | RpShapeUser  # names itself
RpShapeLoop = "RpShapeLoop | RpShapeUser"  # text that names itself

_USER = _at(RpShapeUser)
_IN_LIST = _at(item=_USER)
_IN_PLACES = _at(item=_USER, fixed=True)
_IN_DICT = _at(value=_USER)

# annotation -> the Position of the field, None where it puts no model; each one as the
# object and, where it differs, as the text a class writes under
# ``from __future__ import annotations`` or in quotes
SHAPES = [
    (RpShapeUser, _USER),
    ("RpShapeUser", _USER),
    (Optional[RpShapeUser], _USER),
    ("Optional[RpShapeUser]", _USER),
    ("typing.Optional[RpShapeUser]", _USER),
    ("RpShapeUser | None", _USER),
    ("None | RpShapeUser", _USER),
    (Union[int, RpShapeUser], _USER),
    ("Union[int, RpShapeUser]", _USER),
    (Annotated[RpShapeUser, "note"], _USER),
    ("Annotated[RpShapeUser, note]", _USER),
    (list[RpShapeUser], _IN_LIST),
    ("list[RpShapeUser]", _IN_LIST),
    ("List[RpShapeUser]", _IN_LIST),
    (list["RpShapeUser"], _IN_LIST),
    (set[RpShapeUser], _IN_LIST),
    ("set[RpShapeUser]", _IN_LIST),
    (frozenset[RpShapeUser], _IN_LIST),
    ("frozenset[RpShapeUser]", _IN_LIST),
    (Sequence[RpShapeUser], _IN_LIST),
    ("Sequence[RpShapeUser]", _IN_LIST),
    (tuple[RpShapeUser, ...], _IN_LIST),
    ("tuple[RpShapeUser, ...]", _IN_LIST),
    (tuple[RpShapeUser, RpShapeUser], _IN_PLACES),
    ("tuple[RpShapeUser, RpShapeUser]", _IN_PLACES),
    (tuple[int, RpShapeUser], _IN_PLACES),
    ("Tuple[int, RpShapeUser]", _IN_PLACES),
    (dict[str, RpShapeUser], _IN_DICT),
    ("dict[str, RpShapeUser]", _IN_DICT),
    (Mapping[str, RpShapeUser], _IN_DICT),
    ("Mapping[str, RpShapeUser]", _IN_DICT),
    (dict[str, list[RpShapeUser]], _at(value=_IN_LIST)),
    ("dict[str, list[RpShapeUser]]", _at(value=_IN_LIST)),
    (list[dict[str, RpShapeUser]], _at(item=_IN_DICT)),
    ("list[dict[str, RpShapeUser]]", _at(item=_IN_DICT)),
    (Optional[list[Optional[RpShapeUser]]], _IN_LIST),
    ("Optional[list[Optional[RpShapeUser]]]", _IN_LIST),
    ("list[RpShapeUser | None] | None", _IN_LIST),
    (Union[RpShapeUser, list[RpShapeUser]], _at(RpShapeUser, item=_USER)),
    ("RpShapeUser | list[RpShapeUser]", _at(RpShapeUser, item=_USER)),
    # the key of a dict, a class object, a value of a Literal: no stored model is there
    (dict[RpShapeUser, str], None),
    ("dict[RpShapeUser, str]", None),
    (type[RpShapeUser], None),
    ("type[RpShapeUser]", None),
    ("Literal['RpShapeUser']", None),
    (list, None),
    ("list", None),
    (dict[str, Any], None),
    # a name the module gives to an annotation is read through to what it names
    (RpShapeUsers, _IN_LIST),
    ("RpShapeUsers", _IN_LIST),
    ("Optional[RpShapeUsers]", _IN_LIST),
    ("dict[str, RpShapeUsers]", _at(value=_IN_LIST)),
    (RpShapePair, _IN_PLACES),
    ("RpShapePair", _IN_PLACES),
    (Optional[RpShapePair], _IN_PLACES),
    # a name that names itself is read through once
    (RpShapeTree, _USER),
    ("RpShapeTree", _USER),
    ("RpShapeLoop", _USER),
]


@pytest.mark.parametrize("annotation,want", SHAPES, ids=[str(a) for a, _ in SHAPES])
def test_typed_fields_reads_where_an_annotation_puts_a_model(annotation, want):
    """The same answer for an annotation and for its text."""
    from sqler.models import _refs

    ns = {"__tablename__": "rp_shape_holder", "__module__": __name__, "__annotations__": {"f": annotation}, "f": None}
    holder = dataclasses.dataclass(type("RpShapeHolder", (SQLerLiteModel,), ns))

    assert _refs.typed_fields(holder) == ({} if want is None else {"f": want})


@pytest.mark.parametrize(
    "key,quoted",
    [("pydantic", False), ("lite", False), ("lite", True)],
    ids=["pydantic", "lite", "lite-text"],
)
def test_a_gone_row_in_a_tuple_with_one_entry_per_place_keeps_the_places(key, quoted):
    """``tuple[User, User]`` has a first and a second: the row that is gone reads as ``None``
    at its place and the other stays second. In ``tuple[User, ...]`` it is left out. (A
    pydantic class resolves the text of its annotations itself.)"""
    base, lite, _ = BASES[key]
    user = _make(key, base, lite, "PairUser", {"name": (str, "")})
    globals()[user.__name__] = user  # where the text of an annotation finds it
    n = user.__name__
    pair = f"tuple[Optional[{n}], Optional[{n}]]" if quoted else tuple[Optional[user], Optional[user]]
    row = f"tuple[{n}, ...]" if quoted else tuple[user, ...]
    try:
        holder = _make(key, base, lite, f"PairHolder{int(quoted)}", {"pair": (pair, None), "row": (row, ())})
        db = SQLerDB.in_memory(shared=False)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            user.set_db(db)
            holder.set_db(db)
        db.insert_document(user.__tablename__, {"name": "kept"})
        gone = {"_table": user.__tablename__, "_id": 2}
        kept = {"_table": user.__tablename__, "_id": 1}
        db.insert_document(holder.__tablename__, {"pair": [gone, kept], "row": [gone, kept]})
        got = [holder.from_id(1), holder.from_ids([1])[0], holder.query().all()[0], holder.query().first()]
        db.close()
    finally:
        del globals()[n]

    def names(entries):
        return [None if e is None else (e._id, e.name) for e in entries]

    assert [(names(g.pair), names(g.row)) for g in got] == [([None, (1, "kept")], [(1, "kept")])] * 4


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
    assert after == {"child": _refs.Position((RpMsLater,), None, None)}


# ---- a pydantic class whose annotation names a class that is not there yet -----------


@pytest.mark.parametrize("base", [SQLerModel, SQLerSafeModel, AsyncSQLerModel, AsyncSQLerSafeModel])
def test_a_pydantic_class_naming_a_class_not_defined_yet_is_asked_once_until_a_class_is_defined(
    base, monkeypatch
):
    """pydantic resolves such a class at its first use. Before that it is asked here: once,
    then again only after a model class was defined, and never once it has resolved."""
    from sqler.models import _refs

    class RpPdHolder(base):
        name: str = ""
        child: Optional["RpPdLater"] = None  # noqa: F821 - defined below, after the first read

    asked = []
    rebuild = RpPdHolder.model_rebuild

    def counted(**kwargs):
        asked.append(kwargs)
        return rebuild(**kwargs)

    monkeypatch.setattr(RpPdHolder, "model_rebuild", counted)
    before = [_refs.known_typed_fields(RpPdHolder) for _ in range(3)]
    asks_before = len(asked)

    class RpPdOther(base):  # a class it does not name
        name: str = ""

    between = [_refs.known_typed_fields(RpPdHolder) for _ in range(3)]
    asks_between = len(asked)

    class RpPdLater(base):
        name: str = ""

    globals()["RpPdLater"] = RpPdLater
    try:
        after = [_refs.known_typed_fields(RpPdHolder) for _ in range(3)]
    finally:
        del globals()["RpPdLater"]

    assert (before, asks_before) == ([None] * 3, 1)
    assert (between, asks_between) == ([None] * 3, 2)
    assert after == [{"child": _refs.Position((RpPdLater,), None, None)}] * 3
    assert len(asked) == 3
    # asked with the names of its own module and of the place it was defined, no others
    assert asked == [{"raise_errors": False, "_parent_namespace_depth": 0}] * 3


@pytest.mark.parametrize(
    "base", [SQLerLiteModel, SQLerLiteSafeModel, AsyncSQLerLiteModel, AsyncSQLerLiteSafeModel]
)
def test_a_lite_class_left_without_dataclass_has_no_typed_field(base):
    """It has no field to read, and asking is no error: a delete of any class asks every
    class that is defined."""
    from sqler.models import _refs

    class RpNoDataclass(base):  # no ``@dataclass``
        child: Optional[RpLiteChild] = None

    assert _refs.known_typed_fields(RpNoDataclass) == {}


@dataclasses.dataclass
class RpLiteChild(SQLerLiteModel):
    name: str = ""


# ---- a typed reference that leads to no row of the field's class ----------------------
#
# One answer on every base and every way in: ``None`` where the reference is the field's
# value, left out where it is an entry of a list or a value of a dict (the key goes too).


def pic(key: str, x: Any) -> Any:
    """A loaded value all the way down: a model as ``(class, _id, name)``, the class named
    without the base's prefix."""
    if x is None or isinstance(x, (str, int)):
        return x
    if isinstance(x, list):
        return [pic(key, v) for v in x]
    if isinstance(x, dict):
        return {k: pic(key, v) for k, v in x.items()}
    prefix = f"Rp{''.join(p.title() for p in key.split('-'))}"
    return (type(x).__name__.removeprefix(prefix), getattr(x, "_id", None), getattr(x, "name", "-"))


def _held(w: World, spec: Any) -> Any:
    """``("user", 1)`` is a reference into a class's table; ``("raw", d)`` is ``d`` as it is."""
    if isinstance(spec, tuple) and spec[0] == "raw":
        return spec[1]
    if isinstance(spec, tuple):
        return w.ref(*spec)
    if isinstance(spec, list):
        return [_held(w, v) for v in spec]
    if isinstance(spec, dict):
        return {k: _held(w, v) for k, v in spec.items()}
    return spec


U1 = ("User", 1, "u1")
GONE_USER = ("user", 99)
NO_CLASS = ("raw", {"_table": "rp_no_class_owns_it", "_id": 1})

# (field, what it holds, what it reads as, the table it must not be read from)
NO_ROW = [
    pytest.param("one", GONE_USER, None, None, id="one-gone"),
    pytest.param("many", [("user", 1), GONE_USER], [U1], None, id="many-one-gone"),
    pytest.param("many", [("user", 98), GONE_USER], [], None, id="many-all-gone"),
    pytest.param("many", [GONE_USER, ("user", 1), GONE_USER], [U1], None, id="many-same-gone-twice"),
    pytest.param("by_key", {"a": ("user", 1), "b": GONE_USER}, {"a": U1}, None, id="dict-one-gone"),
    pytest.param("one", ("secret", 1), None, "secret", id="one-other-class"),
    pytest.param("many", [("user", 1), ("secret", 1)], [U1], "secret", id="many-other-class"),
    pytest.param("by_key", {"a": ("user", 1), "b": ("secret", 1)}, {"a": U1}, "secret", id="dict-other-class"),
    pytest.param("one", NO_CLASS, None, None, id="one-no-class"),
    pytest.param("many", [("user", 1), NO_CLASS], [U1], None, id="many-no-class"),
    pytest.param("either", ("secret", 1), None, "secret", id="union-dict-reference-not-followed"),
    pytest.param("either", ("user", 1), U1, None, id="union-dict-reference-followed"),
    pytest.param("num", 5, 5, "user", id="union-int-holding-an-int"),
    pytest.param("num", ("user", 1), U1, None, id="union-int-holding-a-reference"),
    pytest.param("grid", [[("user", 1), GONE_USER], [GONE_USER]], [[U1], []], None, id="list-of-lists"),
    pytest.param(
        "groups", {"a": [("user", 1), GONE_USER], "b": [GONE_USER]}, {"a": [U1], "b": []}, None, id="dict-of-lists"
    ),
    pytest.param("one", ("admin", 1), ("Admin", 1, "a1"), None, id="parent-class-field-subclass-row"),
    pytest.param("admin", ("user", 1), None, "user", id="subclass-field-parent-class-row"),
]


async def _two_rows(w: World) -> None:
    await w.put("user", {"name": "u1"})
    await w.put("admin", {"name": "a1"})
    await w.put("secret", {"owner": "admin", "token": "s3cr3t"})


@pytest.mark.parametrize(*all_pairs(("key", list(BASES)), ("field,held,want,unread", NO_ROW), ("way", WAYS)))
def test_a_typed_reference_to_no_row_of_the_fields_class(key, field, held, want, unread, way):
    async def scenario(w):
        await _two_rows(w)
        hid = await w.put("holder", {field: _held(w, held)})
        with query_logger.capture() as sent:
            got = await w.load("holder", hid, way)
        return pic(key, getattr(got, field)), selects_from(sent, w.t[unread]) if unread else 0

    assert run(key, scenario) == (want, 0)


# a reference (a str ``_table``, an int ``_id``) no row can be at
UNUSABLE = [p for p in BAD if p.id in {"table-not-a-name", "id-past-int64"}]


@pytest.mark.parametrize(*all_pairs(("key", list(BASES)), ("table,rid", UNUSABLE), ("way", WAYS)))
def test_an_unusable_reference_at_a_typed_position_reads_as_no_row(key, table, rid, way):
    async def scenario(w):
        await w.put("user", {"name": "u1"})
        bad = {"_table": w.t["user"] if table == "USER" else table, "_id": rid}
        hid = await w.put("holder", {"one": bad, "many": [bad], "by_key": {"b": bad}})
        with query_logger.capture() as sent:
            got = await w.load("holder", hid, way)
        return got.one, got.many, got.by_key, selects_from(sent, w.t["user"])

    assert run(key, scenario) == (None, [], {}, 0)


# a reference deeper than the annotation puts a model is data: nothing is read for it, and
# it stays the dict it is; a pydantic base validates the dict at a ``User`` position as a
# ``User`` (no row id), a lite base keeps it
DEEPER_THAN_A_USER = {
    k: ("User", None, "") if not BASES[k][1] else {"deeper": "ref"} for k in BASES
}


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_reference_deeper_than_the_annotation_puts_a_model_is_data(key, way):
    async def scenario(w):
        await w.put("user", {"name": "u1"})
        ref = w.ref("user", 1)
        hid = await w.put("holder", {"either": {"k": ref}, "by_key": {"k": {"deeper": ref}}})
        with query_logger.capture() as sent:
            got = await w.load("holder", hid, way)
        deeper = pic(key, got.by_key["k"])
        if isinstance(deeper, dict):
            deeper = {k: "ref" if v == ref else v for k, v in deeper.items()}
        return got.either == {"k": ref}, deeper, selects_from(sent, w.t["user"])

    assert run(key, scenario) == (True, DEEPER_THAN_A_USER[key], 0)


# Dicts that are not references (a reference is exactly ``_table``, a str, and ``_id``, an int
# that is not a bool), each held at a typed position: read as any other dict there is (a
# pydantic base validates it as a ``User`` with no row id, a lite base keeps the dict), and
# the row it names is not read. ``USER`` stands for the user table, which has row 1.
NOT_A_REFERENCE = [
    pytest.param({"_table": "USER", "_id": 1, "label": "data"}, id="more-keys"),
    pytest.param({"_table": "USER", "_id": True}, id="id-bool"),
]


@pytest.mark.parametrize(*all_pairs(("key", list(BASES)), ("held", NOT_A_REFERENCE), ("way", WAYS)))
def test_a_dict_that_is_not_a_reference_at_a_typed_position_is_not_followed(key, held, way):
    async def scenario(w):
        await w.put("user", {"name": "u1"})
        data = {k: w.t["user"] if v == "USER" else v for k, v in held.items()}
        twin = w.ref("user", 1)
        hid = await w.put("holder", {"one": data, "many": [twin, data], "by_key": {"a": twin, "b": data}})
        with query_logger.capture() as sent:
            got = await w.load("holder", hid, way)
        return (
            (pic(key, got.one), pic(key, got.many), pic(key, got.by_key)),
            pic(key, data),
            selects_from(sent, w.t["user"]),
        )

    (one, many, by_key), data, user_reads = run(key, scenario)
    read = data if BASES[key][1] else ("User", None, "")
    assert (one, many, by_key) == (read, [U1, read], {"a": U1, "b": read})
    assert user_reads == 1


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_gone_row_two_levels_down(key, way):
    async def scenario(w):
        uid = await w.put("user", {"name": "u1", "team": w.ref("team", 99)})
        hid = await w.put("holder", {"one": w.ref("user", uid), "many": [w.ref("user", uid)]})
        got = await w.load("holder", hid, way)
        return pic(key, got.one), got.one.team, [m.team for m in got.many]

    assert run(key, scenario) == (U1, None, [None])


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_typed_reference_into_a_dropped_table(key, way):
    async def scenario(w):
        await w.put("user", {"name": "u1"})
        hid = await w.put("holder", {"one": w.ref("user", 1), "many": [w.ref("user", 1)]})
        await aw(w.db.adapter.execute(f"DROP TABLE {w.t['user']}"))
        got = await w.load("holder", hid, way)
        return got.one, got.many

    assert run(key, scenario) == (None, [])


@pytest.mark.parametrize("key", list(BASES))
def test_refresh_after_a_child_row_was_deleted(key):
    async def scenario(w):
        await w.put("user", {"name": "u1"})
        await w.put("user", {"name": "u2"})
        hid = await w.put(
            "holder", {"one": w.ref("user", 2), "many": [w.ref("user", 1), w.ref("user", 2)]}
        )
        got = await w.load("holder", hid, "from_id")
        before = pic(key, got.one), pic(key, got.many)
        await aw(w.db.delete_document(w.t["user"], 2))
        await aw(got.refresh())
        return before, (got.one, pic(key, got.many))

    assert run(key, scenario) == (
        (("User", 2, "u2"), [U1, ("User", 2, "u2")]),
        (None, [U1]),
    )


# a field that cannot hold ``None``: the walk gives ``None`` and the class says so
REQUIRED_FIELD = {
    k: ("ValidationError", "writer") if not BASES[k][1] else ("loaded", None) for k in BASES
}


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_required_typed_field_whose_row_is_not_there(key, way):
    async def scenario(w):
        await w.put("user", {"name": "u1"})
        sid = await w.put("strict", {"writer": w.ref("user", 99)})
        try:
            got = await w.load("strict", sid, way)
        except Exception as exc:  # the outcome is pinned, whatever it is
            first = str(exc).splitlines()[1] if "\n" in str(exc) else str(exc)
            return type(exc).__name__, first.strip()
        return "loaded", got.writer

    assert run(key, scenario) == REQUIRED_FIELD[key]


# ``is_dirty()``: a model whose reference read as no row would be saved without it
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_model_loaded_with_a_reference_to_no_row_is_dirty(key, way):
    async def scenario(w):
        user, holder = w.c["user"], w.c["holder"]
        u1 = await aw(user(name="u1").save())
        u2 = await aw(user(name="u2").save())
        saved = [
            await aw(holder(one=u1, many=[u1], by_key={"a": u1}).save()),
            await aw(holder(one=u2).save()),
            await aw(holder(many=[u1, u2]).save()),
            await aw(holder(by_key={"a": u1, "b": u2}).save()),
        ]
        await aw(w.db.delete_document(w.t["user"], u2._id))
        out = []
        for h in saved:
            got = await w.load("holder", h._id, way)
            out.append(got.is_dirty())
        return out

    assert run(key, scenario) == [False, True, True, True]


# statements of one load of a holder: its row's SELECT ("refresh" also the query that loads the
# instance it refreshes) and one SELECT of the users' table, whether its references lead to
# rows or not; a reference into a class the field does not admit reads nothing
STATEMENTS_OF_A_HOLDER: dict = {"from_id": 2, "from_ids": 2, "refresh": 3, "all": 2, "first": 2}


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_reference_to_no_row_costs_what_a_found_one_costs(key, way):
    async def scenario(w):
        await w.put("user", {"name": "u1"})
        await w.put("user", {"name": "u2"})
        found = await w.put("holder", {"one": w.ref("user", 2), "many": [w.ref("user", 1), w.ref("user", 2)]})
        lost = await w.put(
            "holder",
            {
                "one": w.ref("user", 99),
                "many": [w.ref("user", 1), w.ref("user", 98)],
                "by_key": {"b": w.ref("secret", 1)},
            },
        )
        await w.load("holder", found, way)  # the tables' columns are learned once, here
        with query_logger.capture() as found_sent:
            await w.load("holder", found, way)
        with query_logger.capture() as lost_sent:
            await w.load("holder", lost, way)
        shapes = [[q.sql.split(" WHERE ")[0] for q in sent] for sent in (found_sent, lost_sent)]
        return len(shapes[1]), shapes[0] == shapes[1]

    assert run(key, scenario) == (STATEMENTS_OF_A_HOLDER[way], True)


def test_a_msgspec_query_reads_a_typed_reference_to_no_row_by_the_same_rule():
    from sqler.models.msgspec.model import SQLerMsgspecModel

    class RpMsUser(SQLerMsgspecModel, kw_only=True):
        name: str = ""

    class RpMsOwner(SQLerMsgspecModel, kw_only=True):
        one: Optional[RpMsUser] = None
        many: list[RpMsUser] = []
        by_key: dict[str, RpMsUser] = {}

    globals()["RpMsUser"] = RpMsUser
    db = SQLerDB.in_memory()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            RpMsUser.set_db(db, "rp_ms_user")
            RpMsOwner.set_db(db, "rp_ms_owner")
        db.insert_document("rp_ms_user", {"name": "u1"})
        ref, gone = {"_table": "rp_ms_user", "_id": 1}, {"_table": "rp_ms_user", "_id": 99}
        db.insert_document("rp_ms_owner", {"one": gone, "many": [ref, gone], "by_key": {"a": ref, "b": gone}})
        got = [RpMsOwner.query().all()[0], RpMsOwner.query().first()]
        out = [
            (g.one, [(type(m).__name__, m._id, m.name) for m in g.many], sorted(g.by_key)) for g in got
        ]
    finally:
        del globals()["RpMsUser"]
        db.close()

    assert out == [(None, [("RpMsUser", 1, "u1")], ["a"])] * 2


# ---- an error of the database while a reference is read ------------------------------


def break_reads_of(w: World, table: str) -> None:
    """From now on a SELECT from ``table`` raises what a locked database raises, sent with the
    adapter's ``execute`` or with ``_execute_judged`` (the readers that read a missing table
    as no row)."""
    adapter = w.db.adapter

    def refused(sql: str) -> bool:
        return sql.lstrip().upper().startswith("SELECT") and f"FROM {table}" in sql

    def broken(real):
        if inspect.iscoroutinefunction(real):

            async def execute(sql, *args, **kwargs):
                if refused(sql):
                    raise sqlite3.OperationalError("database is locked")
                return await real(sql, *args, **kwargs)

            return execute

        def execute(sql, *args, **kwargs):
            if refused(sql):
                raise sqlite3.OperationalError("database is locked")
            return real(sql, *args, **kwargs)

        return execute

    adapter.execute = broken(adapter.execute)
    adapter._execute_judged = broken(adapter._execute_judged)


@pytest.mark.parametrize(*all_pairs(("key", list(BASES)), ("way", WAYS), ("broken", ["user", "team"])))
def test_a_database_error_while_a_reference_is_read_reaches_the_caller(key, way, broken):
    """The read of the child's table (``user``) fails, or the read of the child's own child's
    (``team``). The load raises that error; it gives back no model with the reference unread."""

    async def scenario(w):
        tid = await w.put("team", {"name": "t1"})
        uid = await w.put("user", {"name": "u1", "team": w.ref("team", tid)})
        hid = await w.put("holder", {"one": w.ref("user", uid), "many": [w.ref("user", uid)]})
        before = await w.load("holder", hid, way)
        break_reads_of(w, w.t[broken])
        with pytest.raises(sqlite3.OperationalError) as err:
            await w.load("holder", hid, way)
        return before.one.name, before.one.team.name, before.many[0].team.name, str(err.value)

    assert run(key, scenario) == ("u1", "t1", "t1", "database is locked")


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("key", list(BASES))
def test_a_typed_reference_into_a_table_that_holds_no_documents_raises(key, way):
    """The table is there and is not a table of documents. That is SQLite's own error, where a
    table that is not there reads as no row."""

    async def scenario(w):
        uid = await w.put("user", {"name": "u1"})
        hid = await w.put("holder", {"one": w.ref("user", uid)})
        await aw(w.db.adapter.execute(f"DROP TABLE {w.t['user']}"))
        await aw(w.db.adapter.execute(f"CREATE TABLE {w.t['user']} (x TEXT)"))
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await w.load("holder", hid, way)
        return True

    assert run(key, scenario) is True


# ---- a quoted class name the module does not have ------------------------------------

_STOCK_MODULE = """
from dataclasses import dataclass
from sqler import {base}

@dataclass
class RpStockItem({base}):
    __tablename__ = "{table}"
    name: str = ""
"""
_ORDER_MODULE = """
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
from sqler import {base}

if TYPE_CHECKING:
    from {warehouse} import RpStockItem

@dataclass
class RpOrder({base}):
    __tablename__ = "{table}"
    item: Optional["RpStockItem"] = None
"""


def _module(monkeypatch, name: str, source: str, **names: str) -> Any:
    """A module ``name`` made of ``source``, as an import of a file would make it."""
    module = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, module)
    exec(source.format(**names), module.__dict__)
    return module


async def _stock_db(asynchronous: bool, *classes: type) -> Any:
    db = AsyncSQLerDB.in_memory(shared=False) if asynchronous else SQLerDB.in_memory(shared=False)
    await aw(db.connect())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for cls in classes:
            cls.set_db(db)
    return db


@pytest.mark.parametrize("base", ["SQLerLiteModel", "AsyncSQLerLiteModel"])
@pytest.mark.parametrize("last", ["shop", "warehouse"])
def test_a_quoted_name_its_module_lacks_is_every_model_class_of_that_name(monkeypatch, base, last):
    """Two modules each have a class ``RpStockItem``; a third names ``"RpStockItem"`` in
    quotes and imports the warehouse's only for the type checker. Whichever was defined
    last, a reference to a warehouse row reads as that row and holds its delete back."""
    tag = f"rp_{base.lower()}_{last}_last"
    made = {}
    for part in sorted(("shop", "warehouse"), key=lambda part: part == last):
        made[part] = _module(monkeypatch, f"{tag}_{part}", _STOCK_MODULE, base=base, table=f"{tag}_{part}")
    shop, warehouse = made["shop"].RpStockItem, made["warehouse"].RpStockItem
    order_cls = _module(
        monkeypatch, f"{tag}_orders", _ORDER_MODULE, base=base, warehouse=f"{tag}_warehouse", table=f"{tag}_orders"
    ).RpOrder

    async def main():
        in_order = [module.RpStockItem for module in made.values()]  # bound as defined
        db = await _stock_db(base.startswith("Async"), *in_order, order_cls)
        try:
            wanted = await aw(warehouse(name="from the warehouse").save())
            other = await aw(shop(name="from the shop").save())
            assert other._id == wanted._id  # a row of the wrong table shows as a wrong name
            order = await aw(order_cls(item=wanted).save())

            back = await aw(order_cls.from_id(order._id))
            assert (type(back.item), back.item.name) == (warehouse, "from the warehouse")

            with pytest.raises(ReferentialIntegrityError):
                await aw(wanted.delete())
            left = await aw(db.find_document(warehouse.__tablename__, wanted._id))
            assert left["name"] == "from the warehouse"
        finally:
            await aw(db.close())

    asyncio.run(main())


def test_a_class_defined_after_a_quoted_name_was_first_read_is_admitted(monkeypatch):
    """The field is read while only the shop's class has the name; the warehouse's class is
    defined afterwards, and a reference to one of its rows is then followed and counted."""
    base, tag = "SQLerLiteModel", "rp_defined_later"
    shop = _module(monkeypatch, f"{tag}_shop", _STOCK_MODULE, base=base, table=f"{tag}_shop").RpStockItem
    order_cls = _module(
        monkeypatch, f"{tag}_orders", _ORDER_MODULE, base=base, warehouse=f"{tag}_warehouse", table=f"{tag}_orders"
    ).RpOrder
    db = SQLerDB.in_memory(shared=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        shop.set_db(db)
        order_cls.set_db(db)
    try:
        first = order_cls(item=shop(name="from the shop").save()).save()
        assert order_cls.from_id(first._id).item.name == "from the shop"

        warehouse = _module(
            monkeypatch, f"{tag}_warehouse", _STOCK_MODULE, base=base, table=f"{tag}_warehouse"
        ).RpStockItem
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warehouse.set_db(db)
        wanted = warehouse(name="from the warehouse").save()
        order = order_cls(item=wanted).save()

        back = order_cls.from_id(order._id)
        assert (type(back.item), back.item.name) == (warehouse, "from the warehouse")
        with pytest.raises(ReferentialIntegrityError):
            wanted.delete()
        assert db.find_document(warehouse.__tablename__, wanted._id)["name"] == "from the warehouse"
    finally:
        db.close()
