"""Every way of writing an instance meets the rules of its class: ``save()``, ``save_many()``,
``delete()`` and ``delete_with_policy()`` stamp the same times, write the same audit rows and
call the same hooks, on every sync base; and a delete with its policy is one transaction."""

import itertools
import json
import re
import threading
import types
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import PurePosixPath
from typing import ClassVar, Optional, Union
from uuid import UUID

import msgspec
import pytest

from sqler import (
    ReferentialIntegrityError,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
)
from sqler.exceptions import NotFoundError, StaleVersionError, UniqueConstraintError
from sqler.logging import query_logger
from sqler.models._table_names import _home_of
from sqler.models.mixins import (
    AuditLogMixin,
    AuditMixin,
    FullMixin,
    HooksMixin,
    SoftDeleteMixin,
    TimestampMixin,
)
from sqler.tracking import TrackedModel

BASES = {
    "pydantic": SQLerModel,
    "pydantic_safe": SQLerSafeModel,
    "lite": SQLerLiteModel,
    "lite_safe": SQLerLiteSafeModel,
    "msgspec": SQLerMsgspecModel,
}
KINDS = list(BASES)
SAFE = {"pydantic_safe", "lite_safe"}

_tables = itertools.count()

CALLS: list = []
PLAN: dict = {}


class Recorded(HooksMixin):
    """Hooks that note each call; ``PLAN`` names the instance whose hook refuses or raises."""

    def before_save(self) -> bool:
        CALLS.append(("before_save", self.name))
        if PLAN.get("raise_before") == self.name:
            raise LookupError("before_save raised")
        return PLAN.get("veto") != self.name

    def after_save(self) -> None:
        CALLS.append(("after_save", self.name))
        if PLAN.get("raise_after") == self.name:
            raise LookupError("after_save raised")

    def before_delete(self) -> bool:
        CALLS.append(("before_delete", self.name))
        return True

    def after_delete(self) -> None:
        CALLS.append(("after_delete", self.name))


class Unhooked(Recorded):
    _hooks_enabled: ClassVar[bool] = False


def make(kind, *mixins, promoted=None, fields=None):
    """A model class of ``kind`` with the mixins, a ``name`` field, the ``fields``
    (``{name: (type, default)}``) and its own table."""
    table = f"wr_{kind}_{next(_tables)}"
    annotations = {"name": str}
    ns = {"__tablename__": table, "name": ""}
    for field, (hint, default) in (fields or {}).items():
        annotations[field] = hint
        ns[field] = default
    if promoted:
        annotations["status"] = Optional[str]
        ns["status"] = None
        ns["__promoted__"] = promoted
    ns["__annotations__"] = annotations
    kwds = {"tag": False} if kind == "msgspec" else {}
    cls = types.new_class(
        f"Item_{table}", (*mixins, BASES[kind]), kwds, lambda body: body.update(ns)
    )
    return dataclass(cls) if kind.startswith("lite") else cls


@pytest.fixture
def db():
    CALLS.clear()
    PLAN.clear()
    AuditMixin.set_current_user("alice")
    db = SQLerDB.in_memory(shared=False)
    yield db
    db.close()
    AuditMixin.set_current_user(None)
    AuditMixin._current_user_getter = None


def _exists(db, table):
    return (
        db.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table]
        ).fetchone()
        is not None
    )


def _docs(db, cls):
    table = cls.__tablename__
    if not _exists(db, table):
        return []
    return [
        (row[0], json.loads(row[1]))
        for row in db.adapter.execute(f"SELECT _id, data FROM {table} ORDER BY _id;").fetchall()
    ]


def _audit(db, cls):
    table = f"{cls.__tablename__}_audit"
    if not _exists(db, table):
        return []
    return db.adapter.execute(
        f"SELECT record_id, action, user, changes, snapshot FROM {table} ORDER BY _id;"
    ).fetchall()


def _shape(text):
    return re.sub(r"\d", "9", text)


# ----- save_many stamps as save stamps --------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_stamps_each_row_as_save_does(db, kind):
    Item = make(kind, TimestampMixin)
    Item(name="by_save").save(db=db)
    a, b = Item(name="a"), Item(name="b")

    Item.save_many([a, b], db=db)

    docs = _docs(db, Item)
    assert len(docs) == 3
    by_save = docs[0][1]
    for inst, (_id, doc) in zip([a, b], docs[1:]):
        assert doc["created_at"] is not None
        assert doc["created_at"] == doc["updated_at"]
        assert _shape(doc["created_at"]) == _shape(by_save["created_at"])
        assert inst.created_at == datetime.fromisoformat(doc["created_at"])
        assert inst.updated_at == inst.created_at
        assert inst.created_at.tzinfo is not None


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_keeps_a_created_at_the_caller_set(db, kind):
    Item = make(kind, TimestampMixin)
    set_by_caller = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    inst = Item(name="a")
    inst.created_at = set_by_caller

    Item.save_many([inst], db=db)

    doc = _docs(db, Item)[0][1]
    assert doc["created_at"] == "2020-01-02T03:04:05+00:00"
    assert inst.created_at == set_by_caller
    assert inst.updated_at > set_by_caller


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_sets_the_audit_users(db, kind):
    Item = make(kind, AuditMixin)
    a, b = Item(name="a"), Item(name="b")

    Item.save_many([a, b], db=db)

    docs = _docs(db, Item)
    assert [(d["created_by"], d["updated_by"]) for _, d in docs] == [("alice", "alice")] * 2
    assert [(i.created_by, i.updated_by) for i in (a, b)] == [("alice", "alice")] * 2
    assert docs[0][1]["created_at"] == docs[0][1]["updated_at"]


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_naive_created_at_raises_and_writes_nothing(db, kind):
    Item = make(kind, TimestampMixin)
    naive = Item(name="naive")
    naive.created_at = datetime(2020, 1, 1)

    with pytest.raises(TypeError):
        Item.save_many([Item(name="a"), naive, Item(name="c")], db=db)

    assert _docs(db, Item) == []


# ----- save_many writes its audit rows, as one unit ---------------------------------------


