"""What kind a model class is (pydantic, lite, msgspec; frozen or not) is found once per
class: a row read after the first asks the class nothing of what it is, each class has its
own answer, and the answer keeps no class alive. A pydantic class that forbids extra keys
is one of the answers: it is not given the row's own ``_id`` and ``_version`` as fields."""

import asyncio
import gc
import inspect
import sys
import threading
import types
import weakref
from dataclasses import FrozenInstanceError, dataclass
from typing import Optional

import pytest
from pydantic import ConfigDict, ValidationError

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
    SQLerDB,
    SQLerLiteModel,
    SQLerModel,
    SQLerSafeModel,
)
from sqler.models import _refs
from sqler.models._compat import is_lite_model, is_msgspec_model
from sqler.models.msgspec import AsyncSQLerMsgspecModel, SQLerMsgspecModel


@pytest.fixture
def db():
    db = SQLerDB.in_memory()
    yield db
    db.close()


# ---- a row read after the first asks the class nothing ------------------------------

LOOKED_UP: list = []
COUNTING = [False]


def _counting(meta):
    """A metaclass that notes each attribute the row builder of sqler looks up on its class."""

    class Counting(meta):
        def __getattribute__(cls, name):
            if COUNTING[0] and sys._getframe(1).f_globals.get("__name__") == _refs.__name__:
                LOOKED_UP.append(name)
            return super().__getattribute__(name)

    return Counting


class KCountP(SQLerModel, metaclass=_counting(type(SQLerModel))):
    name: str = ""


@dataclass
class KCountL(SQLerLiteModel, metaclass=_counting(type)):
    name: str = ""


try:

    class KCountM(SQLerMsgspecModel, metaclass=_counting(type(SQLerMsgspecModel))):
        name: str = ""

except TypeError:  # msgspec before 0.20: no class can extend the metaclass of its structs
    KCountM = None

needs_a_counting_struct = pytest.mark.skipif(
    KCountM is None,
    reason="msgspec before 0.20 lets no class extend the metaclass of its structs",
)


@pytest.mark.parametrize(
    "cls",
    [
        pytest.param(KCountP, id="pydantic"),
        pytest.param(KCountL, id="lite"),
        pytest.param(KCountM, id="msgspec", marks=needs_a_counting_struct),
    ],
)
def test_a_row_read_after_the_first_asks_its_class_nothing_of_what_it_is(db, cls):
    """The two lookups left are uses, not questions: the class's ``_versioned_rows`` (a class
    may set it) and the ``model_validate`` that builds the model."""
    db.insert_document("k_count", {"name": "a"})
    rows = cls.using(db, "k_count")
    rows.all()  # the first use of the class
    LOOKED_UP.clear()
    COUNTING[0] = True
    try:
        (model,) = rows.all()
    finally:
        COUNTING[0] = False
    assert (model.name, model._id) == ("a", 1)
    assert LOOKED_UP == ["model_validate", "_versioned_rows"]


# ---- the answer keeps no class alive ------------------------------------------------


class KDropChild(SQLerModel):
    name: str = ""


def test_a_thousand_classes_made_used_and_dropped_are_all_collected(db):
    """Pydantic classes, each read and saved through, with a field that admits a model. They
    share one table, so the registry holds the latest only, and a last class takes its place
    (the lite and msgspec bases keep per-class field caches of their own)."""
    db.insert_document("k_dropped", {"name": "a", "child": None})
    refs, ids = [], []
    for i in range(1000):

        class KDropped(SQLerModel):
            __tablename__ = "k_dropped"
            name: str = ""
            child: Optional[KDropChild] = None

        (model,) = KDropped.using(db, "k_dropped").all()
        assert (model.name, model.child, model._id) == ("a", None, 1)
        model.save(db=db)
        refs.append(weakref.ref(KDropped))
        ids.append(id(KDropped))
    del KDropped, model

    class KDroppedLast(SQLerModel):
        __tablename__ = "k_dropped"
        name: str = ""

    gc.collect()
    assert sum(r() is not None for r in refs) == 0
    assert [i for i in ids if i in _refs._KINDS] == []  # no answer is left for a new class to find


# ---- each class its own answer --------------------------------------------------------


class KPlain(SQLerLiteModel):
    """A lite class left without ``@dataclass``: assigning to it is a plain ``setattr``."""


