"""A set-based write on an async class is a write: ``update()``, ``update_one()``,
``update_n()`` and ``delete_all()`` move the version, stamp the times and write the audit rows
the instance ways do, and the rows a delete's policy changes meet their own class's rules. No
hook is awaited."""

import asyncio
import itertools
import json
import re
import types
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pytest
import pytest_asyncio
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
)
from sqler.exceptions import StaleVersionError
from sqler.logging import query_logger
from sqler.models.mixins import AsyncAuditLogMixin, AsyncAuditMixin, AsyncHooksMixin, TimestampMixin
from sqler.query import F

pytestmark = pytest.mark.asyncio

BASES = {
    "pydantic": AsyncSQLerModel,
    "pydantic_safe": AsyncSQLerSafeModel,
    "lite": AsyncSQLerLiteModel,
    "lite_safe": AsyncSQLerLiteSafeModel,
}
KINDS = list(BASES)
SAFE_KINDS = ["pydantic_safe", "lite_safe"]

_tables = itertools.count()


def make(kind, *mixins, promoted=False):
    table = f"asw_{kind}_{next(_tables)}"
    annotations = {"name": str, "count": int}
    ns = {"__tablename__": table, "name": "", "count": 0}
    if promoted:
        annotations["status"] = Optional[str]
        ns["status"] = None
        ns["__promoted__"] = {"status": "TEXT"}
    ns["__annotations__"] = annotations
    cls = types.new_class(f"Item_{table}", (*mixins, BASES[kind]), {}, lambda body: body.update(ns))
    return dataclass(cls) if kind.startswith("lite") else cls


def bound(cls, db):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)
    return cls.query()


@pytest_asyncio.fixture
async def db():
    AsyncAuditMixin.set_current_user_getter(lambda: "alice")
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    yield db
    await db.close()
    AsyncAuditMixin._current_user_getter = None


async def _rows(db, sql, params=()):
    cur = await db.adapter.execute(sql, list(params))
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def _exists(db, table):
    return bool(
        await _rows(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table])
    )


async def _audit(db, cls):
    table = f"{cls.__tablename__}_audit"
    if not await _exists(db, table):
        return []
    return [
        (r[0], r[1], r[2], json.loads(r[3]) if r[3] else None, json.loads(r[4]))
        for r in await _rows(
            db, f"SELECT record_id, action, user, changes, snapshot FROM {table} ORDER BY _id;"
        )
    ]


async def _after_creates(db, cls):
    return [row for row in await _audit(db, cls) if row[1] != "create"]


async def _stored(db, table):
    columns = [r[1] for r in await _rows(db, f"PRAGMA table_info({table});")]
    extra = ", status" if "status" in columns else ""
    docs = []
    for row in await _rows(db, f"SELECT _id, data{extra} FROM {table} ORDER BY _id;"):
        doc = json.loads(row[1])
        if extra:
            doc["status"] = row[2]
        docs.append((row[0], doc))
    return docs


async def _versions(db, table):
    return [r[0] for r in await _rows(db, f"SELECT _version FROM {table} ORDER BY _id;")]


# ----- the statements of a class with no rule are the ones it sent ----------------------------


class Plain(AsyncSQLerModel):
    __tablename__ = "asw_pin_plain"
    name: str = ""
    k: int = 0


class Safe(AsyncSQLerSafeModel):
    __tablename__ = "asw_pin_safe"
    name: str = ""
    k: int = 0


def _g(table, *names):
    """The guard a statement built from what its database remembers of ``table`` carries
    (``names``: the columns ``SELECT *`` gives): a branch that never runs."""
    row = ", ".join(f"`{name}`" for name in names)
    return f"CASE WHEN 1 THEN 1 ELSE (SELECT ({row}) IN (SELECT * FROM {table}) FROM {table}) END"


