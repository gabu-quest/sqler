"""Every way of writing an async instance meets the rules of its class: ``save()``,
``save_many()``, ``delete()`` and ``delete_with_policy()`` stamp the same times, write the same
audit rows and await the same hooks, on every async base; and a delete with its policy is one
transaction."""

import itertools
import json
import re
import types
import warnings
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timedelta, timezone
from typing import ClassVar, Optional

import pytest
import pytest_asyncio

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
)
from sqler.exceptions import NotFoundError, StaleVersionError, UniqueConstraintError
from sqler.logging import query_logger
from sqler.models._table_names import _home_of
from sqler.models.mixins import (
    AsyncAuditLogMixin,
    AsyncAuditMixin,
    AsyncFullMixin,
    AsyncHooksMixin,
    AsyncSoftDeleteMixin,
    TimestampMixin,
)
from sqler.tracking import AsyncTrackedModel

pytestmark = pytest.mark.asyncio

BASES = {
    "pydantic": AsyncSQLerModel,
    "pydantic_safe": AsyncSQLerSafeModel,
    "lite": AsyncSQLerLiteModel,
    "lite_safe": AsyncSQLerLiteSafeModel,
}
KINDS = list(BASES)
SAFE = {"pydantic_safe", "lite_safe"}

_tables = itertools.count()

CALLS: list = []
PLAN: dict = {}


class Interrupt(BaseException):
    """Stands for an interrupt; asyncio keeps a plain ``BaseException`` in its task."""


class Recorded(AsyncHooksMixin):
    async def before_save(self) -> bool:
        CALLS.append(("before_save", self.name))
        return PLAN.get("veto") != self.name

    async def after_save(self) -> None:
        CALLS.append(("after_save", self.name))

    async def before_delete(self) -> bool:
        CALLS.append(("before_delete", self.name))
        return True

    async def after_delete(self) -> None:
        CALLS.append(("after_delete", self.name))


class Unhooked(Recorded):
    _hooks_enabled: ClassVar[bool] = False


def make(kind, *mixins, fields=None):
    """A model class of ``kind`` with the mixins, a ``name`` field, the ``fields``
    (``{name: (type, default)}``) and its own table."""
    table = f"awr_{kind}_{next(_tables)}"
    ns = {"__tablename__": table, "name": "", "__annotations__": {"name": str}}
    for field, (hint, default) in (fields or {}).items():
        ns["__annotations__"][field] = hint
        ns[field] = default
    cls = types.new_class(f"Item_{table}", (*mixins, BASES[kind]), {}, lambda body: body.update(ns))
    return dataclass(cls) if kind.startswith("lite") else cls


@pytest_asyncio.fixture
async def db():
    CALLS.clear()
    PLAN.clear()
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


async def _docs(db, cls):
    table = cls.__tablename__
    if not await _exists(db, table):
        return []
    return [
        (r[0], json.loads(r[1]))
        for r in await _rows(db, f"SELECT _id, data FROM {table} ORDER BY _id;")
    ]


async def _audit(db, cls):
    table = f"{cls.__tablename__}_audit"
    if not await _exists(db, table):
        return []
    return await _rows(
        db, f"SELECT record_id, action, user, changes, snapshot FROM {table} ORDER BY _id;"
    )


# ----- save_many is the name, and it stamps as save stamps -----------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_stamps_each_row_as_save_does(db, kind):
    Item = make(kind, TimestampMixin)
    await Item(name="by_save").save(db=db)
    a, b = Item(name="a"), Item(name="b")

    await Item.save_many([a, b], db=db)

    docs = await _docs(db, Item)
    assert len(docs) == 3
    shape = re.sub(r"\d", "9", docs[0][1]["created_at"])
    for inst, (_id, doc) in zip([a, b], docs[1:]):
        assert doc["created_at"] == doc["updated_at"]
        assert re.sub(r"\d", "9", doc["created_at"]) == shape
        assert inst.created_at == datetime.fromisoformat(doc["created_at"])
        assert inst.created_at.tzinfo is not None


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_sets_the_audit_users(db, kind):
    AsyncAuditMixin.set_current_user("alice")
    Item = make(kind, AsyncAuditMixin)

    await Item.save_many([Item(name="a")], db=db)

    doc = (await _docs(db, Item))[0][1]
    assert (doc["created_by"], doc["updated_by"]) == ("alice", "alice")


@pytest.mark.parametrize("kind", KINDS)
async def test_asave_many_warns_and_does_what_save_many_does(db, kind):
    Item = make(kind, TimestampMixin)
    batch = [Item(name="a"), Item(name="b")]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = await Item.asave_many(batch, db=db)

    assert out is batch
    assert [(str(w.message), w.category, w.filename) for w in caught] == [
        (
            f"{Item.__name__}.asave_many() is deprecated. Use save_many(); "
            "asave_many() will be removed in a future version.",
            DeprecationWarning,
            __file__,
        )
    ]
    docs = await _docs(db, Item)
    assert [(i, d["name"]) for i, d in docs] == [(a._id, a.name) for a in batch]
    assert docs[0][1]["created_at"] == docs[0][1]["updated_at"]
    if kind in SAFE:
        assert [i._version for i in batch] == [0, 0]