def test_a_frozen_dataclass_subclass_of_a_plain_class_used_first_gets_its_own_answer(db):
    plain = KPlain()
    _refs._set(plain, "_id", 7)
    assert plain._id == 7

    @dataclass(frozen=True)
    class KFrozenSub(KPlain):
        name: str = ""

    db.insert_document("k_frozen_sub", {"name": "a"})
    (model,) = KFrozenSub.using(db, "k_frozen_sub").all()
    assert (model.name, model._id) == ("a", 1)
    with pytest.raises(FrozenInstanceError):
        model.name = "b"


class KMsBase(SQLerMsgspecModel):
    name: str = ""


def test_a_msgspec_subclass_with_more_fields_defined_after_its_base_was_used(db):
    db.insert_document("k_ms", {"name": "a", "extra": 5})
    (base,) = KMsBase.using(db, "k_ms").all()

    class KMsSub(KMsBase):
        extra: int = 0
        other: Optional[KMsBase] = None

    (sub,) = KMsSub.using(db, "k_ms").all()
    assert (base.name, base._id, hasattr(base, "extra")) == ("a", 1, False)
    assert (sub.name, sub.extra, sub.other, sub._id) == ("a", 5, None, 1)


# ---- shapes of a class -----------------------------------------------------------------


def _pydantic_dataclass():
    @dataclass
    class KPdDc(SQLerModel):
        name: str = ""

    return KPdDc


def _pydantic_frozen():
    class KPdFrozen(SQLerModel):
        model_config = ConfigDict(frozen=True)
        name: str = ""

    return KPdFrozen


def _pydantic_slots():
    class KPdSlots(SQLerModel):
        __slots__ = ("extra",)
        name: str = ""

    return KPdSlots


def _lite_frozen():
    @dataclass(frozen=True)
    class KLFrozen(SQLerLiteModel):
        name: str = ""

    return KLFrozen


def _lite_slots():
    @dataclass(slots=True)
    class KLSlots(SQLerLiteModel):
        name: str = ""

    return KLSlots


def _lite_frozen_slots():
    @dataclass(frozen=True, slots=True)
    class KLFrozenSlots(SQLerLiteModel):
        name: str = ""

    return KLFrozenSlots


def _msgspec_frozen():
    class KMFrozen(SQLerMsgspecModel, frozen=True):
        name: str = ""

    return KMFrozen


@pytest.mark.parametrize(
    "make",
    [
        _pydantic_dataclass,
        _pydantic_frozen,
        _pydantic_slots,
        _lite_frozen,
        _lite_slots,
        _lite_frozen_slots,
        _msgspec_frozen,
    ],
)
def test_each_shape_of_class_reads_its_rows_twice_the_same(db, make):
    cls = make()
    db.insert_document("k_shape", {"name": "a"})
    db.insert_document("k_shape", {"name": "b"})
    rows = cls.using(db, "k_shape")
    first = [(m.name, m._id) for m in rows.all()]
    second = [(m.name, m._id) for m in rows.all()]
    assert first == second == [("a", 1), ("b", 2)]


@pytest.mark.parametrize("base", [SQLerMsgspecModel, AsyncSQLerMsgspecModel], ids=["sync", "async"])
@pytest.mark.parametrize("frozen", [False, True], ids=["plain", "frozen"])
def test_a_msgspec_class_is_a_msgspec_model_on_the_sync_and_the_async_base(base, frozen):
    cls = types.new_class(
        "KMsKind",
        (base,),
        {"frozen": frozen},
        lambda ns: ns.update({"__annotations__": {"name": str}, "name": ""}),
    )
    kind = _refs._kind(cls)
    assert (is_msgspec_model(cls), is_lite_model(cls)) == (True, False)
    assert (kind.struct, kind.model, kind.lite, kind.pydantic, kind.forbids) == (
        True,
        True,
        False,
        False,
        False,
    )


# ---- two threads use a class for the first time at once -------------------------------