PINS = [
    (
        Plain,
        lambda q: q.filter(F("k") == 0).update(name="x", k=F("k") + 1),
        "UPDATE asw_pin_plain SET data = json_set(data, '$.name', ?, '$.k', "
        "json_extract(data, '$.k') + ?) WHERE JSON_EXTRACT(data, '$.k') = ? AND " + _g("asw_pin_plain", "_id", "data"),
        ["x", 1, 0],
    ),
    (
        Plain,
        lambda q: q.filter(F("k") >= 0).order_by("name", desc=True).update_one(name="y"),
        "UPDATE asw_pin_plain SET data = json_set(data, '$.name', ?) WHERE _id = (SELECT _id FROM "
        "asw_pin_plain WHERE JSON_EXTRACT(data, '$.k') >= ? AND " + _g("asw_pin_plain", "_id", "data") + " ORDER BY json_extract(data, '$.name') "
        "DESC LIMIT 1) RETURNING _id, data",
        ["y", 0],
    ),
    (
        Plain,
        lambda q: q.filter(F("name") == "nope").delete_all(),
        "DELETE FROM asw_pin_plain WHERE JSON_EXTRACT(data, '$.name') = ? AND " + _g("asw_pin_plain", "_id", "data"),
        ["nope"],
    ),
    (
        Safe,
        lambda q: q.filter(F("_id") == 1).update_one(k=6),
        "UPDATE asw_pin_safe SET data = json_set(data, '$.k', ?, '$._version', _version + 1), _version = _version + 1 WHERE "
        "_id = (SELECT _id FROM asw_pin_safe WHERE _id = ? AND " + _g("asw_pin_safe", "_id", "data", "_version") + " LIMIT 1) RETURNING _id, data, _version",
        [6, 1],
    ),
    (
        Safe,
        lambda q: q.filter(F("_id") >= 1).update_n(3, k=7),
        "UPDATE asw_pin_safe SET data = json_set(data, '$.k', ?, '$._version', _version + 1), _version = _version + 1 WHERE "
        "_id IN (SELECT _id FROM asw_pin_safe WHERE _id >= ? AND " + _g("asw_pin_safe", "_id", "data", "_version") + " LIMIT 3) RETURNING _id, data, _version",
        [7, 1],
    ),
    (
        Safe,
        lambda q: q.filter(F("_id") == 1).update(k=5),
        "UPDATE asw_pin_safe SET data = json_set(data, '$.k', ?, '$._version', _version + 1), _version = _version + 1 "
        "WHERE _id = ? AND " + _g("asw_pin_safe", "_id", "data", "_version"),
        [5, 1],
    ),
]


@pytest.mark.parametrize(
    "cls,way,sql,params",
    PINS,
    ids=["plain update", "plain update_one", "plain delete_all", "safe update_one", "safe update_n", "safe update"],
)
async def test_a_class_with_no_audit_log_sends_one_statement(db, cls, way, sql, params):
    await cls().save(db=db)
    q = bound(cls, db)
    with query_logger.capture() as got:
        await way(q)
    assert [(e.sql, list(e.params or [])) for e in got] == [(sql, params)]


# ----- a versioned class: every update moves the version -------------------------------------


@pytest.mark.parametrize(
    "way",
    [
        lambda q, i: q.filter(F("_id") == i).update(count=500),
        lambda q, i: q.filter(F("_id") == i).update_one(count=500),
        lambda q, i: q.filter(F("_id") == i).update_n(1, count=500),
    ],
    ids=["update", "update_one", "update_n"],
)
@pytest.mark.parametrize("kind", SAFE_KINDS)
async def test_a_stale_save_after_a_set_based_update_raises(db, kind, way):
    Item = make(kind)
    item = await Item(name="o", count=100).save(db=db)
    held = await bound(Item, db).first()
    await way(bound(Item, db), item._id)
    held.name = "h"

    with pytest.raises(StaleVersionError):
        await held.save(db=db)

    assert [d["count"] for _, d in await _stored(db, Item.__tablename__)] == [500]
    assert await _versions(db, Item.__tablename__) == [1]


@pytest.mark.parametrize("kind", SAFE_KINDS)
async def test_version_cannot_be_set_on_a_versioned_class(db, kind):
    Item = make(kind)
    await Item(name="a").save(db=db)

    with pytest.raises(ValueError, match="_version cannot be set"):
        await bound(Item, db).update(_version=7)

    assert await _versions(db, Item.__tablename__) == [0]


