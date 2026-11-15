"""A set-based write is a write: a queryset's ``update()``, ``update_one()``, ``update_n()`` and
``delete_all()`` move the version of a versioned class, stamp the times of a class that
stamps and write the audit rows of an audited class, as the instance ways do; and the rows a
delete's policy nulls or deletes meet the rules of their own class. No hook is called."""

import itertools
import json
import re
import sqlite3
import threading
import types
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, Optional

import pytest
from sqler import (
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
)
from sqler.exceptions import StaleVersionError
from sqler.logging import query_logger
from sqler.models.mixins import AuditLogMixin, AuditMixin, HooksMixin, TimestampMixin
from sqler.query import F

BASES = {
    "pydantic": SQLerModel,
    "pydantic_safe": SQLerSafeModel,
    "lite": SQLerLiteModel,
    "lite_safe": SQLerLiteSafeModel,
    "msgspec": SQLerMsgspecModel,
}
KINDS = list(BASES)
SAFE_KINDS = ["pydantic_safe", "lite_safe"]

_tables = itertools.count()


def make(kind, *mixins, promoted=False):
    """A model class of ``kind`` with the mixins, ``name`` and ``count`` fields (and a promoted
    ``status``) on a table of its own."""
    table = f"sw_{kind}_{next(_tables)}"
    annotations = {"name": str, "count": int}
    ns = {"__tablename__": table, "name": "", "count": 0}
    if promoted:
        annotations["status"] = Optional[str]
        ns["status"] = None
        ns["__promoted__"] = {"status": "TEXT"}
    ns["__annotations__"] = annotations
    kwds = {"tag": False} if kind == "msgspec" else {}
    cls = types.new_class(f"Item_{table}", (*mixins, BASES[kind]), kwds, lambda body: body.update(ns))
    return dataclass(cls) if kind.startswith("lite") else cls