# ----- save_many writes its audit rows, as one unit ----------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_writes_the_audit_rows_a_loop_of_save_writes(db, kind):
    AsyncAuditMixin.set_current_user("alice")
    Batch, Loop = make(kind, AsyncAuditLogMixin), make(kind, AsyncAuditLogMixin)
    batch = [Batch(name="a"), Batch(name="b")]

    await Batch.save_many(batch, db=db)
    for name in ("a", "b"):
        await Loop(name=name).save(db=db)

    batch_rows, loop_rows = await _audit(db, Batch), await _audit(db, Loop)
    assert [r[0] for r in batch_rows] == [i._id for i in batch]
    assert [r[1:] for r in batch_rows] == [r[1:] for r in loop_rows]
    assert [(r[1], r[2]) for r in batch_rows] == [("create", "alice")] * 2


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_refused_in_a_later_chunk_leaves_no_row_and_no_audit_row(db, kind):
    Item = make(kind, AsyncAuditLogMixin)
    await Item(name="seed").save(db=db)
    await db.create_index(Item.__tablename__, "name", unique=True)
    batch = [Item(name=f"n{i}") for i in range(999)] + [Item(name="seed")]

    with pytest.raises(UniqueConstraintError):
        await Item.save_many(batch, db=db)

    assert [d["name"] for _, d in await _docs(db, Item)] == ["seed"]
    assert [r[1] for r in await _audit(db, Item)] == ["create"]
    assert {(i._id, i._snapshot) for i in batch} == {(None, None)}


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_follows_the_callers_transaction(db, kind):
    Item = make(kind, AsyncAuditLogMixin)
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await Item.save_many([Item(name="a")], db=db)
            raise RuntimeError("the caller changes its mind")
    assert await _docs(db, Item) == []

    async with db.transaction():
        await Item.save_many([Item(name="c"), Item(name="d")], db=db)
    assert [d["name"] for _, d in await _docs(db, Item)] == ["c", "d"]
    assert [r[1] for r in await _audit(db, Item)] == ["create", "create"]


@pytest.mark.parametrize("kind", KINDS)
async def test_an_interrupt_in_the_batch_puts_every_identity_back(db, kind):
    Item = make(kind, AsyncAuditLogMixin)
    await Item(name="kept").save(db=db)

    def interrupted():
        raise Interrupt

    AsyncAuditMixin.set_current_user_getter(interrupted)
    batch = [Item(name="a"), Item(name="b")]
    with pytest.raises(Interrupt):
        await Item.save_many(batch, db=db)

    assert {(i._id, i._snapshot) for i in batch} == {(None, None)}
    assert [d["name"] for _, d in await _docs(db, Item)] == ["kept"]
    assert [r[1] for r in await _audit(db, Item)] == ["create"]


# ----- save_many awaits the hooks -----------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_awaits_each_hook_in_order(db, kind):
    Item = make(kind, Recorded)

    await Item.save_many([Item(name="a"), Item(name="b")], db=db)

    assert CALLS == [
        ("before_save", "a"),
        ("before_save", "b"),
        ("after_save", "a"),
        ("after_save", "b"),
    ]


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_before_save_false_names_the_instance_and_changes_nothing(db, kind):
    Item = make(kind, Recorded, TimestampMixin)
    batch = [Item(name="a"), Item(name="b")]
    PLAN["veto"] = "b"

    with pytest.raises(RuntimeError, match="for instance 1"):
        await Item.save_many(batch, db=db)

    assert await _docs(db, Item) == []
    assert {(i._id, i.created_at) for i in batch} == {(None, None)}


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_with_hooks_disabled_calls_none(db, kind):
    Item = make(kind, Unhooked)

    await Item.save_many([Item(name="a")], db=db)

    assert CALLS == []


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_refuses_a_saved_instance_before_any_hook(db, kind):
    Item = make(kind, Recorded)
    saved = await Item(name="saved").save(db=db)
    CALLS.clear()

    with pytest.raises(ValueError, match=r"^save_many\(\) only accepts new instances"):
        await Item.save_many([Item(name="new"), saved], db=db)

    assert CALLS == []


@pytest.mark.parametrize("kind", KINDS)
async def test_save_many_of_nothing_sends_no_statement(db, kind):
    Item = make(kind, Recorded, AsyncAuditLogMixin)

    with query_logger.capture() as statements:
        assert await Item.save_many([], db=db) == []

    assert statements == []
    assert CALLS == []