@pytest.mark.parametrize("way", ["update", "update_one", "update_n", "delete_all"])
@pytest.mark.parametrize("kind", ["pydantic", "pydantic_safe"])
async def test_a_write_in_a_rolled_back_transaction_leaves_rows_version_and_audit(db, kind, way):
    Item = make(kind, AsyncAuditLogMixin)
    await Item(name="a", count=1).save(db=db)
    before = (await _stored(db, Item.__tablename__), await _audit(db, Item))
    q = bound(Item, db)
    ways = {
        "update": lambda: q.update(count=2),
        "update_one": lambda: q.update_one(count=2),
        "update_n": lambda: q.update_n(5, count=2),
        "delete_all": lambda: q.delete_all(),
    }

    with pytest.raises(LookupError):
        async with db.transaction():
            await ways[way]()
            raise LookupError("the caller changes its mind")

    assert (await _stored(db, Item.__tablename__), await _audit(db, Item)) == before
    if kind == "pydantic_safe":
        assert await _versions(db, Item.__tablename__) == [0]


# ----- a class that stamps -------------------------------------------------------------------


@pytest.mark.parametrize("way", ["update", "update_one", "update_n"])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_set_based_update_stamps_updated_at_as_save_stores_it(db, kind, way, ticking_clock):
    Item = make(kind, TimestampMixin)
    for i in range(2):
        await Item(name=f"r{i}", count=i).save(db=db)
    saved = (await _stored(db, Item.__tablename__))[0][1]["updated_at"]
    q = bound(Item, db)

    with query_logger.capture() as got:
        result = await {
            "update": lambda: q.update(name="x"),
            "update_one": lambda: q.update_one(name="x"),
            "update_n": lambda: q.update_n(2, name="x"),
        }[way]()

    assert len(got) == 1
    changed = [d for _, d in await _stored(db, Item.__tablename__) if d["name"] == "x"]
    assert len(changed) == (1 if way == "update_one" else 2)
    stamps = {d["updated_at"] for d in changed}
    assert len(stamps) == 1
    stamp = stamps.pop()
    assert re.sub(r"\d", "9", stamp) == re.sub(r"\d", "9", saved)
    assert datetime.fromisoformat(stamp) > datetime.fromisoformat(saved)
    loaded = [i.updated_at for i in await bound(Item, db).filter(F("name") == "x").order_by("count").all()]
    if way == "update_one":
        assert result.updated_at == loaded[0]
    elif way == "update_n":
        assert [r.updated_at for r in result] == loaded


async def test_an_async_audit_mixin_class_stamps_updated_by(db):
    Item = make("pydantic", AsyncAuditMixin)
    await Item(name="a").save(db=db)
    AsyncAuditMixin.set_current_user_getter(lambda: "bob")

    got = await bound(Item, db).update_one(name="b")

    doc = (await _stored(db, Item.__tablename__))[0][1]
    assert (doc["created_by"], doc["updated_by"], got.updated_by) == ("alice", "bob", "bob")


# ----- an audited class: the audit rows a loop of instance writes would write -----------------


async def _twins(db, kind, promoted=False):
    pair = []
    for _ in range(2):
        Item = make(kind, AsyncAuditLogMixin, promoted=promoted)
        for i in range(3):
            extra = {"status": "new"} if promoted else {}
            await Item(name=f"r{i}", count=i, **extra).save(db=db)
        pair.append(Item)
    return pair


async def _loop_save(db, Item, ids, **changes):
    for _id in ids:
        inst = await bound(Item, db).filter(F("_id") == _id).first()
        for name, value in changes.items():
            setattr(inst, name, value(inst) if callable(value) else value)
        await inst.save(db=db)


@pytest.mark.parametrize("kind", KINDS)
async def test_update_writes_the_audit_rows_a_loop_of_save_writes(db, kind):
    Set, Loop = await _twins(db, kind, promoted=True)

    assert await bound(Set, db).filter(F("count") >= 1).update(count=F("count") + 10, status="done") == 2
    await _loop_save(db, Loop, [2, 3], count=lambda i: i.count + 10, status="done")

    assert len(await _after_creates(db, Set)) == 2
    assert await _after_creates(db, Set) == await _after_creates(db, Loop)
    assert (await _after_creates(db, Set))[0][3] == {
        "count": {"old": 1, "new": 11},
        "status": {"old": "new", "new": "done"},
    }