@pytest.mark.parametrize("promoted", [None, {"status": "TEXT"}], ids=["json", "promoted"])
@pytest.mark.parametrize("kind", KINDS)
def test_save_many_writes_the_audit_rows_a_loop_of_save_writes(db, kind, promoted):
    Batch = make(kind, AuditLogMixin, promoted=promoted)
    Loop = make(kind, AuditLogMixin, promoted=promoted)
    batch = [Batch(name="a"), Batch(name="b")]
    loop = [Loop(name="a"), Loop(name="b")]
    if promoted:
        batch[0].status = loop[0].status = "open"

    Batch.save_many(batch, db=db)
    for inst in loop:
        inst.save(db=db)

    batch_rows, loop_rows = _audit(db, Batch), _audit(db, Loop)
    assert len(batch_rows) == 2
    assert [r[0] for r in batch_rows] == [i._id for i in batch]
    assert [r[1:] for r in batch_rows] == [r[1:] for r in loop_rows]
    assert [r[1] for r in batch_rows] == ["create", "create"]
    assert [r[2] for r in batch_rows] == ["alice", "alice"]
    if kind in SAFE:
        assert [i._version for i in batch] == [0, 0]


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_refused_in_a_later_chunk_leaves_no_row_and_no_audit_row(db, kind):
    Item = make(kind, AuditLogMixin)
    Item(name="seed").save(db=db)
    db.create_index(Item.__tablename__, "name", unique=True)
    # 999 rows fill the first INSERT; the 1000th, a second "seed", is refused in the next
    batch = [Item(name=f"n{i}") for i in range(999)] + [Item(name="seed")]

    with pytest.raises(UniqueConstraintError):
        Item.save_many(batch, db=db)

    assert [d["name"] for _, d in _docs(db, Item)] == ["seed"]
    assert [r[1] for r in _audit(db, Item)] == ["create"]
    assert {(i._id, i._snapshot) for i in batch} == {(None, None)}
    if kind in SAFE:
        assert {i._version for i in batch} == {0}


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_follows_the_callers_transaction(db, kind):
    Item = make(kind, AuditLogMixin)
    with pytest.raises(RuntimeError):
        with db.transaction():
            Item.save_many([Item(name="a"), Item(name="b")], db=db)
            raise RuntimeError("the caller changes its mind")
    assert _docs(db, Item) == []
    assert _audit(db, Item) == []

    with db.transaction():
        Item.save_many([Item(name="c"), Item(name="d")], db=db)
    assert [d["name"] for _, d in _docs(db, Item)] == ["c", "d"]
    assert [r[1] for r in _audit(db, Item)] == ["create", "create"]


@pytest.mark.parametrize("kind", KINDS)
def test_an_interrupt_in_the_batch_puts_every_identity_back(db, kind):
    Item = make(kind, AuditLogMixin)
    kept = Item(name="kept").save(db=db)
    kept_snapshot = kept._snapshot

    def interrupted():
        raise KeyboardInterrupt

    AuditMixin.set_current_user_getter(interrupted)
    batch = [Item(name="a"), Item(name="b")]
    with pytest.raises(KeyboardInterrupt):
        Item.save_many(batch, db=db)
    kept.name = "changed"
    with pytest.raises(KeyboardInterrupt):
        kept.save(db=db)

    assert {(i._id, i._snapshot) for i in batch} == {(None, None)}
    assert (kept._id, kept._snapshot) == (1, kept_snapshot)
    assert [d["name"] for _, d in _docs(db, Item)] == ["kept"]
    assert [r[1] for r in _audit(db, Item)] == ["create"]