async def test_full_mixin_hooks_before_the_stamp_and_after_the_commit(db):
    seen = []

    class Watching(AsyncHooksMixin):
        async def before_save(self) -> bool:
            seen.append(("before_save", self.created_at))
            return True

        async def after_save(self) -> None:
            seen.append(("after_save", db.adapter.in_transaction))

    Item = make("pydantic", Watching, AsyncFullMixin, AsyncAuditLogMixin)

    await Item(name="one").save(db=db)
    await Item.save_many([Item(name="two")], db=db)

    assert seen == [("before_save", None), ("after_save", False)] * 2
    assert [r[1] for r in await _audit(db, Item)] == ["create", "create"]


# ----- the ways built on save() meet the rules too ----------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_first_or_create_writes_the_audit_row_of_the_row_it_creates(db, kind):
    Item = make(kind, AsyncAuditLogMixin)

    made, created = await Item.first_or_create({"name": "only"}, db=db)
    found, created_again = await Item.first_or_create({"name": "only"}, db=db)

    assert (created, created_again) == (True, False)
    assert (made._id, found._id) == (1, 1)
    assert [(r[0], r[1]) for r in await _audit(db, Item)] == [(1, "create")]


async def test_soft_delete_and_restore_are_updates_with_their_audit_rows(db):
    Item = make("pydantic", AsyncFullMixin, AsyncAuditLogMixin)
    inst = await Item(name="kept").save(db=db)

    await inst.soft_delete(db=db)
    deleted_at = (await _docs(db, Item))[0][1]["deleted_at"]
    await inst.restore(db=db)

    assert deleted_at is not None
    assert (await _docs(db, Item))[0][1]["deleted_at"] is None
    audit = await _audit(db, Item)
    assert [(r[0], r[1]) for r in audit] == [(1, "create"), (1, "update"), (1, "update")]
    assert json.loads(audit[1][3])["deleted_at"]["new"] == deleted_at
    assert json.loads(audit[2][3])["deleted_at"]["new"] is None


# ----- delete_with_policy is the delete, whoever calls it ------------------------------------


@pytest.mark.parametrize("way", ["delete", "restrict", "set_null", "cascade"])
@pytest.mark.parametrize("kind", KINDS)
async def test_delete_with_policy_called_directly_is_the_delete(db, kind, way):
    Item = make(kind, Recorded, AsyncAuditLogMixin)
    inst = await Item(name="gone").save(db=db)
    row_id = inst._id
    CALLS.clear()

    if way == "delete":
        await inst.delete(db=db)
    else:
        await inst.delete_with_policy(db=db, on_delete=way)

    assert CALLS == [("before_delete", "gone"), ("after_delete", "gone")]
    assert [(r[0], r[1]) for r in await _audit(db, Item)] == [(row_id, "create"), (row_id, "delete")]
    assert await _docs(db, Item) == []
    assert inst._id is None


@pytest.mark.parametrize("kind", KINDS)
async def test_an_unknown_policy_raises_before_any_hook_or_audit_row(db, kind):
    Item = make(kind, Recorded, AsyncAuditLogMixin)
    inst = await Item(name="kept").save(db=db)
    CALLS.clear()

    with pytest.raises(ValueError, match="on_delete must be"):
        await inst.delete_with_policy(db=db, on_delete="nonsense")

    assert CALLS == []
    assert [r[1] for r in await _audit(db, Item)] == ["create"]


class AAuthor(AsyncSQLerModel):
    __tablename__ = "awr_authors"
    name: str


class ABook(AsyncSQLerModel):
    __tablename__ = "awr_books"
    title: str
    author: Optional[AAuthor] = None


class LoggedAuthor(AsyncAuditLogMixin, AsyncSQLerModel):
    __tablename__ = "awr_logged_authors"
    name: str


class LoggedBook(AsyncSQLerModel):
    __tablename__ = "awr_logged_books"
    title: str
    author: Optional[LoggedAuthor] = None


@pytest_asyncio.fixture
async def library(db):
    author = await AAuthor(name="a").save(db=db)
    await ABook(title="b1", author=author).save(db=db)
    await ABook(title="b2", author=author).save(db=db)
    return author


async def _state(db, authors="awr_authors", books="awr_books"):
    count = (await _rows(db, f"SELECT count(*) FROM {authors};"))[0][0]
    refs = await _rows(db, f"SELECT json_extract(data, '$.author._id') FROM {books} ORDER BY _id;")
    return count, [r[0] for r in refs]


async def _run(db, sql):
    cur = await db.adapter.execute(sql)
    await cur.close()
    await db.adapter.auto_commit()