@pytest.mark.parametrize("kind", KINDS)
async def test_update_one_and_update_n_write_the_audit_rows_a_save_writes(db, kind):
    Set, Loop = await _twins(db, kind)

    one = await bound(Set, db).order_by("count", desc=True).update_one(name="one")
    many = await bound(Set, db).order_by("count").update_n(2, name="many")
    await _loop_save(db, Loop, [3], name="one")
    await _loop_save(db, Loop, [1, 2], name="many")

    assert (one._id, [m._id for m in many]) == (3, [1, 2])
    assert len(await _after_creates(db, Set)) == 3
    assert await _after_creates(db, Set) == await _after_creates(db, Loop)


@pytest.mark.parametrize("kind", KINDS)
async def test_delete_all_writes_the_audit_rows_a_loop_of_delete_writes(db, kind):
    Set, Loop = await _twins(db, kind)

    assert await bound(Set, db).filter(F("count") >= 1).delete_all() == 2
    for _id in (2, 3):
        await (await bound(Loop, db).filter(F("_id") == _id).first()).delete(db=db)

    assert await _after_creates(db, Set) == await _after_creates(db, Loop)
    assert await _after_creates(db, Set) == [
        (2, "delete", "alice", None, {"name": "r1", "count": 1}),
        (3, "delete", "alice", None, {"name": "r2", "count": 2}),
    ]


WAYS = {
    "update": (lambda q: q.filter(F("name") == "none").update(name="x"), 0),
    "update_one": (lambda q: q.filter(F("name") == "none").update_one(name="x"), None),
    "update_n": (lambda q: q.filter(F("name") == "none").update_n(3, name="x"), []),
    "delete_all": (lambda q: q.filter(F("name") == "none").delete_all(), 0),
}


@pytest.mark.parametrize("way", list(WAYS))
async def test_a_write_that_matches_no_row_writes_no_audit_row_and_leaves_no_transaction(db, way):
    Item = make("pydantic", AsyncAuditLogMixin)
    await Item(name="a").save(db=db)
    fn, nothing = WAYS[way]

    assert await fn(bound(Item, db)) == nothing

    assert await _after_creates(db, Item) == []
    assert db.adapter.in_transaction is False


@pytest.mark.parametrize("way", list(WAYS))
async def test_a_missing_table_reads_as_empty_and_stays_missing(db, way):
    Item = make("pydantic", AsyncAuditLogMixin, TimestampMixin)
    fn, nothing = WAYS[way]

    assert await fn(Item.using(db)) == nothing

    assert not await _exists(db, Item.__tablename__)
    assert not await _exists(db, f"{Item.__tablename__}_audit")


async def test_two_thousand_rows_are_one_read_one_write_and_their_audit_rows(db):
    Item = make("pydantic", AsyncAuditLogMixin)
    await db.insert_many(Item.__tablename__, [{"name": "r", "count": i} for i in range(2000)])
    q = bound(Item, db)

    with query_logger.capture() as got:
        assert await q.update(name="x") == 2000

    assert [e.sql.split()[0] for e in got] == ["SELECT", "UPDATE", "CREATE"] + ["INSERT"] * 2000


async def test_a_set_based_write_awaits_no_hook(db):
    calls = []

    class Hooked(AsyncHooksMixin, AsyncAuditLogMixin, AsyncSQLerModel):
        __tablename__ = "asw_hooked"
        name: str = ""

        async def before_save(self):
            calls.append("before_save")
            return True

        async def before_delete(self):
            calls.append("before_delete")
            return True

    await Hooked(name="a").save(db=db)
    calls.clear()
    q = bound(Hooked, db)

    await q.update(name="b")
    await q.update_one(name="c")
    await q.update_n(1, name="d")
    await q.delete_all()

    assert calls == []
    assert [r[1] for r in await _audit(db, Hooked)] == ["create", "update", "update", "update", "delete"]


# ----- two tasks on two adapters claim each row once ------------------------------------------