def test_two_threads_that_use_a_class_first_at_once_read_one_answer(db):
    class KThreads(SQLerModel):
        name: str = ""

    db.insert_document("k_threads", {"name": "a"})
    start = threading.Barrier(2, timeout=30)
    kinds, read, failed = [], [], []

    def use():
        try:
            start.wait()
            kinds.append(_refs._kind(KThreads))
            read.append([(m.name, m._id) for m in KThreads.using(db, "k_threads").all()])
        except BaseException as exc:  # pragma: no cover - reported below
            failed.append(exc)

    threads = [threading.Thread(target=use) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "a thread was still running after 30 s"
    assert failed == [], (
        "a thread failed (a BrokenBarrierError means the barrier timed out after 30 s)"
    )
    assert read == [[("a", 1)], [("a", 1)]]
    assert len(kinds) == 2 and kinds[0] is kinds[1] is _refs._kind(KThreads)


def test_an_answer_left_under_the_id_of_a_class_that_is_gone_is_not_given_to_the_class_that_has_the_id_now(
    db,
):
    """An id is used again once its class is gone; where the weak reference's callback runs
    late, the old answer is still there when the new class asks."""

    @dataclass
    class KOld(SQLerLiteModel):
        __tablename__ = "k_old"
        name: str = ""

    class KNew(SQLerModel):
        __tablename__ = "k_new"
        name: str = ""

    stale = _refs._kind(KOld)
    _refs._KINDS[id(KNew)] = stale  # as if KNew had been given the id KOld had
    kind = _refs._kind(KNew)
    assert (kind.lite, kind.pydantic, kind is stale) == (False, True, False)
    KNew(name="a").save(db=db)
    (model,) = KNew.using(db, "k_new").all()
    assert (type(model), model.name, model._id) == (KNew, "a", 1)


# ---- a class that forbids extra keys --------------------------------------------------


def _forbidding(base, child_base):
    """A parent and a child class on ``base`` that both set ``extra="forbid"``."""
    config = ConfigDict(extra="forbid")
    child = type(
        "FbChild",
        (child_base,),
        {
            "__tablename__": "fb_child",
            "__module__": __name__,
            "model_config": config,
            "__annotations__": {"label": str},
            "label": "",
        },
    )
    parent = type(
        "FbParent",
        (base,),
        {
            "__tablename__": "fb_parent",
            "__module__": __name__,
            "model_config": config,
            "__annotations__": {"name": str, "child": Optional[child]},
            "name": "",
            "child": None,
        },
    )
    return parent, child


FORBIDDING = {
    "plain": (SQLerModel, SQLerModel, False),
    "safe": (SQLerSafeModel, SQLerModel, False),
    "async": (AsyncSQLerModel, AsyncSQLerModel, True),
    "async_safe": (AsyncSQLerSafeModel, AsyncSQLerModel, True),
}


async def _aw(value):
    return await value if inspect.isawaitable(value) else value


def _with_forbidding(kind, steps):
    base, child_base, is_async = FORBIDDING[kind]

    async def scenario():
        db = AsyncSQLerDB.in_memory() if is_async else SQLerDB.in_memory()
        if is_async:
            await db.connect()
        parent, child = _forbidding(base, child_base)
        try:
            return await steps(db, parent, child)
        finally:
            await _aw(db.close())

    return asyncio.run(scenario())


@pytest.mark.parametrize("kind", list(FORBIDDING))
def test_a_class_that_forbids_extra_keys_reads_the_rows_it_saved_each_way(kind):
    async def steps(db, parent, child):
        kid = await _aw(child(label="one").save(db=db))
        saved = await _aw(parent(name="a", child=kid).save(db=db))
        saved.name = "b"
        await _aw(saved.save(db=db))
        got = [
            await _aw(parent.from_id(saved._id, db=db)),
            (await _aw(parent.from_ids([saved._id], db=db)))[0],
            await _aw(parent.using(db).first()),
            (await _aw(parent.using(db).all()))[0],
        ]
        again = await _aw(parent.from_id(saved._id, db=db))
        again.name = "stale"
        await _aw(again.refresh())
        got.append(again)
        return [(p._id, p.name, type(p.child).__name__, p.child._id, p.child.label) for p in got]

    assert _with_forbidding(kind, steps) == [(1, "b", "FbChild", 1, "one")] * 5


@pytest.mark.parametrize("kind", list(FORBIDDING))
def test_a_class_that_forbids_extra_keys_still_refuses_a_key_it_has_no_field_for(kind):
    async def steps(db, parent, child):
        await _aw(db.insert_document("fb_parent", {"name": "a", "child": None, "other": 1}))
        with pytest.raises(ValidationError) as caught:
            await _aw(parent.from_id(1, db=db))
        return [(e["loc"], e["type"]) for e in caught.value.errors()]

    assert _with_forbidding(kind, steps) == [(("other",), "extra_forbidden")]