@pytest.mark.parametrize("caught_inside", [False, True])
@pytest.mark.parametrize("policy", ["cascade", "set_null"])
@pytest.mark.parametrize("classes", [(AAuthor, ABook), (LoggedAuthor, LoggedBook)])
async def test_a_refused_target_delete_leaves_the_referrers_as_they_were(
    db, classes, policy, caught_inside
):
    author_cls, book_cls = classes
    author = await author_cls(name="a").save(db=db)
    await book_cls(title="b1", author=author).save(db=db)
    await book_cls(title="b2", author=author).save(db=db)
    authors, books = author_cls.__tablename__, book_cls.__tablename__
    await _run(
        db,
        f"CREATE TRIGGER awr_refuse BEFORE DELETE ON {authors} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END;",
    )

    async def refused():
        with pytest.raises(Exception, match="refused"):
            await author.delete_with_policy(db=db, on_delete=policy)

    if caught_inside:
        async with db.transaction():
            await refused()
    else:
        await refused()

    assert await _state(db, authors, books) == (1, [1, 1])
    assert author._id == 1
    if author_cls is LoggedAuthor:
        assert [r[1] for r in await _audit(db, LoggedAuthor)] == ["create"]


@pytest.mark.parametrize("policy", ["cascade", "set_null"])
async def test_a_delete_in_a_rolled_back_transaction_keeps_nothing(db, library, policy):
    with pytest.raises(RuntimeError, match="changes its mind"):
        async with db.transaction():
            await AAuthor(name="written before the delete").save(db=db)
            await library.delete_with_policy(db=db, on_delete=policy)
            raise RuntimeError("the caller changes its mind")

    assert await _state(db) == (1, [1, 1])


# ----- a write that fails half way leaves the rows and the instance as they were ----------


async def _refuse_audit_rows(db, Item):
    """The audit table takes no more rows: the write's second statement fails."""
    await _run(
        db,
        f"CREATE TRIGGER refuse_audit BEFORE INSERT ON {Item.__tablename__}_audit "
        "BEGIN SELECT RAISE(ABORT, 'audit refused'); END;",
    )


def _identity(inst):
    return (inst._id, inst._snapshot, getattr(inst, "_version", None))


@pytest.mark.parametrize("kind", KINDS)
async def test_a_delete_whose_audit_row_fails_keeps_the_row_and_can_be_done_again(db, kind):
    Item = make(kind, AsyncAuditLogMixin)
    inst = await Item(name="kept").save(db=db)
    before = _identity(inst)
    await _refuse_audit_rows(db, Item)

    with pytest.raises(Exception, match="audit refused"):
        await inst.delete(db=db)

    assert [(i, d["name"]) for i, d in await _docs(db, Item)] == [(1, "kept")]
    assert _identity(inst) == before
    assert before[0] == 1

    await _run(db, "DROP TRIGGER refuse_audit;")
    await inst.delete(db=db)

    assert await _docs(db, Item) == []
    assert [(r[0], r[1]) for r in await _audit(db, Item)] == [(1, "create"), (1, "delete")]
    assert inst._id is None


def _new(db, Item, seed):
    inst = Item(name="new")
    return [inst], lambda: inst.save(db=db)


def _changed(db, Item, seed):
    seed.name = "changed"
    return [seed], lambda: seed.save(db=db)


def _batch(db, Item, seed):
    batch = [Item(name="a"), Item(name="b")]
    return batch, lambda: Item.save_many(batch, db=db)


def _deleted(db, Item, seed):
    return [seed], lambda: seed.delete(db=db)


@pytest.mark.parametrize("write", [_new, _changed, _batch, _deleted])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_failed_write_the_caller_catches_and_commits_past_keeps_nothing(db, kind, write):
    Item = make(kind, AsyncAuditLogMixin)
    seed = await Item(name="seed").save(db=db)
    await _refuse_audit_rows(db, Item)
    touched, act = write(db, Item, seed)
    before = [_identity(inst) for inst in touched]

    async with db.transaction():
        with pytest.raises(Exception, match="audit refused"):
            await act()
        await db.insert_document("beside", {"name": "the caller's own row"})

    assert [(i, d["name"]) for i, d in await _docs(db, Item)] == [(1, "seed")]
    assert [r[1] for r in await _audit(db, Item)] == ["create"]
    assert [_identity(inst) for inst in touched] == before
    assert await db.find_document("beside", 1) == {"name": "the caller's own row", "_id": 1}


# ----- a class with no rule pays nothing --------------------------------------------------------

# What a class with no row rule sent before the rules had one owner, by the first word of
# each statement: a new row, a changed row, a batch of two, and a delete (the same for
# delete() and each policy). A delete with a policy that touches referrers also looks through
# the registered tables for them; those reads depend on what else is registered and are left
# out.
# a write by _id makes no table; a new row makes none once its table is remembered; a batch of
# several rows sends the check of the columns first (a ``SELECT``)
UNVERSIONED = (["INSERT"], ["UPDATE"], ["CREATE", "SELECT", "INSERT"])
VERSIONED = (["INSERT"], ["UPDATE"], ["SELECT", "INSERT"])
A_DELETE = ["CREATE", "DELETE"]


async def _sent(act):
    with query_logger.capture() as q:
        await act()
    return [
        e.sql.split()[0]
        for e in q
        if "sqlite_master" not in e.sql and "data LIKE" not in e.sql
    ]


