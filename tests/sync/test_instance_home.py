"""The home of an instance (the database and table of its row) does not travel and does not
show: a copy keeps the same database object, a pickle drops it, and no dump, comparison or
stored document sees it. On a pydantic, a frozen lite, a slots lite and a msgspec class."""

import copy
import dataclasses
import json
import pickle
from dataclasses import dataclass

import msgspec
import pytest

from sqler import SQLerDB
from sqler.exceptions import NotBoundError
from sqler.models import SQLerLiteModel, SQLerModel
from sqler.models.msgspec import SQLerMsgspecModel


class HomePy(SQLerModel):
    __tablename__ = "home_py"
    name: str = ""


@dataclass(frozen=True)
class HomeFrozen(SQLerLiteModel):
    __tablename__ = "home_frozen"
    name: str = ""


@dataclass(slots=True)
class HomeSlots(SQLerLiteModel):
    __tablename__ = "home_slots"
    name: str = ""


@dataclass(frozen=True, slots=True)
class HomeFrozenSlots(SQLerLiteModel):
    __tablename__ = "home_frozen_slots"
    name: str = ""


class HomeMsg(SQLerMsgspecModel):
    __tablename__ = "home_msg"
    name: str = ""


CLASSES = [HomePy, HomeFrozen, HomeSlots, HomeMsg]
# a frozen class with slots gets a ``__getstate__`` from ``@dataclass``: copied as the others
COPIED = [*CLASSES, HomeFrozenSlots]


def renamed(inst, name):
    """``inst`` with ``name`` set, frozen or not."""
    if type(inst) in (HomeFrozen, HomeFrozenSlots):
        object.__setattr__(inst, "name", name)
    else:
        inst.name = name
    return inst


def copies(cls):
    """Each way of copying an instance of ``cls`` that keeps its ``_id``."""
    ways = {"copy.copy": copy.copy, "copy.deepcopy": copy.deepcopy}
    if cls is HomePy:
        ways["model_copy()"] = lambda m: m.model_copy()
        ways["model_copy(deep=True)"] = lambda m: m.model_copy(deep=True)
    return ways


COPIES = [(cls, way) for cls in COPIED for way in copies(cls)]


@pytest.fixture
def dbs():
    a, b = SQLerDB.in_memory(shared=False), SQLerDB.in_memory(shared=False)
    yield a, b
    a.close()
    b.close()


def read_from(cls, db):
    """Row 1 of ``cls`` in ``db``, written there and read back with ``using(db)``."""
    cls(name="stored").save(db=db)
    return cls.using(db).first()


@pytest.mark.parametrize(("cls", "way"), COPIES, ids=lambda x: getattr(x, "__name__", x))
def test_a_copy_saves_to_the_database_its_original_came_from(dbs, cls, way):
    a, _ = dbs
    got = read_from(cls, a)
    twin = copies(cls)[way](got)
    assert twin._id == 1
    renamed(twin, "from the copy").save()
    assert a.find_document(cls.__tablename__, 1)["name"] == "from the copy"


@pytest.mark.parametrize(("cls", "way"), COPIES, ids=lambda x: getattr(x, "__name__", x))
def test_a_copy_holds_the_row_id_the_snapshot_and_the_home_of_its_original(dbs, cls, way):
    a, _ = dbs
    got = read_from(cls, a)
    twin = copies(cls)[way](got)
    assert (twin._id, twin._snapshot, vars(twin)["_home"].db is a) == (1, {"name": "stored"}, True)
    renamed(twin, "from the copy").save()  # the class is bound to no database: the home takes it
    assert [d["name"] for d in a.find_documents(cls.__tablename__, [1, 2])] == ["from the copy"]


@pytest.mark.parametrize("cls", COPIED, ids=lambda c: c.__name__)
def test_a_deep_copy_holds_the_same_database_object(dbs, cls):
    a, b = dbs
    got = read_from(cls, a)
    twin = copy.deepcopy(got)
    renamed(twin, "deep").save()
    # the copy wrote through the database object itself: a copy of it would be a second
    # connection to a private in-memory database, and a would not hold the row
    assert (
        a.find_document(cls.__tablename__, 1)["name"],
        b.find_document(cls.__tablename__, 1),
    ) == (
        "deep",
        None,
    )


@pytest.mark.parametrize("cls", [HomeFrozen, HomeSlots], ids=lambda c: c.__name__)
def test_dataclasses_replace_gives_a_new_instance_with_no_row(dbs, cls):
    a, _ = dbs
    got = read_from(cls, a)
    fresh = dataclasses.replace(got, name="replaced")
    assert (fresh.name, fresh._id, vars(fresh).get("_home")) == ("replaced", None, None)


@pytest.mark.parametrize("cls", CLASSES, ids=lambda c: c.__name__)
def test_a_pickle_round_trip_keeps_the_row_id_and_drops_the_home(dbs, cls):
    a, _ = dbs
    got = read_from(cls, a)
    back = pickle.loads(pickle.dumps(got))
    assert back == got
    assert back._id == 1
    with pytest.raises(NotBoundError):  # no home, and the class is bound to no database
        renamed(back, "unpickled").save()
    assert a.find_document(cls.__tablename__, 1)["name"] == "stored"


@pytest.mark.parametrize("cls", CLASSES, ids=lambda c: c.__name__)
def test_the_home_is_in_no_comparison_dump_or_stored_document(dbs, cls):
    a, b = dbs
    from_a = read_from(cls, a)
    from_b = read_from(cls, b)
    assert from_a == from_b
    assert repr(from_a) == repr(from_b)
    assert from_a.model_dump() == {"name": "stored"}
    assert json.loads(a.adapter.execute(f"SELECT data FROM {cls.__tablename__}").fetchone()[0]) == {
        "name": "stored"
    }
    if cls is HomePy:
        assert from_a.model_dump_json() == '{"name":"stored"}'
    elif cls is HomeMsg:
        assert msgspec.to_builtins(from_a) == {
            "name": "stored",
            "_id": 1,
            "_snapshot": {"name": "stored"},
        }
    else:
        assert dataclasses.asdict(from_a) == {"name": "stored"}


@pytest.mark.parametrize("cls", [HomeFrozen], ids=lambda c: c.__name__)
def test_a_frozen_instance_hashes_as_before(dbs, cls):
    a, b = dbs
    assert hash(read_from(cls, a)) == hash(read_from(cls, b)) == hash(cls(name="stored"))