class Claim(AsyncAuditLogMixin, AsyncSQLerModel):
    __tablename__ = "asw_claims"
    name: str = ""
    owner: Optional[str] = None


async def test_two_tasks_on_two_adapters_claim_ten_rows_never_one_twice(tmp_path):
    AsyncAuditMixin.set_current_user_getter(lambda: "alice")
    path = str(tmp_path / "claims.db")
    first = AsyncSQLerDB.on_disk(path)
    await first.connect()
    for i in range(10):
        await Claim(name=f"c{i}").save(db=first)
    second = AsyncSQLerDB.on_disk(path)
    await second.connect()
    claimed: dict[str, list[int]] = {"a": [], "b": []}
    go = asyncio.Event()

    async def worker(name, db):
        await go.wait()
        while True:
            got = await Claim.using(db).filter(F("owner") == None).update_one(owner=name)  # noqa: E711
            if got is None:
                return
            claimed[name].append(got._id)

    tasks = [asyncio.create_task(worker(n, d)) for n, d in (("a", first), ("b", second))]
    go.set()
    await asyncio.gather(*tasks)

    try:
        assert sorted(claimed["a"] + claimed["b"]) == list(range(1, 11))
        updates = [r for r in await _audit(first, Claim) if r[1] == "update"]
        assert sorted(r[0] for r in updates) == list(range(1, 11))
        for record_id, _, _, changes, _ in updates:
            owner = "a" if record_id in claimed["a"] else "b"
            assert changes == {"owner": {"old": None, "new": owner}}
    finally:
        await first.close()
        await second.close()
        AsyncAuditMixin._current_user_getter = None


# ----- the rows a delete's policy changes meet their own class's rules ------------------------


class Shelf(AsyncAuditLogMixin, AsyncSQLerModel):
    __tablename__ = "asw_shelves"
    name: str


class Volume(AsyncAuditLogMixin, TimestampMixin, AsyncSQLerModel):
    __tablename__ = "asw_volumes"
    title: str
    shelf: Optional[Shelf] = None


class Page(AsyncSQLerModel):
    __tablename__ = "asw_pages"
    n: int
    volume: Optional[Volume] = None


async def test_set_null_writes_the_referrers_update_row_and_moves_its_updated_at(db, ticking_clock):
    shelf = await Shelf(name="s").save(db=db)
    shelf_id = shelf._id
    vol = await Volume(title="v", shelf=shelf).save(db=db)
    stamped = (await _stored(db, "asw_volumes"))[0][1]["updated_at"]

    await shelf.delete_with_policy(db=db, on_delete="set_null")

    [(rid, action, user, changes, _)] = await _after_creates(db, Volume)
    doc = (await _stored(db, "asw_volumes"))[0][1]
    assert (rid, action, user) == (vol._id, "update", "alice")
    assert changes["shelf"] == {"old": {"_table": "asw_shelves", "_id": shelf_id}, "new": None}
    assert datetime.fromisoformat(doc["updated_at"]) > datetime.fromisoformat(stamped)


class Binder(AsyncSQLerModel):
    __tablename__ = "asw_binders"
    name: str = ""


class Sheet(AsyncSQLerSafeModel):
    __tablename__ = "asw_sheets"
    title: str = ""
    binder: Optional[Binder] = None


@dataclass
class LiteBinder(AsyncSQLerLiteModel):
    __tablename__ = "asw_lite_binders"
    name: str = ""


@dataclass
class LiteSheet(AsyncSQLerLiteSafeModel):
    __tablename__ = "asw_lite_sheets"
    title: str = ""
    binder: Optional[LiteBinder] = None