# ----- save_many calls the hooks ------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_calls_each_hook_in_order(db, kind):
    Item = make(kind, Recorded)

    Item.save_many([Item(name="a"), Item(name="b")], db=db)

    assert CALLS == [
        ("before_save", "a"),
        ("before_save", "b"),
        ("after_save", "a"),
        ("after_save", "b"),
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_before_save_false_names_the_instance_and_changes_nothing(db, kind):
    Item = make(kind, Recorded, TimestampMixin)
    batch = [Item(name="a"), Item(name="b"), Item(name="c")]
    PLAN["veto"] = "b"

    with pytest.raises(RuntimeError, match="for instance 1"):
        Item.save_many(batch, db=db)

    assert CALLS == [("before_save", "a"), ("before_save", "b")]
    assert _docs(db, Item) == []
    assert {(i._id, i.created_at, i.updated_at) for i in batch} == {(None, None, None)}


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_before_save_raising_writes_nothing(db, kind):
    Item = make(kind, Recorded)
    PLAN["raise_before"] = "b"

    with pytest.raises(LookupError, match="before_save raised"):
        Item.save_many([Item(name="a"), Item(name="b")], db=db)

    assert _docs(db, Item) == []


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_with_hooks_disabled_calls_none(db, kind):
    Item = make(kind, Unhooked)

    Item.save_many([Item(name="a")], db=db)

    assert CALLS == []
    assert [d["name"] for _, d in _docs(db, Item)] == ["a"]


@pytest.mark.parametrize("kind", KINDS)
def test_after_save_raising_leaves_the_rows_written_as_save_does(db, kind):
    Item = make(kind, Recorded)
    PLAN["raise_after"] = "x"
    single = Item(name="x")
    with pytest.raises(LookupError, match="after_save raised"):
        single.save(db=db)
    assert single._id == 1
    CALLS.clear()
    batch = [Item(name="p"), Item(name="x"), Item(name="q")]

    with pytest.raises(LookupError, match="after_save raised"):
        Item.save_many(batch, db=db)

    assert [d["name"] for _, d in _docs(db, Item)] == ["x", "p", "x", "q"]
    assert [i._id for i in batch] == [2, 3, 4]
    assert CALLS[3:] == [("after_save", "p"), ("after_save", "x")]


def test_after_save_runs_once_the_write_is_committed(db):
    seen = []

    class Watching(HooksMixin):
        def before_save(self) -> bool:
            seen.append(("before_save", self.created_at))
            return True

        def after_save(self) -> None:
            seen.append(("after_save", db.adapter.in_transaction))

    Item = make("pydantic", Watching, FullMixin, AuditLogMixin)

    Item(name="one").save(db=db)
    Item.save_many([Item(name="two")], db=db)

    assert seen == [("before_save", None), ("after_save", False)] * 2


def test_before_save_false_leaves_the_instance_unstamped(db):
    Item = make("pydantic", Recorded, FullMixin)
    inst = Item(name="v")
    PLAN["veto"] = "v"

    with pytest.raises(RuntimeError, match="before_save"):
        inst.save(db=db)

    assert (inst.created_at, inst.updated_at) == (None, None)


# ----- hostile inputs -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_of_nothing_sends_no_statement(db, kind):
    Item = make(kind, Recorded, AuditLogMixin)

    with query_logger.capture() as statements:
        assert Item.save_many([], db=db) == []

    assert statements == []
    assert CALLS == []


@pytest.mark.parametrize("kind", KINDS)
def test_save_many_refuses_a_saved_instance_before_any_hook(db, kind):
    Item = make(kind, Recorded)
    saved = Item(name="saved").save(db=db)
    CALLS.clear()

    with pytest.raises(ValueError, match="only accepts new instances"):
        Item.save_many([Item(name="new"), saved], db=db)

    assert CALLS == []
    assert [d["name"] for _, d in _docs(db, Item)] == ["saved"]


@pytest.mark.parametrize(
    "mixins",
    [(Recorded, AuditLogMixin), (AuditLogMixin, Recorded), (Recorded, FullMixin, AuditLogMixin)],
    ids=["hooks_then_audit", "audit_then_hooks", "full_mixin"],
)
@pytest.mark.parametrize("kind", KINDS)
def test_hooks_and_audit_log_meet_on_every_way(db, kind, mixins):
    Item = make(kind, *mixins)

    a, b = Item.save_many([Item(name="a"), Item(name="b")], db=db)
    a.name = "a2"
    a.save(db=db)
    b.delete_with_policy(db=db, on_delete="cascade")
    a.delete(db=db)

    assert [r[1] for r in _audit(db, Item)] == ["create", "create", "update", "delete", "delete"]
    assert CALLS == [
        ("before_save", "a"),
        ("before_save", "b"),
        ("after_save", "a"),
        ("after_save", "b"),
        ("before_save", "a2"),
        ("after_save", "a2"),
        ("before_delete", "b"),
        ("after_delete", "b"),
        ("before_delete", "a2"),
        ("after_delete", "a2"),
    ]
    assert _docs(db, Item) == []


def test_a_subclass_that_overrides_save_stamps_once_and_logs_once(db):
    Base = make("pydantic", TimestampMixin, AuditLogMixin)

    class Own(Base):
        def save(self, *, db=None, _partial: bool = False):
            self.name = self.name.upper()
            return super().save(db=db, _partial=_partial)

    inst = Own(name="own").save(db=db)

    assert inst.created_at == inst.updated_at
    assert [(r[1], json.loads(r[4])["name"]) for r in _audit(db, Own)] == [("create", "OWN")]


# ----- the ways built on save() meet the rules too ----------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_first_or_create_writes_the_audit_row_of_the_row_it_creates(db, kind):
    Item = make(kind, AuditLogMixin)

    made, created = Item.first_or_create({"name": "only"}, db=db)
    found, created_again = Item.first_or_create({"name": "only"}, db=db)

    assert (created, created_again) == (True, False)
    assert (made._id, found._id) == (1, 1)
    assert [(r[0], r[1]) for r in _audit(db, Item)] == [(1, "create")]


def test_soft_delete_and_restore_are_updates_with_their_audit_rows(db):
    Item = make("pydantic", FullMixin, AuditLogMixin)
    inst = Item(name="kept").save(db=db)

    inst.soft_delete(db=db)
    deleted_at = _docs(db, Item)[0][1]["deleted_at"]
    inst.restore(db=db)

    assert deleted_at is not None
    assert _docs(db, Item)[0][1]["deleted_at"] is None
    audit = _audit(db, Item)
    assert [(r[0], r[1]) for r in audit] == [(1, "create"), (1, "update"), (1, "update")]
    assert json.loads(audit[1][3])["deleted_at"]["new"] == deleted_at
    assert json.loads(audit[2][3])["deleted_at"]["new"] is None


# ----- delete_with_policy is the delete, whoever calls it -----------------------------------


@pytest.mark.parametrize("way", ["delete", "restrict", "set_null", "cascade"])
@pytest.mark.parametrize("kind", KINDS)
def test_delete_with_policy_called_directly_is_the_delete(db, kind, way):
    Item = make(kind, Recorded, AuditLogMixin)
    inst = Item(name="gone").save(db=db)
    row_id = inst._id
    CALLS.clear()

    if way == "delete":
        inst.delete(db=db)
    else:
        inst.delete_with_policy(db=db, on_delete=way)

    assert CALLS == [("before_delete", "gone"), ("after_delete", "gone")]
    audit = _audit(db, Item)
    assert [(r[0], r[1]) for r in audit] == [(row_id, "create"), (row_id, "delete")]
    assert json.loads(audit[1][4])["name"] == "gone"
    assert _docs(db, Item) == []
    assert inst._id is None


@pytest.mark.parametrize("kind", KINDS)
def test_an_unknown_policy_raises_before_any_hook_or_audit_row(db, kind):
    Item = make(kind, Recorded, AuditLogMixin)
    inst = Item(name="kept").save(db=db)
    CALLS.clear()

    with pytest.raises(ValueError, match="on_delete must be"):
        inst.delete_with_policy(db=db, on_delete="nonsense")

    assert CALLS == []
    assert [r[1] for r in _audit(db, Item)] == ["create"]
    assert [d["name"] for _, d in _docs(db, Item)] == ["kept"]


class Author(Recorded, AuditLogMixin, SQLerModel):
    __tablename__ = "wr_authors"
    name: str


class Book(SQLerModel):
    __tablename__ = "wr_books"
    title: str
    author: Optional[Author] = None


@pytest.fixture
def library(db):
    author = Author(name="a").save(db=db)
    Book(title="b1", author=author).save(db=db)
    Book(title="b2", author=author).save(db=db)
    CALLS.clear()
    return author


def _book_author_ids(db):
    return [
        r[0]
        for r in db.adapter.execute(
            "SELECT json_extract(data, '$.author._id') FROM wr_books ORDER BY _id;"
        ).fetchall()
    ]


def _author_count(db):
    return db.adapter.execute("SELECT count(*) FROM wr_authors;").fetchone()[0]


@pytest.mark.parametrize("way", ["delete", "delete_with_policy"])
def test_restrict_with_a_referrer_raises_after_before_delete_and_writes_no_audit_row(
    db, library, way
):
    with pytest.raises(ReferentialIntegrityError):
        getattr(library, way)(db=db)

    assert CALLS == [("before_delete", "a")]
    assert [r[1] for r in _audit(db, Author)] == ["create"]
    assert _author_count(db) == 1


@pytest.mark.parametrize("policy", ["cascade", "set_null"])
def test_a_delete_in_a_rolled_back_transaction_commits_nothing_the_caller_wrote(
    db, library, policy
):
    with pytest.raises(RuntimeError, match="changes its mind"):
        with db.transaction():
            Author(name="written before the delete").save(db=db)
            library.delete_with_policy(db=db, on_delete=policy)
            raise RuntimeError("the caller changes its mind")

    assert _author_count(db) == 1
    assert _book_author_ids(db) == [1, 1]
    assert [r[1] for r in _audit(db, Author)] == ["create"]


@pytest.mark.parametrize("policy,after", [("cascade", []), ("set_null", [None, None])])
def test_a_delete_with_policy_does_what_it_says(db, library, policy, after):
    library.delete_with_policy(db=db, on_delete=policy)

    assert _author_count(db) == 0
    assert _book_author_ids(db) == after


# ----- a write that fails half way leaves the rows and the instance as they were ----------


def _refuse_audit_rows(db, Item):
    """The audit table takes no more rows: the write's second statement fails."""
    db.adapter.execute(
        f"CREATE TRIGGER refuse_audit BEFORE INSERT ON {Item.__tablename__}_audit "
        "BEGIN SELECT RAISE(ABORT, 'audit refused'); END;"
    )
    db.adapter.auto_commit()


def _allow_audit_rows(db):
    db.adapter.execute("DROP TRIGGER refuse_audit;")
    db.adapter.auto_commit()


def _identity(inst):
    return (inst._id, inst._snapshot, getattr(inst, "_version", None))


@pytest.mark.parametrize("kind", KINDS)
def test_a_delete_whose_audit_row_fails_keeps_the_row_and_can_be_done_again(db, kind):
    Item = make(kind, AuditLogMixin)
    inst = Item(name="kept").save(db=db)
    before = _identity(inst)
    _refuse_audit_rows(db, Item)

    with pytest.raises(Exception, match="audit refused"):
        inst.delete(db=db)

    assert [(i, d["name"]) for i, d in _docs(db, Item)] == [(1, "kept")]
    assert _identity(inst) == before
    assert before[0] == 1

    _allow_audit_rows(db)
    inst.delete(db=db)

    assert _docs(db, Item) == []
    assert [(r[0], r[1]) for r in _audit(db, Item)] == [(1, "create"), (1, "delete")]
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
def test_a_failed_write_the_caller_catches_and_commits_past_keeps_nothing(db, kind, write):
    Item = make(kind, AuditLogMixin)
    seed = Item(name="seed").save(db=db)
    _refuse_audit_rows(db, Item)
    touched, act = write(db, Item, seed)
    before = [_identity(inst) for inst in touched]

    with db.transaction():
        with pytest.raises(Exception, match="audit refused"):
            act()
        db.insert_document("beside", {"name": "the caller's own row"})

    assert [(i, d["name"]) for i, d in _docs(db, Item)] == [(1, "seed")]
    assert [r[1] for r in _audit(db, Item)] == ["create"]
    assert [_identity(inst) for inst in touched] == before
    assert db.find_document("beside", 1) == {"name": "the caller's own row", "_id": 1}


class PlainAuthor(SQLerModel):
    __tablename__ = "wr_plain_authors"
    name: str


class PlainBook(SQLerModel):
    __tablename__ = "wr_plain_books"
    title: str
    author: Optional[PlainAuthor] = None


@pytest.mark.parametrize("caught_inside", [False, True])
@pytest.mark.parametrize("policy", ["cascade", "set_null"])
@pytest.mark.parametrize("classes", [(Author, Book), (PlainAuthor, PlainBook)])
def test_a_refused_target_delete_leaves_the_referrers_as_they_were(
    db, classes, policy, caught_inside
):
    author_cls, book_cls = classes
    author = author_cls(name="a").save(db=db)
    book_cls(title="b1", author=author).save(db=db)
    book_cls(title="b2", author=author).save(db=db)
    authors, books = author_cls.__tablename__, book_cls.__tablename__
    db.adapter.execute(
        f"CREATE TRIGGER wr_refuse BEFORE DELETE ON {authors} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END;"
    )
    db.adapter.auto_commit()

    def refused():
        with pytest.raises(Exception, match="refused"):
            author.delete_with_policy(db=db, on_delete=policy)

    if caught_inside:
        with db.transaction():
            refused()
    else:
        refused()

    rows = db.adapter.execute(
        f"SELECT json_extract(data, '$.title'), json_extract(data, '$.author._id') "
        f"FROM {books} ORDER BY _id;"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("b1", 1), ("b2", 1)]
    assert db.adapter.execute(f"SELECT count(*) FROM {authors};").fetchone()[0] == 1
    assert author._id == 1
    if author_cls is Author:
        assert [r[1] for r in _audit(db, Author)] == ["create"]


# ----- a class with no rule pays nothing --------------------------------------------------------

# What a class with no rule sent before the rules had one owner, by the first word of each
# statement: a new row, a changed row, a batch of two, and a delete (the same for delete() and
# each policy). A delete also looks through every registered table for rows that refer to
# the target; those reads depend on what else is registered and are left out.
# a write by _id makes no table; a new row makes none once its table is remembered; a batch of
# several rows sends the check of the columns first (a ``SELECT``)
UNVERSIONED = (["INSERT"], ["UPDATE"], ["CREATE", "SELECT", "INSERT"])
VERSIONED = (
    ["INSERT"],
    ["BEGIN", "UPDATE"],
    ["CREATE", "PRAGMA", "CREATE", "PRAGMA", "SELECT", "INSERT"],
)
A_DELETE = ["CREATE", "DELETE"]


def _sent(act):
    with query_logger.capture() as q:
        act()
    return [
        e.sql.split()[0] for e in q if "sqlite_master" not in e.sql and "data LIKE" not in e.sql
    ]


def _statements(db, Item):
    Item(name="warm").save(db=db)
    inst = Item(name="a")
    sent = [_sent(lambda: inst.save(db=db))]
    inst.name = "b"
    sent.append(_sent(lambda: inst.save(db=db)))
    sent.append(_sent(lambda: Item.save_many([Item(name="c"), Item(name="d")], db=db)))
    sent.append(_sent(lambda: inst.delete(db=db)))
    for policy in ("restrict", "set_null", "cascade"):
        other = Item(name=policy).save(db=db)
        sent.append(_sent(lambda: other.delete_with_policy(db=db, on_delete=policy)))
    return sent


def _expected(kind):
    return [*(VERSIONED if kind in SAFE else UNVERSIONED), *[A_DELETE] * 4]


@pytest.mark.parametrize("kind", KINDS)
def test_a_class_with_no_mixin_sends_the_statements_it_sent(db, kind):
    assert _statements(db, make(kind)) == _expected(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_a_timestamp_class_sends_what_a_plain_class_sends(db, kind):
    assert _statements(db, make(kind, TimestampMixin)) == _expected(kind)


# ----- a save of a row that is not there ---------------------------------------------------------


def _refused(kind):
    """What a by-id save of a row that is not there raises: a safe class reads it as a row at
    another version, every other class as a row that is gone."""
    return StaleVersionError if kind in SAFE else NotFoundError


def _delete_rows(db, table):
    db.adapter.execute(f"DELETE FROM {table};")
    db.adapter.auto_commit()


def _gone_by_statement(db, Item, inst):
    _delete_rows(db, Item.__tablename__)


def _gone_by_delete_all(db, Item, inst):
    Item.using(db).delete_all()


def _never_there(db, Item, inst):
    inst._id = 41


def _gone_in_the_callers_transaction(db, Item, inst):
    """Marks the case: the row is deleted inside the transaction the save runs in."""


GONE = [_gone_by_statement, _gone_by_delete_all, _never_there, _gone_in_the_callers_transaction]
MIXINS = {"none": (), "hooks_audit": (Recorded, AuditLogMixin), "timestamps": (TimestampMixin,)}


@pytest.mark.parametrize("gone", GONE)
@pytest.mark.parametrize("mixins", list(MIXINS))
@pytest.mark.parametrize("promoted", [None, {"status": "TEXT"}], ids=["json", "promoted"])
@pytest.mark.parametrize("kind", KINDS)
def test_a_save_of_a_row_that_is_not_there_raises_and_keeps_the_instance(
    db, kind, promoted, mixins, gone
):
    Item = make(kind, *MIXINS[mixins], promoted=promoted)
    inst = Item(name="first").save(db=db)
    gone(db, Item, inst)
    inst.name = "second"
    before = (_identity(inst), _home_of(inst))
    audit_before = _audit(db, Item) if mixins == "hooks_audit" else None
    CALLS.clear()

    if gone is _gone_in_the_callers_transaction:
        with db.transaction():
            db.adapter.execute(f"DELETE FROM {Item.__tablename__};")
            with pytest.raises(_refused(kind)):
                inst.save(db=db)
    else:
        with pytest.raises(_refused(kind)):
            inst.save(db=db)

    kept = [(1, "first")] if gone is _never_there else []
    assert [(i, d["name"]) for i, d in _docs(db, Item)] == kept
    assert (_identity(inst), _home_of(inst)) == before
    assert inst.name == "second"
    if mixins == "hooks_audit":
        assert CALLS == [("before_save", "second")]
        assert _audit(db, Item) == audit_before


@pytest.mark.parametrize("kind", KINDS)
def test_a_save_of_a_row_another_connection_deleted_raises(kind, tmp_path):
    path = str(tmp_path / "two.db")
    one, two = SQLerDB.on_disk(path), SQLerDB.on_disk(path)
    try:
        Item = make(kind)
        inst = Item(name="first").save(db=one)
        _delete_rows(two, Item.__tablename__)
        inst.name = "second"
        with pytest.raises(_refused(kind)):
            inst.save(db=one)
        assert _docs(one, Item) == []
        assert inst._id == 1
    finally:
        one.close()
        two.close()


@pytest.mark.parametrize("kind", KINDS)
def test_a_save_after_another_thread_deleted_the_row_raises(kind, tmp_path):
    path = str(tmp_path / "threads.db")
    db = SQLerDB.on_disk(path)
    try:
        Item = make(kind)
        inst = Item(name="first").save(db=db)

        def delete():
            other = SQLerDB.on_disk(path)
            _delete_rows(other, Item.__tablename__)
            other.close()

        t = threading.Thread(target=delete)
        t.start()
        t.join()
        inst.name = "second"
        with pytest.raises(_refused(kind)):
            inst.save(db=db)
        assert _docs(db, Item) == []
    finally:
        db.close()


def test_a_save_of_a_row_a_cascade_deleted_raises(db):
    author = PlainAuthor(name="a").save(db=db)
    book = PlainBook(title="b", author=author).save(db=db)
    author.delete_with_policy(db=db, on_delete="cascade")
    book.title = "c"
    book.author = None
    with pytest.raises(NotFoundError, match="Row 1 of wr_plain_books"):
        book.save(db=db)
    assert _docs(db, PlainBook) == []
    assert book._id == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_refused_save_the_caller_catches_and_commits_past_writes_nothing(db, kind):
    Item = make(kind, Recorded, AuditLogMixin)
    inst = Item(name="first").save(db=db)
    _delete_rows(db, Item.__tablename__)
    inst.name = "second"
    with db.transaction():
        with pytest.raises(_refused(kind)):
            inst.save(db=db)
        db.insert_document("beside", {"name": "the caller's own row"})
    assert _docs(db, Item) == []
    assert [r[1] for r in _audit(db, Item)] == ["create"]
    assert db.find_document("beside", 1) == {"name": "the caller's own row", "_id": 1}


@pytest.mark.parametrize("kind", KINDS)
def test_a_save_of_a_row_read_from_another_table_that_is_gone_raises(db, kind):
    Item = make(kind)
    db.insert_document("wr_other_table", {"name": "first"})
    inst = Item.using(db, table="wr_other_table").all()[0]
    _delete_rows(db, "wr_other_table")
    inst.name = "second"
    with pytest.raises(_refused(kind)):
        inst.save()
    assert db.adapter.execute("SELECT count(*) FROM wr_other_table;").fetchone()[0] == 0
    assert _docs(db, Item) == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_save_to_a_database_without_the_row_raises_and_the_home_stays(db, kind):
    Item = make(kind)
    inst = Item(name="first").save(db=db)
    other = SQLerDB.in_memory(shared=False)
    try:
        inst.name = "second"
        with pytest.raises(_refused(kind)):
            inst.save(db=other)
        assert (
            _exists(other, Item.__tablename__) is False
        )  # a write by _id makes no table, versioned too
        assert _docs(other, Item) == []
        inst.save()
        assert [(i, d["name"]) for i, d in _docs(db, Item)] == [(1, "second")]
        copy = Item(name=inst.name).save(db=other)
        assert [(i, d["name"]) for i, d in _docs(other, Item)] == [(copy._id, "second")]
        assert copy._id == 1
    finally:
        other.close()


def test_the_refusal_names_the_table_the_id_and_how_to_copy_a_row(db):
    with pytest.raises(NotFoundError) as caught:
        db.upsert_document("wr_named", 7, {"name": "x"})
    assert str(caught.value) == (
        "Row 7 of wr_named is not there: a write by _id does not create it; to put a copy "
        "into another database, save a new instance without the _id"
    )
    assert caught.value.id_ == 7
    assert _exists(db, "wr_named") is False


def test_a_refused_update_sends_what_an_update_that_lands_sends(db):
    db.insert_document("wr_counted", {"name": "x"})
    with query_logger.capture() as landed:
        db.upsert_document("wr_counted", 1, {"name": "y"})
    with query_logger.capture() as refused:
        with pytest.raises(NotFoundError):
            db.upsert_document("wr_counted", 9, {"name": "y"})
    assert [q.sql for q in refused] == [q.sql for q in landed]
    assert [q.sql.split()[0] for q in refused] == ["UPDATE"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_save_after_the_table_was_dropped_raises(db, kind):
    """A write by ``_id`` finds no table and makes none: a plain class raises that the row is
    gone, a safe class that it is at another version."""
    Item = make(kind)
    inst = Item(name="first").save(db=db)
    db.adapter.execute(f"DROP TABLE {Item.__tablename__};")
    db.adapter.auto_commit()
    inst.name = "second"
    with pytest.raises(_refused(kind)):
        inst.save(db=db)
    assert _exists(db, Item.__tablename__) is False
    assert _docs(db, Item) == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_refresh_of_a_row_that_is_gone_raises_lookup_error(db, kind):
    Item = make(kind)
    inst = Item(name="first").save(db=db)
    _delete_rows(db, Item.__tablename__)
    with pytest.raises(LookupError, match="Row 1 not found for refresh"):
        inst.refresh(db=db)
    assert inst._id == 1


# ----- a set_null write is the database's by-id write: the table as it is now ---------------------


class NulledAuthor(SQLerModel):
    __tablename__ = "wr_nulled_authors"
    name: str


class StampedBook(TimestampMixin, SQLerModel):
    __tablename__ = "wr_stamped_books"
    title: str
    author: Optional[NulledAuthor] = None


class SafeBook(SQLerSafeModel):
    __tablename__ = "wr_safe_books"
    title: str
    author: Optional[NulledAuthor] = None


def _raw(path, sql):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_a_set_null_after_another_object_promoted_the_stamp_writes_the_stamp_into_its_column(
    tmp_path, ticking_clock
):
    """``updated_at`` became a column after ``db`` learned the table: the nulled row's new
    stamp goes into the column, and the document holds no copy the column would hide."""
    path = str(tmp_path / "stamped.db")
    db = SQLerDB.on_disk(path)
    author = NulledAuthor(name="a").save(db=db)
    book = StampedBook(title="b", author=author).save(db=db)
    db.find_document("wr_stamped_books", book._id)
    other = SQLerDB.on_disk(path)
    other.ensure_table("wr_stamped_books", promoted={"updated_at": "TEXT"})
    other.close()
    [(old,)] = _raw(path, "SELECT updated_at FROM wr_stamped_books")
    try:
        author.delete_with_policy(db=db, on_delete="set_null")
        read = StampedBook.from_id(book._id, db=db).updated_at.isoformat()
    finally:
        db.close()
    [(data, new)] = _raw(path, "SELECT data, updated_at FROM wr_stamped_books")
    assert json.loads(data) == {
        "created_at": book.created_at.isoformat(),
        "title": "b",
        "author": None,
    }
    assert (new != old, read) == (True, new)


def test_a_set_null_on_a_safe_table_another_object_remade_without_its_version_writes_the_row(
    tmp_path,
):
    """The table was dropped and made again without ``_version`` after ``db`` learned it: the
    write learns it again and writes the row as the table is now; a plain write adds no column,
    so the version is a fact about the table, not the class of the row."""
    import sqlite3

    path = str(tmp_path / "safe.db")
    db = SQLerDB.on_disk(path)
    author = NulledAuthor(name="a").save(db=db)
    book = SafeBook(title="b", author=author).save(db=db)
    db.find_document_with_version("wr_safe_books", book._id)
    conn = sqlite3.connect(path)
    [(data,)] = conn.execute("SELECT data FROM wr_safe_books").fetchall()
    conn.executescript(
        "DROP TABLE wr_safe_books; CREATE TABLE wr_safe_books (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL);"
    )
    conn.execute("INSERT INTO wr_safe_books (_id, data) VALUES (1, ?)", [data])
    conn.commit()
    conn.close()
    try:
        author.delete_with_policy(db=db, on_delete="set_null")
    finally:
        db.close()
    assert _raw(path, "SELECT name FROM pragma_table_info('wr_safe_books')") == [
        ("_id",),
        ("data",),
    ]
    [(data,)] = _raw(path, "SELECT data FROM wr_safe_books")
    assert json.loads(data) == {"title": "b", "author": None}
    assert _raw(path, "SELECT count(*) FROM wr_nulled_authors") == [(0,)]


# ----- a loaded instance saves again, on every base -----------------------------------------

T_SET = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)

# (name, mixins, the fields the mixins give and the type each holds once set)
ROUND_TRIPS = [
    ("timestamp", (TimestampMixin,), {"created_at": datetime, "updated_at": datetime}),
    ("soft_delete", (SoftDeleteMixin,), {"deleted_at": datetime}),
    (
        "audit",
        (AuditMixin,),
        {"created_at": datetime, "updated_at": datetime, "created_by": str, "updated_by": str},
    ),
    ("hooks", (Recorded,), {}),
    ("tracking", (TrackedModel,), {}),
    (
        "full",
        (FullMixin,),
        {"created_at": datetime, "updated_at": datetime, "deleted_at": datetime},
    ),
    ("audit_log", (AuditLogMixin,), {}),
]
ROUND_TRIP_CASES = [
    pytest.param(mixins, held, kind, id=f"{name}-{kind}")
    for name, mixins, held in ROUND_TRIPS
    for kind in KINDS
]


@pytest.mark.parametrize("mixins, held, kind", ROUND_TRIP_CASES)
def test_a_loaded_instance_holds_its_mixin_fields_as_typed_and_saves_again(db, mixins, held, kind):
    Item = make(kind, *mixins)
    fresh = Item(name="a")
    if "deleted_at" in held:
        fresh.deleted_at = T_SET
    fresh.save(db=db)

    loaded = Item.from_id(fresh._id, db=db)
    assert {f: type(getattr(loaded, f)) for f in held} == held
    assert {f: getattr(loaded, f) for f in held} == {f: getattr(fresh, f) for f in held}

    loaded.name = "b"
    loaded.save(db=db)

    again = Item.from_id(fresh._id, db=db)
    assert again.name == "b"
    assert [doc["name"] for _, doc in _docs(db, Item)] == ["b"]
    if "created_at" in held:
        assert again.created_at == fresh.created_at
        assert again.updated_at > fresh.updated_at
    if "deleted_at" in held:
        assert again.deleted_at == T_SET
    if "created_by" in held:
        assert (again.created_by, again.updated_by) == ("alice", "alice")


@pytest.mark.parametrize("kind", KINDS)
def test_a_refresh_holds_the_row_as_a_load_does_and_saves_again(db, kind):
    Item = make(kind, TimestampMixin, fields={"when": (Optional[datetime], None)})
    mine = Item(name="a", when=T_SET).save(db=db)
    other = Item.from_id(mine._id, db=db)
    other.name = "b"
    other.save(db=db)

    assert mine.refresh(db=db) is mine

    held = ("name", "when", "created_at", "updated_at")
    assert (mine.name, mine.when) == ("b", T_SET)
    assert {f: getattr(mine, f) for f in held} == {f: getattr(other, f) for f in held}
    assert [type(getattr(mine, f)) for f in held] == [str, datetime, datetime, datetime]

    mine.name = "c"
    mine.save(db=db)

    assert [doc["name"] for _, doc in _docs(db, Item)] == ["c"]


@pytest.mark.parametrize("mixin", [TimestampMixin, AuditMixin])
@pytest.mark.parametrize("kind", KINDS)
def test_save_with_a_naive_created_at_raises_and_writes_nothing(db, kind, mixin):
    Item = make(kind, mixin)
    naive = Item(name="naive")
    naive.created_at = datetime(2020, 1, 1)

    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        naive.save(db=db)

    assert _docs(db, Item) == []


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class Size(Enum):
    S = 1
    L = 3


# (field, its type, the value saved); each loads back as that value, of that type
TYPED_FIELDS = {
    "when": (Optional[datetime], T_SET),
    "naive": (datetime, datetime(2026, 1, 2, 3, 4)),
    "day": (date, date(2026, 11, 14)),
    "at": (time, time(9, 30, 1)),
    "ticket": (UUID, UUID(int=7)),
    "where": (PurePosixPath, PurePosixPath("/srv/data")),
    "color": (Color, Color.BLUE),
    "size": (Size | None, Size.L),
    "price": (Decimal, Decimal("12.25")),
    "wait": (timedelta, timedelta(minutes=90, microseconds=500000)),
    "pair": (tuple[int, str], (1, "a")),
    "steps": (tuple[date, ...], (date(2026, 1, 1), date(2026, 1, 2))),
    "labels": (set[str], {"b", "a"}),
    "stamps": (list[datetime], [T_SET, T_SET + timedelta(days=1)]),
    "due": (dict[str, date], {"x": date(2026, 2, 3)}),
}
# the types a msgspec Struct field cannot be loaded as: msgspec converts a Struct's own fields
# itself, and refuses the stored form of these (sqler converts only the fields a mixin gives)
MSGSPEC_CANNOT = {"where"}


@pytest.mark.parametrize("kind", KINDS)
def test_a_field_loads_as_the_type_it_declares(db, kind):
    typed = {
        n: hv for n, hv in TYPED_FIELDS.items() if kind != "msgspec" or n not in MSGSPEC_CANNOT
    }
    Item = make(kind, fields={name: (hint, None) for name, (hint, _) in typed.items()})
    saved = Item(name="a", **{name: value for name, (_, value) in typed.items()}).save(db=db)

    loaded = Item.from_id(saved._id, db=db)

    got = {name: getattr(loaded, name) for name in typed}
    want = {name: value for name, (_, value) in typed.items()}
    assert len(got) == (14 if kind == "msgspec" else 15)
    assert got == want
    assert {n: type(v) for n, v in got.items()} == {n: type(v) for n, v in want.items()}
    assert loaded.is_dirty() is False


@pytest.mark.parametrize("kind", ["lite", "lite_safe"])
def test_a_string_that_is_no_date_in_a_date_field_loads_as_it_was_saved(db, kind):
    # a lite model checks no type on save, so its datetime field can hold any string; the
    # pydantic and msgspec bases refuse one that is no date
    Item = make(kind, fields={"when": (Optional[datetime], None)})
    saved = Item(name="a", when="tomorrow").save(db=db)
    assert Item.from_id(saved._id, db=db).when == "tomorrow"


@pytest.mark.parametrize("base", [SQLerLiteModel, SQLerLiteSafeModel])
def test_a_lite_field_typed_in_quotes_loads_as_its_type(db, base):
    @dataclass
    class Visit(base):
        __tablename__ = f"wr_quoted_{base.__name__}"
        when: "Optional[datetime]" = None

    saved = Visit(when=T_SET).save(db=db)

    assert Visit.from_id(saved._id, db=db).when == T_SET


# ----- a field holding a plain dataclass: stored as its fields, loaded as its type ----------


@dataclass
class Address:
    city: str = ""


@dataclass
class Stamp:
    at: Optional[datetime] = None
    ticket: Optional[UUID] = None


@dataclass
class Home:
    address: Optional[Address] = None
    since: Optional[date] = None


@dataclass(frozen=True)
class FrozenAddress:
    city: str = ""


@dataclass(slots=True)
class SlottedAddress:
    city: str = ""


@dataclass
class Counted:
    items: list = dataclass_field(default_factory=list)
    count: int = dataclass_field(init=False, default=0)

    def __post_init__(self):
        self.count = len(self.items)


@dataclass
class Blob:
    data: bytes = b""


# (row, field type, value saved, the document's value for it); each loads back as that value
NESTED = [
    ("dataclass", Optional[Address], Address(city="x"), {"city": "x"}),
    (
        "datetime_and_uuid",
        Optional[Stamp],
        Stamp(T_SET, UUID(int=7)),
        {"at": T_SET.isoformat(), "ticket": str(UUID(int=7))},
    ),
    (
        "dataclass_in_dataclass",
        Optional[Home],
        Home(Address(city="x"), date(2026, 1, 2)),
        {"address": {"city": "x"}, "since": "2026-01-02"},
    ),
    ("optional_none", Optional[Address], None, None),
    ("list_empty", list[Address], [], []),
    (
        "list_two",
        list[Address],
        [Address(city="x"), Address(city="y")],
        [{"city": "x"}, {"city": "y"}],
    ),
    ("tuple", tuple[Address, ...], (Address(city="x"),), [{"city": "x"}]),
    ("dict", dict[str, Address], {"k": Address(city="x")}, {"k": {"city": "x"}}),
    ("frozen", Optional[FrozenAddress], FrozenAddress(city="x"), {"city": "x"}),
    ("slots", Optional[SlottedAddress], SlottedAddress(city="x"), {"city": "x"}),
    ("init_false", Optional[Counted], Counted(items=[1, 2]), {"items": [1, 2], "count": 2}),
]
NESTED_CASES = [
    pytest.param(hint, value, doc, kind, id=f"{row}-{kind}")
    for row, hint, value, doc in NESTED
    for kind in KINDS
]


@pytest.mark.parametrize("hint, value, doc, kind", NESTED_CASES)
def test_a_dataclass_field_is_stored_as_its_fields_and_loads_as_its_type(
    db, hint, value, doc, kind
):
    Item = make(kind, fields={"home": (hint, None)})
    saved = Item(name="a", home=value).save(db=db)

    assert _docs(db, Item) == [(saved._id, {"name": "a", "home": doc})]
    loaded = Item.from_id(saved._id, db=db)
    assert loaded.home == value
    assert type(loaded.home) is type(value)
    if isinstance(
        value, (list, tuple, dict)
    ):  # each entry an Address, not the dict it is stored as
        entries = list(loaded.home.values() if isinstance(value, dict) else loaded.home)
        assert [type(v) for v in entries] == [Address] * len(value)
    assert loaded.is_dirty() is False


@pytest.mark.parametrize("kind", ["lite", "lite_safe"])
def test_a_lite_dataclass_field_loads_by_a_query_a_refresh_and_a_nested_typed_field(db, kind):
    Item = make(kind, fields={"home": (Optional[Home], None)})
    value = Home(Address(city="x"), date(2026, 1, 2))
    saved = Item(name="a", home=value).save(db=db)

    [queried] = Item.using(db).all()
    assert queried.home == value
    assert type(queried.home.address) is Address and type(queried.home.since) is date

    other = Item.from_id(saved._id, db=db)
    other.home = Home(Address(city="y"))
    other.save(db=db)
    assert saved.refresh(db=db).home == Home(Address(city="y"))
    assert type(saved.home) is Home


@pytest.mark.parametrize("kind", KINDS)
def test_a_reference_stays_a_reference_beside_a_dataclass_field(db, kind):
    Owner = make(kind)
    Item = make(kind, fields={"owner": (Optional[Owner], None), "home": (Optional[Address], None)})
    owner = Owner(name="o").save(db=db)
    saved = Item(name="a", owner=owner, home=Address(city="x")).save(db=db)

    assert _docs(db, Item) == [
        (
            saved._id,
            {
                "name": "a",
                "owner": {"_table": Owner.__tablename__, "_id": owner._id},
                "home": {"city": "x"},
            },
        )
    ]
    loaded = Item.from_id(saved._id, db=db)
    assert (type(loaded.owner), loaded.owner._id, loaded.owner.name) == (Owner, owner._id, "o")
    assert loaded.home == Address(city="x")


def test_a_lite_model_the_caller_does_not_store_as_a_reference_is_refused_not_nested():
    from sqler import AsyncSQLerLiteModel
    from sqler._stored import dumps, stored
    from sqler.exceptions import UnstorableValueError

    @dataclass
    class Other(AsyncSQLerLiteModel):
        __tablename__ = "wr_other_async_lite"
        name: str = ""

    with pytest.raises(
        UnstorableValueError, match=r"Object of type Other is not JSON serializable \(at home\)"
    ):
        dumps(stored({"home": Other(name="o")}, SQLerLiteModel))


@pytest.mark.parametrize("kind", KINDS)
def test_a_field_init_does_not_take_is_stored_and_loads_as_init_makes_it_on_every_base(db, kind):
    Item = make(kind, fields={"home": (Optional[Counted], None)})
    counted = Counted(items=[1, 2])
    counted.count = 5  # set after __init__
    saved = Item(name="a", home=counted).save(db=db)

    assert _docs(db, Item) == [(saved._id, {"name": "a", "home": {"items": [1, 2], "count": 5}})]
    assert Item.from_id(saved._id, db=db).home == Counted(items=[1, 2])  # count 2: __post_init__


@pytest.mark.parametrize("kind", KINDS)
def test_bytes_inside_a_dataclass_field_is_refused_at_its_path(db, kind):
    from sqler.exceptions import UnstorableValueError

    Item = make(kind, fields={"home": (Optional[Blob], None)})
    with pytest.raises(UnstorableValueError, match=r"bytes cannot be stored .* \(at home\.data\)"):
        Item(name="a", home=Blob(b"\x00")).save(db=db)
    assert _docs(db, Item) == []


@pytest.mark.parametrize("value", [Address, object()], ids=["dataclass_class", "unknown_object"])
def test_a_dataclass_class_or_an_unknown_object_is_refused(value):
    from sqler._stored import dumps
    from sqler.exceptions import UnstorableValueError

    with pytest.raises(UnstorableValueError, match=r"is not JSON serializable \(at home\)"):
        dumps({"home": value})


@pytest.mark.parametrize("kind", ["lite", "lite_safe"])
def test_a_stored_dict_loads_as_the_dataclass_its_field_declares_and_a_dict_field_stays_a_dict(
    db, kind
):
    Item = make(kind, fields={"home": (Optional[Address], None), "extra": (Optional[dict], None)})
    _id = db.insert_document(
        Item.__tablename__, {"name": "a", "home": {"city": "x"}, "extra": {"city": "y"}}
    )

    loaded = Item.from_id(_id, db=db)

    assert loaded.home == Address(city="x")
    assert loaded.extra == {"city": "y"}


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
def test_a_model_inside_a_dataclass_value_is_refused_at_its_place_and_nothing_is_written(
    db, kind, args, place
):
    from sqler.exceptions import UnstorableValueError

    Owner = make(kind)
    Holder, Inner = _holders(Owner)
    Item = make(kind, fields={"home": (Optional[Holder], None)})
    owner = Owner(name="o").save(db=db)
    item = Item(name="a", home=Holder(city="x", **args(Inner, owner)))

    with pytest.raises(
        UnstorableValueError,
        match=rf"^model {Owner.__name__} inside a dataclass value is not stored as a reference: "
        rf"make it a field of the model \(at {place}\)$",
    ):
        item.save(db=db)
    assert _docs(db, Item) == []
    assert item._id is None


@pytest.mark.parametrize("kind", KINDS)
def test_a_model_in_a_list_and_a_dict_field_of_the_model_stays_a_reference_beside_a_dataclass(
    db, kind
):
    Owner = make(kind)
    Holder, _ = _holders(Owner)
    Item = make(
        kind,
        fields={
            "owners": (list[Owner], None),
            "by": (dict[str, Owner], None),
            "home": (Optional[Holder], None),
        },
    )
    owner = Owner(name="o").save(db=db)
    saved = Item(name="a", owners=[owner], by={"k": owner}, home=Holder(city="x")).save(db=db)

    ref = {"_table": Owner.__tablename__, "_id": owner._id}
    home = {"city": "x", "owner": None, "owners": [], "pair": [], "by": {}, "inner": None}
    assert _docs(db, Item) == [
        (saved._id, {"name": "a", "owners": [ref], "by": {"k": ref}, "home": home})
    ]
    loaded = Item.from_id(saved._id, db=db)
    assert [(type(o), o._id) for o in [*loaded.owners, *loaded.by.values()]] == [
        (Owner, owner._id)
    ] * 2


# ----- a field holding a plain msgspec.Struct ----------------------------------------------


class SAddress(msgspec.Struct):
    city: str = ""


class SStamp(msgspec.Struct):
    at: Optional[datetime] = None
    place: Optional[SAddress] = None


class SFrozen(msgspec.Struct, frozen=True):
    city: str = ""


class SKwOnly(msgspec.Struct, kw_only=True):
    city: str
    zip: str = ""


class SCamel(msgspec.Struct, rename="camel"):
    zip_code: str = ""
    home_city: str = ""


class SBlob(msgspec.Struct):
    data: bytes = b""


class SPair(msgspec.Struct, array_like=True):
    x: int = 0
    y: int = 0


class SCat(msgspec.Struct, tag="cat"):
    name: str = ""


class SDog(msgspec.Struct, tag="dog"):
    name: str = ""


# (row, field type, value saved, the document's value for it); each loads back as that value
STRUCTS = [
    ("struct", Optional[SAddress], SAddress(city="x"), {"city": "x"}),
    (
        "datetime_and_struct_in_struct",
        Optional[SStamp],
        SStamp(T_SET, SAddress("y")),
        {"at": T_SET.isoformat(), "place": {"city": "y"}},
    ),
    ("optional_none", Optional[SAddress], None, None),
    (
        "list_two",
        list[SAddress],
        [SAddress(city="x"), SAddress(city="y")],
        [{"city": "x"}, {"city": "y"}],
    ),
    ("frozen", Optional[SFrozen], SFrozen(city="x"), {"city": "x"}),
    ("kw_only", Optional[SKwOnly], SKwOnly(city="x", zip="1"), {"city": "x", "zip": "1"}),
    (
        "renamed_by_its_encoded_names",
        Optional[SCamel],
        SCamel("1", "x"),
        {"zipCode": "1", "homeCity": "x"},
    ),
    ("array_like_as_a_list", Optional[SPair], SPair(1, 2), [1, 2]),
    ("list_of_array_like", list[SPair], [SPair(1, 2), SPair(3, 4)], [[1, 2], [3, 4]]),
    ("tagged_with_its_tag", Optional[SDog], SDog("rex"), {"type": "dog", "name": "rex"}),
]


@pytest.mark.parametrize(
    "hint, value, doc", [pytest.param(h, v, d, id=r) for r, h, v, d in STRUCTS]
)
@pytest.mark.parametrize("kind", ["msgspec", "lite", "lite_safe"])
def test_a_struct_field_is_stored_as_its_fields_and_loads_as_its_type(db, kind, hint, value, doc):
    Item = make(kind, fields={"home": (hint, None)})
    saved = Item(name="a", home=value).save(db=db)

    assert _docs(db, Item) == [(saved._id, {"name": "a", "home": doc})]
    loaded = Item.from_id(saved._id, db=db)
    assert loaded.home == value
    assert type(loaded.home) is type(value)
    if isinstance(value, list):
        assert [type(v) for v in loaded.home] == [type(v) for v in value]
    if isinstance(value, SStamp):
        assert (type(loaded.home.at), type(loaded.home.place)) == (datetime, SAddress)
    assert loaded.is_dirty() is False


def test_a_union_of_tagged_structs_loads_as_the_one_that_was_saved(db):
    Item = make("msgspec", fields={"pet": (Union[SCat, SDog, None], None)})
    saved = Item(name="a", pet=SDog("rex")).save(db=db)

    assert _docs(db, Item) == [(saved._id, {"name": "a", "pet": {"type": "dog", "name": "rex"}})]
    assert Item.from_id(saved._id, db=db).pet == SDog("rex")


def test_a_msgspec_model_field_stays_a_reference_beside_a_struct_field(db):
    Owner = make("msgspec")
    Item = make(
        "msgspec", fields={"owner": (Optional[Owner], None), "home": (Optional[SAddress], None)}
    )
    owner = Owner(name="o").save(db=db)
    saved = Item(name="a", owner=owner, home=SAddress(city="x")).save(db=db)

    assert _docs(db, Item) == [
        (
            saved._id,
            {
                "name": "a",
                "owner": {"_table": Owner.__tablename__, "_id": owner._id},
                "home": {"city": "x"},
            },
        )
    ]
    loaded = Item.from_id(saved._id, db=db)
    assert (type(loaded.owner), loaded.owner._id, loaded.owner.name) == (Owner, owner._id, "o")
    assert loaded.home == SAddress(city="x")


def test_a_msgspec_model_inside_a_struct_value_is_refused_at_its_place(db):
    from sqler.exceptions import UnstorableValueError

    Owner = make("msgspec")
    Holder = msgspec.defstruct("Holder", [("owners", list, [])])
    Item = make("msgspec", fields={"home": (Optional[Holder], None)})
    owner = Owner(name="o").save(db=db)
    item = Item(name="a", home=Holder(owners=[owner]))

    with pytest.raises(
        UnstorableValueError,
        match=rf"^model {Owner.__name__} inside a Struct value is not stored as a reference: "
        r"make it a field of the model \(at home.owners\[0\]\)$",
    ):
        item.save(db=db)
    assert _docs(db, Item) == []
    assert item._id is None


@pytest.mark.parametrize("kind", ["msgspec", "lite", "lite_safe"])
def test_bytes_inside_a_struct_field_is_refused_at_its_path(db, kind):
    from sqler.exceptions import UnstorableValueError

    Item = make(kind, fields={"home": (Optional[SBlob], None)})
    with pytest.raises(UnstorableValueError, match=r"bytes cannot be stored .* \(at home\.data\)"):
        Item(name="a", home=SBlob(b"\x00")).save(db=db)
    assert _docs(db, Item) == []