@pytest.mark.parametrize("mixins", [(), (TimestampMixin,)], ids=["plain", "timestamps"])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_class_with_no_row_rule_sends_the_statements_it_sent(db, kind, mixins):
    Item = make(kind, *mixins)
    await Item(name="warm").save(db=db)
    inst = Item(name="a")
    sent = [await _sent(lambda: inst.save(db=db))]
    inst.name = "b"
    sent.append(await _sent(lambda: inst.save(db=db)))
    sent.append(await _sent(lambda: Item.save_many([Item(name="c"), Item(name="d")], db=db)))
    sent.append(await _sent(lambda: inst.delete(db=db)))
    for policy in ("restrict", "set_null", "cascade"):
        other = await Item(name=policy).save(db=db)
        sent.append(await _sent(lambda: other.delete_with_policy(db=db, on_delete=policy)))

    expected = VERSIONED if kind in SAFE else UNVERSIONED
    assert sent == [*expected, *[A_DELETE] * 4]


# ----- a save of a row that is not there ---------------------------------------------------------


def _refused(kind):
    """What a by-id save of a row that is not there raises: a safe class reads it as a row at
    another version, every other class as a row that is gone."""
    return StaleVersionError if kind in SAFE else NotFoundError


GONE = ["deleted", "never_there", "in_the_callers_transaction"]
MIXINS = {
    "none": (),
    "hooks_audit": (Recorded, AsyncAuditLogMixin),
    "timestamps": (TimestampMixin,),
}


@pytest.mark.parametrize("gone", GONE)
@pytest.mark.parametrize("mixins", list(MIXINS))
@pytest.mark.parametrize("kind", KINDS)
async def test_a_save_of_a_row_that_is_not_there_raises_and_keeps_the_instance(
    db, kind, mixins, gone
):
    Item = make(kind, *MIXINS[mixins])
    inst = await Item(name="first").save(db=db)
    if gone == "deleted":
        await _run(db, f"DELETE FROM {Item.__tablename__};")
    elif gone == "never_there":
        inst._id = 41
    inst.name = "second"
    before = (_identity(inst), _home_of(inst))
    audit_before = await _audit(db, Item)
    CALLS.clear()

    if gone == "in_the_callers_transaction":
        async with db.transaction():
            cur = await db.adapter.execute(f"DELETE FROM {Item.__tablename__};")
            await cur.close()
            with pytest.raises(_refused(kind)):
                await inst.save(db=db)
    else:
        with pytest.raises(_refused(kind)):
            await inst.save(db=db)

    kept = [(1, "first")] if gone == "never_there" else []
    assert [(i, d["name"]) for i, d in await _docs(db, Item)] == kept
    assert (_identity(inst), _home_of(inst)) == before
    assert inst.name == "second"
    assert await _audit(db, Item) == audit_before
    if mixins == "hooks_audit":
        assert CALLS == [("before_save", "second")]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_refused_save_the_caller_catches_and_commits_past_writes_nothing(db, kind):
    Item = make(kind, Recorded, AsyncAuditLogMixin)
    inst = await Item(name="first").save(db=db)
    await _run(db, f"DELETE FROM {Item.__tablename__};")
    inst.name = "second"
    async with db.transaction():
        with pytest.raises(_refused(kind)):
            await inst.save(db=db)
        await db.insert_document("beside", {"name": "the caller's own row"})
    assert await _docs(db, Item) == []
    assert [r[1] for r in await _audit(db, Item)] == ["create"]
    assert await db.find_document("beside", 1) == {"name": "the caller's own row", "_id": 1}


@pytest.mark.parametrize("kind", KINDS)
async def test_a_save_to_a_database_without_the_row_raises_and_the_home_stays(db, kind):
    Item = make(kind)
    inst = await Item(name="first").save(db=db)
    other = AsyncSQLerDB.in_memory(shared=False)
    await other.connect()
    try:
        inst.name = "second"
        with pytest.raises(_refused(kind)):
            await inst.save(db=other)
        assert await _docs(other, Item) == []
        assert await _exists(other, Item.__tablename__) is False
        await inst.save()
        assert [(i, d["name"]) for i, d in await _docs(db, Item)] == [(1, "second")]
        copy = await Item(name=inst.name).save(db=other)
        assert copy._id == 1
        assert [(i, d["name"]) for i, d in await _docs(other, Item)] == [(1, "second")]
    finally:
        await other.close()


async def test_the_refusal_names_the_table_the_id_and_how_to_copy_a_row(db):
    with pytest.raises(NotFoundError) as caught:
        await db.upsert_document("awr_named", 7, {"name": "x"})
    assert str(caught.value) == (
        "Row 7 of awr_named is not there: a write by _id does not create it; to put a copy "
        "into another database, save a new instance without the _id"
    )
    assert caught.value.id_ == 7
    assert await _rows(db, "SELECT name FROM sqlite_master WHERE name = 'awr_named';") == []