@pytest.mark.parametrize("target, referrer", [(Binder, Sheet), (LiteBinder, LiteSheet)], ids=SAFE_KINDS)
async def test_set_null_moves_the_version_of_a_versioned_referrer_so_a_stale_save_is_refused(db, target, referrer):
    binder = await target(name="b").save(db=db)
    await referrer(title="s", binder=binder).save(db=db)
    held = await bound(referrer, db).first()
    table = referrer.__tablename__

    await binder.delete_with_policy(db=db, on_delete="set_null")

    assert await _rows(db, f"SELECT _version FROM {table};") == [(1,)]
    held.title = "edited by a holder who loaded it before the delete"
    with pytest.raises(StaleVersionError):
        await held.save(db=db)
    [(_, doc)] = await _stored(db, table)
    assert (doc["title"], doc["binder"]) == ("s", None)
    assert await _rows(db, f"SELECT _version FROM {table};") == [(1,)]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_delete_of_a_row_already_gone_logs_nothing_and_raises_nothing(db, kind):
    Item = make(kind, AsyncAuditLogMixin)
    inst = await Item(name="a", count=1).save(db=db)
    await db.delete_document(Item.__tablename__, inst._id)

    await inst.delete(db=db)

    assert await _after_creates(db, Item) == []
    assert await _stored(db, Item.__tablename__) == []


async def test_a_cascade_three_levels_deep_logs_the_audited_level(db):
    shelf = await Shelf(name="s").save(db=db)
    vol = await Volume(title="v", shelf=shelf).save(db=db)
    await Page(n=1, volume=vol).save(db=db)
    stored_vol = (await _stored(db, "asw_volumes"))[0][1]

    await shelf.delete_with_policy(db=db, on_delete="cascade")

    assert await _stored(db, "asw_pages") == []
    assert await _after_creates(db, Volume) == [(vol._id, "delete", "alice", None, stored_vol)]


@pytest.mark.parametrize("kind", KINDS)
async def test_an_instance_edited_and_not_saved_logs_the_stored_row_on_delete(db, kind):
    Item = make(kind, AsyncAuditLogMixin)
    inst = await Item(name="stored", count=1).save(db=db)
    inst.name = "edited, never saved"

    await inst.delete(db=db)

    assert await _after_creates(db, Item) == [(1, "delete", "alice", None, {"name": "stored", "count": 1})]


# ----- delete_all() looks for the rows that refer to the set, once per referring table -----------


def holder_of(kind, target):
    """A class of ``kind`` whose ``target`` field holds a row of ``target``."""
    table = f"asw_holder_{kind}_{next(_tables)}"
    ns = {"__tablename__": table, "target": None, "__annotations__": {"target": Optional[target]}}
    cls = types.new_class(f"Holder_{table}", (BASES[kind],), {}, lambda body: body.update(ns))
    return dataclass(cls) if kind.startswith("lite") else cls


@pytest.mark.parametrize("kind", KINDS)
async def test_delete_all_of_one_row_and_of_ten_thousand_send_the_same_statements(db, kind):
    Item = make(kind)
    Holder = holder_of(kind, Item)
    await Item.save_many([Item(name="one")] + [Item(name="many") for _ in range(10_000)], db=db)
    await Holder().save(db=db)
    version = ", _version" if kind in SAFE_KINDS else ""
    names = [Item.__tablename__, Item.__name__.lower()]  # the names a reference to an Item may give, in order

    def look(ids):
        """The one read of the look for ``ids``: one id is also looked for by its digits."""
        digits = "data GLOB ('*' || substr(?1, 2, length(?1) - 2) || '*') AND " if len(ids) == 1 else ""
        sql = (
            f"SELECT _id, data FROM {Holder.__tablename__} WHERE (({digits}(data GLOB '*{names[0]}*' OR "
            f"data GLOB '*{names[1]}*' OR data GLOB '*\\u*')) OR data >= x'') AND "
            "(json_extract(data, '$.target._id') IN (SELECT value FROM json_each(?1)));"
        )
        return sql, [json.dumps(ids)]

    q = bound(Item, db)
    for name, ids in (("one", [1]), ("many", list(range(2, 10_002)))):
        with query_logger.capture() as got:
            count = await q.filter(F("name") == name).delete_all()
        assert count == len(ids)
        assert [(e.sql, list(e.params or [])) for e in got] == [
            (
                f"DELETE FROM {Item.__tablename__} WHERE JSON_EXTRACT(data, '$.name') = ? AND "
                f"{_g(Item.__tablename__, '_id', 'data', *version[2:].split())} RETURNING _id, data{version}",
                [name],
            ),
            look(ids),
        ]