def bound(cls, db):
    """``cls.query()`` on ``db``: the queryset each base gives its own class."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)
    return cls.query()


@pytest.fixture
def db():
    AuditMixin.set_current_user("alice")
    db = SQLerDB.in_memory(shared=False)
    yield db
    db.close()
    AuditMixin.set_current_user(None)


def _exists(db, table):
    return (
        db.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table]
        ).fetchone()
        is not None
    )


def _audit(db, cls):
    """``(record_id, action, user, changes, snapshot)`` of each audit row, in order."""
    table = f"{cls.__tablename__}_audit"
    if not _exists(db, table):
        return []
    return [
        (r[0], r[1], r[2], json.loads(r[3]) if r[3] else None, json.loads(r[4]))
        for r in db.adapter.execute(
            f"SELECT record_id, action, user, changes, snapshot FROM {table} ORDER BY _id;"
        ).fetchall()
    ]


def _stored(db, table):
    """``(_id, document)`` of each row, the promoted ``status`` merged when there is one."""
    columns = [r[1] for r in db.adapter.execute(f"PRAGMA table_info({table});").fetchall()]
    extra = ", status" if "status" in columns else ""
    rows = db.adapter.execute(f"SELECT _id, data{extra} FROM {table} ORDER BY _id;").fetchall()
    docs = []
    for row in rows:
        doc = json.loads(row[1])
        if extra:
            doc["status"] = row[2]
        docs.append((row[0], doc))
    return docs


def _version_column(db, table):
    return [r[0] for r in db.adapter.execute(f"SELECT _version FROM {table} ORDER BY _id;").fetchall()]


def _sent(fn):
    with query_logger.capture() as got:
        result = fn()
    return result, [(e.sql, list(e.params or [])) for e in got]


# ----- the statements of a class with no rule are the ones it sent ----------------------------


class Plain(SQLerModel):
    __tablename__ = "sw_pin_plain"
    name: str = ""
    k: int = 0


class Safe(SQLerSafeModel):
    __tablename__ = "sw_pin_safe"
    name: str = ""
    k: int = 0


class Job(SQLerSafeModel):
    """The shape of a job a queue claims: promoted columns, F-expressions, its own clock."""

    __tablename__ = "sw_pin_job"
    __promoted__: ClassVar[dict[str, str]] = {"status": "TEXT", "priority": "INTEGER"}
    status: str = "pending"
    priority: int = 0
    attempts: int = 0
    updated_at: int = 0


PINS = [
    (
        "plain update",
        Plain,
        lambda q: q.filter(F("k") == 0).update(name="x", k=F("k") + 1),
        "UPDATE sw_pin_plain SET data = json_set(data, '$.name', ?, '$.k', "
        "json_extract(data, '$.k') + ?) WHERE JSON_EXTRACT(data, '$.k') = ?",
        ["x", 1, 0],
    ),
    (
        "plain update with no filter",
        Plain,
        lambda q: q.update(flag=True),
        "UPDATE sw_pin_plain SET data = json_set(data, '$.flag', json(?))",
        ["true"],
    ),
    (
        "plain update_one",
        Plain,
        lambda q: q.filter(F("k") >= 0).order_by("name", desc=True).update_one(name="y"),
        "UPDATE sw_pin_plain SET data = json_set(data, '$.name', ?) WHERE _id = (SELECT _id FROM "
        "sw_pin_plain WHERE JSON_EXTRACT(data, '$.k') >= ? ORDER BY json_extract(data, '$.name') "
        "DESC LIMIT 1) RETURNING _id, data",
        ["y", 0],
    ),
    (
        "plain update_n",
        Plain,
        lambda q: q.filter(F("k") >= 0).order_by("k").update_n(2, name="z", tags=["a"]),
        "UPDATE sw_pin_plain SET data = json_set(data, '$.name', ?, '$.tags', json(?)) WHERE _id IN "
        "(SELECT _id FROM sw_pin_plain WHERE JSON_EXTRACT(data, '$.k') >= ? ORDER BY "
        "json_extract(data, '$.k') LIMIT 2) RETURNING _id, data",
        ["z", '["a"]', 0],
    ),
    (
        "plain delete_all",
        Plain,
        lambda q: q.filter(F("name") == "nope").delete_all(),
        "DELETE FROM sw_pin_plain WHERE JSON_EXTRACT(data, '$.name') = ?",
        ["nope"],
    ),
    (
        "safe update_one",
        Safe,
        lambda q: q.filter(F("_id") == 1).update_one(k=6),
        "UPDATE sw_pin_safe SET data = json_set(data, '$.k', ?, '$._version', _version + 1), _version = _version + 1 WHERE "
        "_id = (SELECT _id FROM sw_pin_safe WHERE _id = ? LIMIT 1) RETURNING _id, data, _version",
        [6, 1],
    ),
    (
        "safe update_n",
        Safe,
        lambda q: q.filter(F("_id") >= 1).update_n(3, k=7),
        "UPDATE sw_pin_safe SET data = json_set(data, '$.k', ?, '$._version', _version + 1), _version = _version + 1 WHERE "
        "_id IN (SELECT _id FROM sw_pin_safe WHERE _id >= ? LIMIT 3) RETURNING _id, data, _version",
        [7, 1],
    ),
    (
        "job claim update_one",
        Job,
        lambda q: q.filter((F("status") == "pending") & (F("attempts") < 3))
        .order_by("priority", desc=True)
        .update_one(status="running", attempts=F("attempts") + 1, updated_at=17),
        "UPDATE sw_pin_job SET data = json_set(data, '$.attempts', json_extract(data, '$.attempts') "
        "+ ?, '$.updated_at', ?, '$._version', _version + 1), status = ?, _version = _version + 1 WHERE _id = (SELECT "
        "_id FROM "
        "sw_pin_job WHERE (status = ?) AND (JSON_EXTRACT(data, '$.attempts') < ?) ORDER BY priority "
        "DESC LIMIT 1) RETURNING _id, data, _version, status, priority",
        [1, 17, "running", "pending", 3],
    ),
    (
        "job batch update_n",
        Job,
        lambda q: q.filter(F("status") == "running").order_by("priority").update_n(
            5, status="pending", priority=F("priority") - 1
        ),
        "UPDATE sw_pin_job SET data = json_set(data, '$._version', _version + 1), status = ?, priority = priority - ?, "
        "_version = _version + 1 WHERE "
        "_id IN (SELECT _id FROM sw_pin_job WHERE status = ? ORDER BY priority LIMIT 5) RETURNING "
        "_id, data, _version, status, priority",
        ["pending", 1, "running"],
    ),
    (
        "job delete_all",
        Job,
        lambda q: q.filter(F("priority") > 100).delete_all(),
        "DELETE FROM sw_pin_job WHERE priority > ?",
        [100],
    ),
]


@pytest.mark.parametrize("label,cls,way,sql,params", PINS, ids=[p[0] for p in PINS])
def test_a_class_with_no_rule_sends_the_one_statement_it_sent(db, label, cls, way, sql, params):
    cls().save(db=db)
    q = bound(cls, db)
    _, sent = _sent(lambda: way(q))
    assert sent == [(sql, params)]


def test_update_on_a_versioned_class_gains_only_the_version_term(db):
    Safe().save(db=db)
    q = bound(Safe, db)
    _, sent = _sent(lambda: q.filter(F("_id") == 1).update(k=5))
    assert sent == [
        (
            "UPDATE sw_pin_safe SET data = json_set(data, '$.k', ?, '$._version', _version + 1), _version = _version + 1 "
            "WHERE _id = ?",
            [5, 1],
        )
    ]


def test_job_update_gains_only_the_version_term(db):
    Job().save(db=db)
    q = bound(Job, db)
    _, sent = _sent(lambda: q.filter(F("status") == "pending").update(priority=3))
    assert sent == [
        (
            "UPDATE sw_pin_job SET data = json_set(data, '$._version', _version + 1), priority = ?, "
            "_version = _version + 1 WHERE status = ?",
            [3, "pending"],
        )
    ]


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
def test_a_stale_save_after_a_set_based_update_raises(db, kind, way):
    Item = make(kind)
    item = Item(name="o", count=100).save(db=db)
    held = bound(Item, db).first()
    way(bound(Item, db), item._id)
    held.name = "h"

    with pytest.raises(StaleVersionError):
        held.save(db=db)

    assert [d["count"] for _, d in _stored(db, Item.__tablename__)] == [500]
    assert _version_column(db, Item.__tablename__) == [1]


def test_update_moves_the_version_of_every_row_it_changes_by_one(db):
    Item = make("pydantic_safe")
    for i in range(3):
        Item(name=f"r{i}", count=i).save(db=db)
    bound(Item, db).first().save(db=db)  # row 1 at version 1

    assert bound(Item, db).filter(F("count") <= 1).update(name="x") == 2

    assert _version_column(db, Item.__tablename__) == [2, 1, 0]


@pytest.mark.parametrize(
    "way",
    [
        lambda q: q.update(_version=7),
        lambda q: q.update_one(_version=7),
        lambda q: q.update_n(2, _version=7),
    ],
    ids=["update", "update_one", "update_n"],
)
@pytest.mark.parametrize("kind", SAFE_KINDS)
def test_version_cannot_be_set_on_a_versioned_class(db, kind, way):
    Item = make(kind)
    Item(name="a").save(db=db)

    with pytest.raises(ValueError, match="_version cannot be set"):
        way(bound(Item, db))

    assert _version_column(db, Item.__tablename__) == [0]
    assert [d.get("_version") for _, d in _stored(db, Item.__tablename__)] == [0]


def test_version_named_on_a_class_with_no_version_is_a_document_field_as_before(db):
    Item = make("pydantic")
    Item(name="a").save(db=db)

    assert bound(Item, db).update(_version=7) == 1

    assert _stored(db, Item.__tablename__) == [(1, {"name": "a", "count": 0, "_version": 7})]


@pytest.mark.parametrize("way", ["update", "update_one", "update_n"])
@pytest.mark.parametrize("kind", ["pydantic", "pydantic_safe", "lite_safe"])
def test_an_update_in_a_rolled_back_transaction_leaves_the_row_and_version(db, kind, way):
    Item = make(kind, AuditLogMixin) if kind != "pydantic" else make(kind)
    Item(name="a", count=1).save(db=db)
    audit_before = _audit(db, Item)
    stored_before = _stored(db, Item.__tablename__)
    q = bound(Item, db)

    with pytest.raises(LookupError):
        with db.transaction():
            {"update": lambda: q.update(count=2), "update_one": lambda: q.update_one(count=2),
             "update_n": lambda: q.update_n(5, count=2)}[way]()
            raise LookupError("the caller changes its mind")

    assert _stored(db, Item.__tablename__) == stored_before
    assert _audit(db, Item) == audit_before
    if kind != "pydantic":
        assert _version_column(db, Item.__tablename__) == [0]
    assert db.adapter.in_transaction is False


def test_delete_all_in_a_rolled_back_transaction_leaves_the_rows(db):
    Item = make("pydantic")
    Item(name="a").save(db=db)

    with pytest.raises(LookupError):
        with db.transaction():
            bound(Item, db).delete_all()
            raise LookupError("the caller changes its mind")

    assert [d["name"] for _, d in _stored(db, Item.__tablename__)] == ["a"]


@pytest.mark.parametrize("way", ["update", "update_one", "update_n", "delete_all"])
@pytest.mark.parametrize("kind", ["pydantic", "pydantic_safe"])
def test_a_set_based_write_outside_a_transaction_is_seen_by_another_connection_at_once(tmp_path, kind, way):
    path = str(tmp_path / "set_ways.db")
    db = SQLerDB.on_disk(path)
    try:
        Item = make(kind)
        Item(name="a", count=1).save(db=db)
        q = bound(Item, db)

        {"update": lambda: q.update(count=2), "update_one": lambda: q.update_one(count=2),
         "update_n": lambda: q.update_n(5, count=2), "delete_all": q.delete_all}[way]()

        other = sqlite3.connect(path)
        try:
            seen = other.execute(f"SELECT json_extract(data, '$.count') FROM {Item.__tablename__};").fetchall()
        finally:
            other.close()
        assert seen == ([] if way == "delete_all" else [(2,)])
        assert db.adapter.in_transaction is False
    finally:
        db.close()


# ----- a class that stamps: updated_at (and updated_by) in the same statement -----------------


def _iso(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize("way", ["update", "update_one", "update_n"])
@pytest.mark.parametrize("kind", KINDS)
def test_a_set_based_update_stamps_updated_at_as_save_stores_it(db, kind, way):
    Item = make(kind, TimestampMixin)
    for i in range(2):
        Item(name=f"r{i}").save(db=db)
    saved = [d["updated_at"] for _, d in _stored(db, Item.__tablename__)]
    q = bound(Item, db)
    start = datetime.now(timezone.utc)

    result, sent = _sent(
        {
            "update": lambda: q.update(name="x"),
            "update_one": lambda: q.update_one(name="x"),
            "update_n": lambda: q.update_n(2, name="x"),
        }[way]
    )

    assert len(sent) == 1
    rows = _stored(db, Item.__tablename__)
    changed = [d for _, d in rows if d["name"] == "x"]
    assert len(changed) == (1 if way == "update_one" else 2)
    stamps = {d["updated_at"] for d in changed}
    assert len(stamps) == 1  # the statement's one time
    stamp = stamps.pop()
    assert re.sub(r"\d", "9", stamp) == re.sub(r"\d", "9", saved[0])
    assert _iso(stamp) >= start and _iso(stamp).tzinfo is not None
    assert [d["created_at"] for _, d in rows] == [d["created_at"] for _, d in _stored(db, Item.__tablename__)]
    loaded = [i.updated_at for i in bound(Item, db).filter(F("name") == "x").order_by("count").all()]
    if way == "update_one":
        assert result.updated_at == loaded[0]
    elif way == "update_n":
        assert [r.updated_at for r in result] == loaded


@pytest.mark.parametrize("kind", KINDS)
def test_a_field_the_caller_names_is_the_callers(db, kind):
    Item = make(kind, TimestampMixin)
    Item(name="a").save(db=db)
    mine = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    q = bound(Item, db)

    _, sent = _sent(lambda: q.update(updated_at=mine))

    assert _stored(db, Item.__tablename__)[0][1]["updated_at"] == mine.isoformat()
    assert sent[0][0].count("updated_at") == 1


def test_an_audit_mixin_class_stamps_updated_by(db):
    Item = make("pydantic", AuditMixin)
    Item(name="a").save(db=db)
    AuditMixin.set_current_user("bob")

    got = bound(Item, db).update_one(name="b")

    doc = _stored(db, Item.__tablename__)[0][1]
    assert (doc["created_by"], doc["updated_by"], got.updated_by) == ("alice", "bob", "bob")


def test_a_stamp_on_a_promoted_field_goes_to_its_column(db):
    class Stamped(TimestampMixin, SQLerModel):
        __tablename__ = "sw_promoted_stamp"
        __promoted__: ClassVar[dict[str, str]] = {"updated_at": "TEXT"}
        name: str = ""

    Stamped(name="a").save(db=db)
    before = db.adapter.execute("SELECT updated_at FROM sw_promoted_stamp;").fetchone()[0]

    bound(Stamped, db).update(name="b")

    after = db.adapter.execute("SELECT updated_at, data FROM sw_promoted_stamp;").fetchone()
    assert _iso(after[0]) > _iso(before)
    assert "updated_at" not in json.loads(after[1])


# ----- an audited class: the audit rows a loop of instance writes would write -----------------


def _twins(db, kind, promoted=False):
    """Two audited classes holding the same three rows, their create rows written."""
    pair = []
    for _ in range(2):
        Item = make(kind, AuditLogMixin, promoted=promoted)
        for i in range(3):
            extra = {"status": "new"} if promoted else {}
            Item(name=f"r{i}", count=i, **extra).save(db=db)
        pair.append(Item)
    return pair


def _after_creates(db, cls):
    return [row for row in _audit(db, cls) if row[1] != "create"]


def _loop_save(db, Item, ids, **changes):
    for _id in ids:
        inst = bound(Item, db).filter(F("_id") == _id).first()
        for name, value in changes.items():
            setattr(inst, name, value(inst) if callable(value) else value)
        inst.save(db=db)


@pytest.mark.parametrize("promoted", [False, True])
@pytest.mark.parametrize("kind", KINDS)
def test_update_writes_the_audit_rows_a_loop_of_save_writes(db, kind, promoted):
    Set, Loop = _twins(db, kind, promoted)
    extra = {"status": "done"} if promoted else {}

    assert bound(Set, db).filter(F("count") >= 1).update(count=F("count") + 10, name="x", **extra) == 2
    _loop_save(db, Loop, [2, 3], count=lambda i: i.count + 10, name="x", **extra)

    assert len(_after_creates(db, Set)) == 2
    assert _after_creates(db, Set) == _after_creates(db, Loop)
    assert _after_creates(db, Set)[0][3]["count"] == {"old": 1, "new": 11}


@pytest.mark.parametrize("kind", KINDS)
def test_update_one_writes_the_audit_row_a_save_writes(db, kind):
    Set, Loop = _twins(db, kind, promoted=True)

    got = bound(Set, db).filter(F("count") >= 0).order_by("count", desc=True).update_one(
        status="claimed", count=F("count") + 1
    )
    _loop_save(db, Loop, [3], status="claimed", count=lambda i: i.count + 1)

    assert (got._id, got.count, got.status) == (3, 3, "claimed")
    assert _after_creates(db, Set) == _after_creates(db, Loop)
    assert _after_creates(db, Set)[0][3] == {
        "count": {"old": 2, "new": 3},
        "status": {"old": "new", "new": "claimed"},
    }


@pytest.mark.parametrize("kind", KINDS)
def test_update_n_writes_the_audit_rows_a_loop_of_save_writes(db, kind):
    Set, Loop = _twins(db, kind)

    got = bound(Set, db).order_by("count").update_n(2, name="claimed")
    _loop_save(db, Loop, [1, 2], name="claimed")

    assert [g._id for g in got] == [1, 2]
    assert len(_after_creates(db, Set)) == 2
    assert _after_creates(db, Set) == _after_creates(db, Loop)


@pytest.mark.parametrize("kind", KINDS)
def test_delete_all_writes_the_audit_rows_a_loop_of_delete_writes(db, kind):
    Set, Loop = _twins(db, kind, promoted=True)

    assert bound(Set, db).filter(F("count") >= 1).delete_all() == 2
    for _id in (2, 3):
        bound(Loop, db).filter(F("_id") == _id).first().delete(db=db)

    assert _after_creates(db, Set) == _after_creates(db, Loop)
    assert _after_creates(db, Set) == [
        (2, "delete", "alice", None, {"name": "r1", "count": 1, "status": "new"}),
        (3, "delete", "alice", None, {"name": "r2", "count": 2, "status": "new"}),
    ]


def test_delete_all_with_no_filter_writes_a_delete_row_per_row(db):
    Item = make("pydantic", AuditLogMixin)
    for i in range(3):
        Item(name=f"r{i}", count=i).save(db=db)

    assert bound(Item, db).delete_all() == 3

    assert [(r[0], r[1], r[4]["name"]) for r in _after_creates(db, Item)] == [
        (1, "delete", "r0"),
        (2, "delete", "r1"),
        (3, "delete", "r2"),
    ]
    assert _stored(db, Item.__tablename__) == []


def test_an_update_that_changes_nothing_writes_no_audit_row_as_save_writes_none(db):
    Set, Loop = _twins(db, "pydantic")

    assert bound(Set, db).filter(F("_id") == 1).update(name="r0") == 1
    _loop_save(db, Loop, [1], name="r0")

    assert _after_creates(db, Set) == _after_creates(db, Loop) == []


def test_an_audited_versioned_class_moves_the_version_and_logs(db):
    Item = make("pydantic_safe", AuditLogMixin, TimestampMixin)
    Item(name="a").save(db=db)

    bound(Item, db).update(name="b")

    assert _version_column(db, Item.__tablename__) == [1]
    [(rid, action, user, changes, snapshot)] = _after_creates(db, Item)
    assert (rid, action, user, sorted(changes)) == (1, "update", "alice", ["name", "updated_at"])
    assert snapshot["updated_at"] == changes["updated_at"]["new"]


# ----- the edges: nothing matched, no table, no audit table yet, many rows --------------------


WAYS = {
    "update": (lambda q: q.filter(F("name") == "none").update(name="x"), 0),
    "update_one": (lambda q: q.filter(F("name") == "none").update_one(name="x"), None),
    "update_n": (lambda q: q.filter(F("name") == "none").update_n(3, name="x"), []),
    "delete_all": (lambda q: q.filter(F("name") == "none").delete_all(), 0),
}


@pytest.mark.parametrize("way", list(WAYS))
def test_a_write_that_matches_no_row_writes_no_audit_row_and_leaves_no_transaction(db, way):
    Item = make("pydantic", AuditLogMixin)
    Item(name="a").save(db=db)
    fn, nothing = WAYS[way]

    assert fn(bound(Item, db)) == nothing

    assert _after_creates(db, Item) == []
    assert db.adapter.in_transaction is False


@pytest.mark.parametrize("mixins", [(), (AuditLogMixin,), (TimestampMixin,)])
@pytest.mark.parametrize("way", list(WAYS))
def test_a_missing_table_reads_as_empty_and_stays_missing(db, way, mixins):
    Item = make("pydantic", *mixins)
    fn, nothing = WAYS[way]

    assert fn(Item.using(db)) == nothing

    assert not _exists(db, Item.__tablename__)
    assert not _exists(db, f"{Item.__tablename__}_audit")


@pytest.mark.parametrize("way", ["update", "update_one", "update_n"])
def test_no_fields_is_refused_on_a_class_that_stamps(db, way):
    Item = make("pydantic", TimestampMixin, AuditLogMixin)
    Item(name="a").save(db=db)
    q = bound(Item, db)

    with pytest.raises(ValueError, match="No fields to update"):
        {"update": q.update, "update_one": q.update_one, "update_n": lambda: q.update_n(2)}[way]()


@pytest.mark.parametrize("mixins", [(), (AuditLogMixin,)])
def test_update_n_of_zero_is_refused(db, mixins):
    Item = make("pydantic", *mixins)
    Item(name="a").save(db=db)

    with pytest.raises(ValueError, match="n must be >= 1"):
        bound(Item, db).update_n(0, name="x")

    assert _after_creates(db, Item) == []


def test_the_audit_table_is_made_inside_the_unit_when_it_does_not_exist(db):
    Item = make("pydantic", AuditLogMixin)
    db.insert_document(Item.__tablename__, {"name": "a", "count": 0})
    assert not _exists(db, f"{Item.__tablename__}_audit")

    with pytest.raises(LookupError):
        with db.transaction():
            bound(Item, db).update(count=1)
            raise LookupError("the caller changes its mind")
    assert not _exists(db, f"{Item.__tablename__}_audit")

    bound(Item, db).update(count=1)
    assert _audit(db, Item) == [
        (1, "update", "alice", {"count": {"old": 0, "new": 1}}, {"name": "a", "count": 1})
    ]


def test_two_thousand_rows_are_one_read_one_write_and_their_audit_rows(db):
    Item = make("pydantic", AuditLogMixin)
    db.insert_many(Item.__tablename__, [{"name": "r", "count": i} for i in range(2000)])

    q = bound(Item, db)
    count, sent = _sent(lambda: q.update(name="x", count=F("count") + 1))

    assert count == 2000
    kinds = [sql.split()[0] for sql, _ in sent]
    assert kinds == ["SELECT", "UPDATE", "CREATE"] + ["INSERT"] * 2000
    assert len(_audit(db, Item)) == 2000


def test_a_set_based_write_calls_no_hook(db):
    calls = []

    class Hooked(HooksMixin, AuditLogMixin, SQLerModel):
        __tablename__ = "sw_hooked"
        name: str = ""

        def before_save(self):
            calls.append("before_save")
            return True

        def before_delete(self):
            calls.append("before_delete")
            return True

    Hooked(name="a").save(db=db)
    Hooked(name="b").save(db=db)
    calls.clear()
    q = bound(Hooked, db)

    q.update(name="c")
    q.update_one(name="d")
    q.update_n(2, name="e")
    q.delete_all()

    assert calls == []
    assert [r[1] for r in _audit(db, Hooked)] == ["create", "create", "update", "update", "update", "update", "update", "delete", "delete"]


# ----- two writers: each row claimed once, each old value read before its claim ---------------


class Claim(AuditLogMixin, SQLerModel):
    __tablename__ = "sw_claims"
    name: str = ""
    owner: Optional[str] = None


def test_two_threads_claim_ten_rows_between_them_never_one_twice(tmp_path):
    path = str(tmp_path / "claims.db")
    first = SQLerDB.on_disk(path)
    for i in range(10):
        Claim(name=f"c{i}").save(db=first)
    second = SQLerDB.on_disk(path)
    start = threading.Barrier(2)
    claimed: dict[str, list[int]] = {"a": [], "b": []}

    def worker(name, db):
        start.wait()
        while True:
            got = Claim.using(db).filter(F("owner") == None).update_one(owner=name)  # noqa: E711
            if got is None:
                return
            claimed[name].append(got._id)

    threads = [threading.Thread(target=worker, args=(n, d)) for n, d in (("a", first), ("b", second))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(claimed["a"] + claimed["b"]) == list(range(1, 11))
    updates = [r for r in _audit(first, Claim) if r[1] == "update"]
    assert len(updates) == 10
    assert sorted(r[0] for r in updates) == list(range(1, 11))
    for record_id, _, _, changes, _ in updates:
        owner = "a" if record_id in claimed["a"] else "b"
        assert changes == {"owner": {"old": None, "new": owner}}
    first.close()
    second.close()


# ----- the rows a delete's policy changes meet their own class's rules ------------------------


class Shelf(AuditLogMixin, SQLerModel):
    __tablename__ = "sw_shelves"
    name: str


class Volume(AuditLogMixin, TimestampMixin, SQLerModel):
    __tablename__ = "sw_volumes"
    title: str
    shelf: Optional[Shelf] = None


class Page(SQLerModel):
    __tablename__ = "sw_pages"
    n: int
    volume: Optional[Volume] = None


class Note(AuditLogMixin, SQLerModel):
    __tablename__ = "sw_notes"
    text: str
    page: Optional[Page] = None


def test_set_null_writes_the_referrers_update_row_and_moves_its_updated_at(db):
    shelf = Shelf(name="s").save(db=db)
    shelf_id = shelf._id
    vol = Volume(title="v", shelf=shelf).save(db=db)
    stamped = _stored(db, "sw_volumes")[0][1]["updated_at"]

    shelf.delete_with_policy(db=db, on_delete="set_null")

    [(rid, action, user, changes, snapshot)] = _after_creates(db, Volume)
    doc = _stored(db, "sw_volumes")[0][1]
    assert (rid, action, user) == (vol._id, "update", "alice")
    assert changes["shelf"] == {"old": {"_table": "sw_shelves", "_id": shelf_id}, "new": None}
    assert sorted(changes) == ["shelf", "updated_at"]
    assert _iso(doc["updated_at"]) > _iso(stamped)
    assert snapshot == {k: v for k, v in doc.items() if k != "_version"}
    assert [r[1] for r in _audit(db, Shelf)] == ["create", "delete"]


class Binder(SQLerModel):
    __tablename__ = "sw_binders"
    name: str = ""


class Sheet(SQLerSafeModel):
    __tablename__ = "sw_sheets"
    title: str = ""
    binder: Optional[Binder] = None


@dataclass
class LiteBinder(SQLerLiteModel):
    __tablename__ = "sw_lite_binders"
    name: str = ""


@dataclass
class LiteSheet(SQLerLiteSafeModel):
    __tablename__ = "sw_lite_sheets"
    title: str = ""
    binder: Optional[LiteBinder] = None


@pytest.mark.parametrize("target, referrer", [(Binder, Sheet), (LiteBinder, LiteSheet)], ids=SAFE_KINDS)
def test_set_null_moves_the_version_of_a_versioned_referrer_so_a_stale_save_is_refused(db, target, referrer):
    binder = target(name="b").save(db=db)
    referrer(title="s", binder=binder).save(db=db)
    held = bound(referrer, db).first()
    table = referrer.__tablename__

    _, sent = _sent(lambda: binder.delete_with_policy(db=db, on_delete="set_null"))

    assert [sql for sql, _ in sent if table in sql and not sql.startswith("SELECT")] == [
        f"UPDATE {table} SET data = json_set(json(?), '$._version', _version + 1), "
        "_version = _version + 1 WHERE _id = ?;"
    ]
    assert _version_column(db, table) == [1]
    held.title = "edited by a holder who loaded it before the delete"
    with pytest.raises(StaleVersionError):
        held.save(db=db)
    [(_, doc)] = _stored(db, table)
    assert (doc["title"], doc["binder"]) == ("s", None)
    assert _version_column(db, table) == [1]
    after = bound(referrer, db).first()
    after.title = "edited by a holder who loaded it after the delete"
    after.save(db=db)
    [(_, doc)] = _stored(db, table)
    assert (doc["title"], doc["binder"]) == ("edited by a holder who loaded it after the delete", None)
    assert _version_column(db, table) == [2]


def test_a_cascade_three_levels_deep_logs_the_audited_level_only(db):
    shelf = Shelf(name="s").save(db=db)
    vol = Volume(title="v", shelf=shelf).save(db=db)
    page = Page(n=1, volume=vol).save(db=db)
    stored_vol = {k: v for k, v in _stored(db, "sw_volumes")[0][1].items()}

    with query_logger.capture() as got:
        shelf.delete_with_policy(db=db, on_delete="cascade")

    assert _stored(db, "sw_volumes") == [] and _stored(db, "sw_pages") == []
    assert _after_creates(db, Volume) == [(vol._id, "delete", "alice", None, stored_vol)]
    assert [r[1] for r in _audit(db, Shelf)] == ["create", "delete"]
    page_deletes = [sql for sql, *_ in ((e.sql,) for e in got) if sql.startswith("DELETE FROM sw_pages")]
    assert page_deletes == ["DELETE FROM sw_pages WHERE _id = ?;"]
    assert page._id == 1


def test_a_cascade_under_an_unaudited_target_logs_the_audited_grandchildren(db):
    vol = Volume(title="v").save(db=db)
    page = Page(n=1, volume=vol).save(db=db)
    note = Note(text="n", page=page).save(db=db)

    vol.delete_with_policy(db=db, on_delete="cascade")

    assert _after_creates(db, Note) == [
        (note._id, "delete", "alice", None, {"text": "n", "page": {"_table": "sw_pages", "_id": page._id}})
    ]


class Node(AuditLogMixin, SQLerModel):
    __tablename__ = "sw_nodes"
    name: str
    parent: Optional["Node"] = None


Node.model_rebuild()


def test_a_tree_cascade_logs_every_node_it_deletes(db):
    root = Node(name="root").save(db=db)
    child = Node(name="child", parent=root).save(db=db)
    Node(name="leaf", parent=child).save(db=db)

    root.delete_with_policy(db=db, on_delete="cascade")

    deletes = [(r[0], r[4]["name"]) for r in _audit(db, Node) if r[1] == "delete"]
    assert sorted(deletes) == [(1, "root"), (2, "child"), (3, "leaf")]
    assert _stored(db, "sw_nodes") == []


def test_a_referrer_with_no_registered_rules_gets_the_statements_it_got(db):
    vol = Volume(title="v").save(db=db)
    Page(n=1, volume=vol).save(db=db)

    with query_logger.capture() as got:
        vol.delete_with_policy(db=db, on_delete="set_null")

    page_sql = [e.sql for e in got if "sw_pages" in e.sql and not e.sql.startswith("SELECT")]
    assert page_sql == ["UPDATE sw_pages SET data = json(?) WHERE _id = ?;"]


# ----- an instance delete records the row as it was stored -----------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_an_instance_edited_and_not_saved_logs_the_stored_row_on_delete(db, kind):
    Item = make(kind, AuditLogMixin)
    inst = Item(name="stored", count=1).save(db=db)
    inst.name = "edited, never saved"

    inst.delete(db=db)

    assert _after_creates(db, Item) == [(1, "delete", "alice", None, {"name": "stored", "count": 1})]


@pytest.mark.parametrize("kind", KINDS)
def test_a_delete_of_a_row_already_gone_logs_nothing_and_raises_nothing(db, kind):
    Item = make(kind, AuditLogMixin)
    inst = Item(name="a", count=1).save(db=db)
    db.delete_document(Item.__tablename__, inst._id)

    inst.delete(db=db)

    assert _after_creates(db, Item) == []
    assert _stored(db, Item.__tablename__) == []
    assert db.adapter.in_transaction is False


class Ranked(AuditLogMixin, SQLerSafeModel):
    __tablename__ = "sw_ranked"
    __promoted__: ClassVar[dict[str, str]] = {"priority": "INTEGER"}
    name: str = ""
    priority: int = 0


def test_an_f_expression_on_a_promoted_field_logs_the_value_the_row_holds_after(db):
    for p in (5, 9):
        Ranked(name=f"p{p}", priority=p).save(db=db)

    got = bound(Ranked, db).order_by("priority", desc=True).update_n(1, priority=F("priority") - 4)

    assert [(g._id, g.priority, g._version) for g in got] == [(2, 5, 1)]
    assert db.adapter.execute("SELECT priority, _version FROM sw_ranked WHERE _id = 2;").fetchone()[:] == (5, 1)
    assert _after_creates(db, Ranked) == [
        (2, "update", "alice", {"priority": {"old": 9, "new": 5}}, {"name": "p9", "priority": 5})
    ]