async def test_a_refused_update_sends_what_an_update_that_lands_sends(db):
    await db.insert_document("awr_counted", {"name": "x"})
    with query_logger.capture() as landed:
        await db.upsert_document("awr_counted", 1, {"name": "y"})
    with query_logger.capture() as refused:
        with pytest.raises(NotFoundError):
            await db.upsert_document("awr_counted", 9, {"name": "y"})
    assert [q.sql for q in refused] == [q.sql for q in landed]
    assert [q.sql.split()[0] for q in refused] == ["UPDATE"]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_refresh_of_a_row_that_is_gone_raises_lookup_error(db, kind):
    Item = make(kind)
    inst = await Item(name="first").save(db=db)
    await _run(db, f"DELETE FROM {Item.__tablename__};")
    with pytest.raises(LookupError, match="Row 1 not found for refresh"):
        await inst.refresh(db=db)
    assert inst._id == 1


# ----- a set_null write is the database's by-id write: the table as it is now ---------------------


class NulledAuthor(AsyncSQLerModel):
    __tablename__ = "awr_nulled_authors"
    name: str


class StampedBook(TimestampMixin, AsyncSQLerModel):
    __tablename__ = "awr_stamped_books"
    title: str
    author: Optional[NulledAuthor] = None


class SafeBook(AsyncSQLerSafeModel):
    __tablename__ = "awr_safe_books"
    title: str
    author: Optional[NulledAuthor] = None


def _raw(path, sql):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def test_a_set_null_after_another_object_promoted_the_stamp_writes_the_stamp_into_its_column(tmp_path, ticking_clock):
    from sqler import SQLerDB

    path = str(tmp_path / "stamped.db")
    db = AsyncSQLerDB.on_disk(path)
    await db.connect()
    try:
        author = await NulledAuthor(name="a").save(db=db)
        book = await StampedBook(title="b", author=author).save(db=db)
        await db.find_document("awr_stamped_books", book._id)
        other = SQLerDB.on_disk(path)
        other.ensure_table("awr_stamped_books", promoted={"updated_at": "TEXT"})
        other.close()
        [(old,)] = _raw(path, "SELECT updated_at FROM awr_stamped_books")
        await author.delete_with_policy(db=db, on_delete="set_null")
        read = (await StampedBook.from_id(book._id, db=db)).updated_at.isoformat()
    finally:
        await db.close()
    [(data, new)] = _raw(path, "SELECT data, updated_at FROM awr_stamped_books")
    assert json.loads(data) == {"created_at": book.created_at.isoformat(), "title": "b", "author": None}
    assert (new != old, read) == (True, new)


async def test_a_set_null_on_a_safe_table_another_object_remade_without_its_version_writes_the_row(tmp_path):
    import sqlite3

    path = str(tmp_path / "safe.db")
    db = AsyncSQLerDB.on_disk(path)
    await db.connect()
    try:
        author = await NulledAuthor(name="a").save(db=db)
        book = await SafeBook(title="b", author=author).save(db=db)
        await db.find_document_with_version("awr_safe_books", book._id)
        conn = sqlite3.connect(path)
        [(data,)] = conn.execute("SELECT data FROM awr_safe_books").fetchall()
        conn.executescript(
            "DROP TABLE awr_safe_books; CREATE TABLE awr_safe_books (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL);"
        )
        conn.execute("INSERT INTO awr_safe_books (_id, data) VALUES (1, ?)", [data])
        conn.commit()
        conn.close()
        await author.delete_with_policy(db=db, on_delete="set_null")
    finally:
        await db.close()
    # written as the table is now: a plain write adds no column (see the sync twin)
    assert _raw(path, "SELECT name FROM pragma_table_info('awr_safe_books')") == [("_id",), ("data",)]
    [(data,)] = _raw(path, "SELECT data FROM awr_safe_books")
    assert json.loads(data) == {"title": "b", "author": None}
    assert _raw(path, "SELECT count(*) FROM awr_nulled_authors") == [(0,)]


# ----- a loaded instance saves again, on every async base -----------------------------------

T_SET = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)

# (name, mixins, the fields the mixins give and the type each holds once set)
ROUND_TRIPS = [
    ("timestamp", (TimestampMixin,), {"created_at": datetime, "updated_at": datetime}),
    ("soft_delete", (AsyncSoftDeleteMixin,), {"deleted_at": datetime}),
    (
        "audit",
        (AsyncAuditMixin,),
        {"created_at": datetime, "updated_at": datetime, "created_by": str, "updated_by": str},
    ),
    ("hooks", (Recorded,), {}),
    ("tracking", (AsyncTrackedModel,), {}),
    ("full", (AsyncFullMixin,), {"created_at": datetime, "updated_at": datetime, "deleted_at": datetime}),
    ("audit_log", (AsyncAuditLogMixin,), {}),
]
ROUND_TRIP_CASES = [
    pytest.param(mixins, held, kind, id=f"{name}-{kind}")
    for name, mixins, held in ROUND_TRIPS
    for kind in KINDS
]


@pytest.mark.parametrize("mixins, held, kind", ROUND_TRIP_CASES)
async def test_a_loaded_instance_holds_its_mixin_fields_as_typed_and_saves_again(db, mixins, held, kind):
    AsyncAuditMixin.set_current_user("alice")
    Item = make(kind, *mixins)
    fresh = Item(name="a")
    if "deleted_at" in held:
        fresh.deleted_at = T_SET
    await fresh.save(db=db)

    loaded = await Item.from_id(fresh._id, db=db)
    assert {f: type(getattr(loaded, f)) for f in held} == held
    assert {f: getattr(loaded, f) for f in held} == {f: getattr(fresh, f) for f in held}

    loaded.name = "b"
    await loaded.save(db=db)

    again = await Item.from_id(fresh._id, db=db)
    assert again.name == "b"
    assert [doc["name"] for _, doc in await _docs(db, Item)] == ["b"]
    if "created_at" in held:
        assert again.created_at == fresh.created_at
        assert again.updated_at > fresh.updated_at
    if "deleted_at" in held:
        assert again.deleted_at == T_SET
    if "created_by" in held:
        assert (again.created_by, again.updated_by) == ("alice", "alice")


@pytest.mark.parametrize("kind", KINDS)
async def test_a_refresh_holds_the_row_as_a_load_does_and_saves_again(db, kind):
    Item = make(kind, TimestampMixin, fields={"when": (Optional[datetime], None)})
    mine = await Item(name="a", when=T_SET).save(db=db)
    other = await Item.from_id(mine._id, db=db)
    other.name = "b"
    await other.save(db=db)

    assert await mine.refresh(db=db) is mine

    held = ("name", "when", "created_at", "updated_at")
    assert (mine.name, mine.when) == ("b", T_SET)
    assert {f: getattr(mine, f) for f in held} == {f: getattr(other, f) for f in held}
    assert [type(getattr(mine, f)) for f in held] == [str, datetime, datetime, datetime]

    mine.name = "c"
    await mine.save(db=db)

    assert [doc["name"] for _, doc in await _docs(db, Item)] == ["c"]


@pytest.mark.parametrize("mixin", [TimestampMixin, AsyncAuditMixin])
@pytest.mark.parametrize("kind", KINDS)
async def test_save_with_a_naive_created_at_raises_and_writes_nothing(db, kind, mixin):
    Item = make(kind, mixin)
    naive = Item(name="naive")
    naive.created_at = datetime(2020, 1, 1)

    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        await naive.save(db=db)

    assert await _docs(db, Item) == []


# (field, its type, the value saved); each loads back as that value, of that type
TYPED_FIELDS = {
    "when": (Optional[datetime], T_SET),
    "day": (date, date(2026, 11, 14)),
    "wait": (timedelta, timedelta(minutes=90)),
    "steps": (tuple[date, ...], (date(2026, 1, 1), date(2026, 1, 2))),
    "stamps": (list[datetime], [T_SET, T_SET + timedelta(days=1)]),
}


@pytest.mark.parametrize("kind", KINDS)
async def test_a_field_loads_as_the_type_it_declares(db, kind):
    Item = make(kind, fields={name: (hint, None) for name, (hint, _) in TYPED_FIELDS.items()})
    saved = await Item(name="a", **{name: value for name, (_, value) in TYPED_FIELDS.items()}).save(db=db)

    loaded = await Item.from_id(saved._id, db=db)

    got = {name: getattr(loaded, name) for name in TYPED_FIELDS}
    want = {name: value for name, (_, value) in TYPED_FIELDS.items()}
    assert len(got) == 5
    assert got == want
    assert {n: type(v) for n, v in got.items()} == {n: type(v) for n, v in want.items()}
    assert loaded.is_dirty() is False


# ----- a field holding a plain dataclass: stored as its fields, loaded as its type ----------


@dataclass
class Address:
    city: str = ""


@dataclass
class Home:
    address: Optional[Address] = None
    since: Optional[date] = None


# (row, field type, value saved, the document's value for it); each loads back as that value
NESTED = [
    ("dataclass_in_dataclass", Optional[Home], Home(Address(city="x"), date(2026, 1, 2)),
     {"address": {"city": "x"}, "since": "2026-01-02"}),
    ("optional_none", Optional[Address], None, None),
    ("list_two", list[Address], [Address(city="x"), Address(city="y")], [{"city": "x"}, {"city": "y"}]),
    ("dict", dict[str, Address], {"k": Address(city="x")}, {"k": {"city": "x"}}),
]
NESTED_CASES = [
    pytest.param(hint, value, doc, kind, id=f"{row}-{kind}") for row, hint, value, doc in NESTED for kind in KINDS
]


@pytest.mark.parametrize("hint, value, doc, kind", NESTED_CASES)
async def test_a_dataclass_field_is_stored_as_its_fields_and_loads_as_its_type(db, hint, value, doc, kind):
    Item = make(kind, fields={"home": (hint, None)})
    saved = await Item(name="a", home=value).save(db=db)

    assert await _docs(db, Item) == [(saved._id, {"name": "a", "home": doc})]
    loaded = await Item.from_id(saved._id, db=db)
    assert loaded.home == value
    assert type(loaded.home) is type(value)
    assert loaded.is_dirty() is False


@pytest.mark.parametrize("kind", KINDS)
async def test_a_reference_stays_a_reference_beside_a_dataclass_field(db, kind):
    Owner = make(kind)
    Item = make(kind, fields={"owner": (Optional[Owner], None), "home": (Optional[Address], None)})
    owner = await Owner(name="o").save(db=db)
    saved = await Item(name="a", owner=owner, home=Address(city="x")).save(db=db)

    assert await _docs(db, Item) == [
        (saved._id, {"name": "a", "owner": {"_table": Owner.__tablename__, "_id": owner._id}, "home": {"city": "x"}})
    ]
    loaded = await Item.from_id(saved._id, db=db)
    assert (type(loaded.owner), loaded.owner._id, loaded.owner.name) == (Owner, owner._id, "o")
    assert loaded.home == Address(city="x")


@pytest.mark.parametrize("kind", ["lite", "lite_safe"])
async def test_a_lite_dataclass_field_loads_by_a_query_and_a_refresh(db, kind):
    Item = make(kind, fields={"home": (Optional[Home], None)})
    saved = await Item(name="a", home=Home(Address(city="x"))).save(db=db)

    [queried] = await Item.using(db).all()
    assert queried.home == Home(Address(city="x"))
    other = await Item.from_id(saved._id, db=db)
    other.home = Home(Address(city="y"), date(2026, 1, 2))
    await other.save(db=db)

    assert (await saved.refresh(db=db)).home == Home(Address(city="y"), date(2026, 1, 2))
    assert type(saved.home.since) is date


# ----- a model inside a dataclass value: refused, never a reference nothing loads ----------


def _holders(Owner):
    """A dataclass holding ``Owner`` directly, in a list, a tuple, a dict and a dataclass."""

    @dataclass
    class Inner:
        owner: Optional[Owner] = None

    @dataclass
    class Holder:
        city: str = ""
        owner: Optional[Owner] = None
        owners: list = dataclass_field(default_factory=list)
        pair: tuple = ()
        by: dict = dataclass_field(default_factory=dict)
        inner: Optional[Inner] = None

    return Holder, Inner


# (row, the Holder's arguments given the saved owner, where the owner sits)
MODEL_INSIDE = [
    ("direct", lambda Inner, o: {"owner": o}, "home.owner"),
    ("in_a_list", lambda Inner, o: {"owners": ["x", o]}, r"home.owners\[1\]"),
    ("in_a_tuple", lambda Inner, o: {"pair": ("x", o)}, r"home.pair\[1\]"),
    ("in_a_dict", lambda Inner, o: {"by": {"k": o}}, "home.by.k"),
    ("in_a_dataclass", lambda Inner, o: {"inner": Inner(owner=o)}, "home.inner.owner"),
]


@pytest.mark.parametrize("args, place", [pytest.param(a, p, id=r) for r, a, p in MODEL_INSIDE])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_model_inside_a_dataclass_value_is_refused_at_its_place_and_nothing_is_written(
    db, kind, args, place
):
    from sqler.exceptions import UnstorableValueError

    Owner = make(kind)
    Holder, Inner = _holders(Owner)
    Item = make(kind, fields={"home": (Optional[Holder], None)})
    owner = await Owner(name="o").save(db=db)
    item = Item(name="a", home=Holder(city="x", **args(Inner, owner)))

    with pytest.raises(
        UnstorableValueError,
        match=rf"^model {Owner.__name__} inside a dataclass value is not stored as a reference: "
        rf"make it a field of the model \(at {place}\)$",
    ):
        await item.save(db=db)
    assert await _docs(db, Item) == []
    assert item._id is None


@pytest.mark.parametrize("kind", KINDS)
async def test_a_model_in_a_list_and_a_dict_field_of_the_model_stays_a_reference_beside_a_dataclass(db, kind):
    Owner = make(kind)
    Holder, _ = _holders(Owner)
    Item = make(kind, fields={
        "owners": (list[Owner], None), "by": (dict[str, Owner], None), "home": (Optional[Holder], None)
    })
    owner = await Owner(name="o").save(db=db)
    saved = await Item(name="a", owners=[owner], by={"k": owner}, home=Holder(city="x")).save(db=db)

    ref = {"_table": Owner.__tablename__, "_id": owner._id}
    home = {"city": "x", "owner": None, "owners": [], "pair": [], "by": {}, "inner": None}
    assert await _docs(db, Item) == [(saved._id, {"name": "a", "owners": [ref], "by": {"k": ref}, "home": home})]
    loaded = await Item.from_id(saved._id, db=db)
    assert [(type(o), o._id) for o in [*loaded.owners, *loaded.by.values()]] == [(Owner, owner._id)] * 2
