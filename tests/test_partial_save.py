"""save_partial(): a write of the changed fields of one row, through the row's own save().

Every case compares the row a partial save leaves with the row a full ``save()`` of the same
change leaves: the stored form of each value, where the row lives, the promoted columns, the
version check, and what the mixins around ``save()`` do.
"""

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

import pytest
from pydantic import BaseModel, create_model
from sqler import SQLerDB, SQLerModel, SQLerSafeModel
from sqler.cache import CacheAwareModel, cached_query, get_cache
from sqler.exceptions import (
    InvalidFieldNameError,
    NotBoundError,
    NotFoundError,
    StaleVersionError,
    UnstorableValueError,
)
from sqler.fts import FTSIndex, SearchableMixin
from sqler.logging import query_logger
from sqler.models.lite import SQLerLiteModel, SQLerLiteSafeModel
from sqler.models.mixins import AuditLogMixin, HooksMixin, TimestampMixin
from sqler.models.utils import RebaseConfig
from sqler.tracking import PartialUpdateMixin, TrackedModel


class PsOwner(SQLerModel):
    __tablename__ = "ps_owners"
    name: str = ""


class PsAddress(BaseModel):
    city: str = ""


class PsDoc(PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_docs"
    key: str = ""
    value: str = "v"
    tags: list = []
    flag: bool = False
    at: Optional[datetime] = None
    price: Optional[Decimal] = None
    took: Optional[timedelta] = None
    pair: Optional[tuple[int, int]] = None
    note: Optional[str] = "n"
    owner: Optional[PsOwner] = None
    owners: list[PsOwner] = []
    address: Optional[PsAddress] = None
    blob: Optional[bytes] = None
    thing: Any = None


class PsSafeDoc(PsDoc, SQLerSafeModel):
    __tablename__ = "ps_safe_docs"


@dataclass
class PsLiteDoc(PartialUpdateMixin, TrackedModel, SQLerLiteModel):
    __tablename__ = "ps_lite_docs"
    key: str = ""
    value: str = "v"
    tags: list = field(default_factory=list)
    flag: bool = False
    at: Optional[datetime] = None
    price: Optional[Decimal] = None
    took: Optional[timedelta] = None
    pair: Optional[tuple] = None
    note: Optional[str] = "n"


@dataclass
class PsLiteSafeDoc(PsLiteDoc, SQLerLiteSafeModel):
    __tablename__ = "ps_lite_safe_docs"


class PsPromo(PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_promos"
    __promoted__ = {"title": "TEXT", "image": "BLOB"}
    title: str = ""
    body: str = ""
    image: Optional[bytes] = None


class PsSafePromo(PsPromo, SQLerSafeModel):
    __tablename__ = "ps_safe_promos"


BASES = [PsDoc, PsSafeDoc, PsLiteDoc, PsLiteSafeDoc]
VERSIONED = [PsSafeDoc, PsLiteSafeDoc]
UNVERSIONED = [PsDoc, PsLiteDoc]

# (field, value a change sets, the value the row's JSON holds for it); every base has these fields
KINDS = [
    ("value", "new", "new"),
    ("tags", ["a", "b"], ["a", "b"]),
    ("flag", True, True),
    ("at", datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05"),
    ("price", Decimal("0.1"), 0.1),
    ("took", timedelta(minutes=90), 5400),
    ("pair", (1, 2), [1, 2]),
    ("note", None, None),
]


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    yield d
    d.close()


def _row(db, table, rid):
    """The stored row, every column but ``_id``."""
    cur = db.adapter.execute(f"SELECT * FROM {table} WHERE _id = ?", [rid])
    r = cur.fetchone()
    return None if r is None else {k: r[k] for k in r.keys() if k != "_id"}


def _load(cls, db, rid):
    found = [m for m in cls.using(db).all() if m._id == rid]
    assert len(found) == 1
    return found[0]


def _values(model):
    return {name: getattr(model, name) for name in type(model).model_fields}


def _twins(cls, db, **fields):
    """Two equal rows: one for a partial save, one for a full save of the same change."""
    return cls(key="k", **fields).save(db=db), cls(key="k", **fields).save(db=db)


def _assert_twins_equal(cls, db, partial, full):
    table = cls.__tablename__
    assert _row(db, table, partial._id) == _row(db, table, full._id)
    assert _values(_load(cls, db, partial._id)) == _values(_load(cls, db, full._id))


# ---- 1. each value as save() stores it, where save() stores it ----


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("name,value,in_json", KINDS, ids=[k[0] for k in KINDS])
def test_a_partial_save_stores_each_kind_of_value_as_save_does(db, cls, name, value, in_json):
    partial, full = _twins(cls, db)
    setattr(partial, name, value)
    setattr(full, name, value)
    partial.save_partial(db=db)
    full.save(db=db)
    _assert_twins_equal(cls, db, partial, full)
    assert json.loads(_row(db, cls.__tablename__, partial._id)["data"])[name] == in_json


@pytest.mark.parametrize("cls", [PsDoc, PsSafeDoc], ids=lambda c: c.__name__)
@pytest.mark.parametrize("kind", ["reference", "list of references", "nested model"])
def test_a_partial_save_stores_references_and_nested_models_as_save_does(db, cls, kind):
    first = PsOwner(name="a").save(db=db)
    second = PsOwner(name="b").save(db=db)
    name, value = {
        "reference": ("owner", first),
        "list of references": ("owners", [first, second]),
        "nested model": ("address", PsAddress(city="Kyoto")),
    }[kind]
    partial, full = _twins(cls, db)
    setattr(partial, name, value)
    setattr(full, name, value)
    partial.save_partial(db=db)
    full.save(db=db)
    _assert_twins_equal(cls, db, partial, full)


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_two_fields_changed_together_are_one_statement(db, cls):
    partial, full = _twins(cls, db)
    for m in (partial, full):
        m.value, m.flag = "new", True
    with query_logger.capture() as sent:
        partial.save_partial(db=db)
    full.save(db=db)
    assert [q.sql.split()[0] for q in sent if q.sql.lstrip().upper().startswith("UPDATE")] == ["UPDATE"]
    _assert_twins_equal(cls, db, partial, full)


@pytest.mark.parametrize("cls", [PsDoc, PsSafeDoc], ids=lambda c: c.__name__)
def test_bytes_in_a_json_field_is_refused_naming_the_field(db, cls):
    m = cls(key="k").save(db=db)
    before = _row(db, cls.__tablename__, m._id)
    m.blob = b"\x00\x01"
    with pytest.raises(UnstorableValueError) as err:
        m.save_partial(db=db)
    assert err.value.field == "blob"
    assert _row(db, cls.__tablename__, m._id) == before
    assert m.changed_fields == {"blob"}


@pytest.mark.parametrize("cls", [PsDoc, PsSafeDoc], ids=lambda c: c.__name__)
def test_a_value_with_no_stored_form_is_refused_naming_its_place(db, cls):
    m = cls(key="k").save(db=db)
    m.thing = {"inner": [1, object()]}
    with pytest.raises(UnstorableValueError) as err:
        m.save_partial(db=db)
    assert err.value.field == "thing.inner[1]"


@pytest.mark.parametrize("cls", [PsPromo, PsSafePromo], ids=lambda c: c.__name__)
@pytest.mark.parametrize(
    "change",
    [{"title": "t2"}, {"title": "t2", "body": "b2"}, {"image": b"\x89PNG\x00"}],
    ids=["promoted alone", "promoted and plain", "promoted BLOB"],
)
def test_a_promoted_field_is_written_to_its_column_as_save_does(db, cls, change):
    partial = cls(title="t1", body="b1").save(db=db)
    full = cls(title="t1", body="b1").save(db=db)
    for m in (partial, full):
        for k, v in change.items():
            setattr(m, k, v)
    with query_logger.capture() as sent:
        partial.save_partial(db=db)
    full.save(db=db)
    assert len([q for q in sent if q.sql.lstrip().upper().startswith("UPDATE")]) == 1
    row = _row(db, cls.__tablename__, partial._id)
    assert row == _row(db, cls.__tablename__, full._id)
    for k, v in change.items():
        assert (row[k] if k in cls.__promoted__ else json.loads(row["data"])[k]) == v
    assert _values(_load(cls, db, partial._id)) == _values(_load(cls, db, full._id))


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_no_changed_field_sends_no_statement(db, cls):
    m = cls(key="k").save(db=db)
    with query_logger.capture() as sent:
        m.save_partial(db=db)
    assert sent == []


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_a_field_changed_and_changed_back_sends_no_statement(db, cls):
    m = cls(key="k").save(db=db)
    m.value = "other"
    m.value = "v"
    with query_logger.capture() as sent:
        m.save_partial(db=db)
    assert sent == []


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_a_list_changed_in_place_is_written(db, cls):
    m = cls(key="k", tags=["a"]).save(db=db)
    m.tags.append("b")
    m.save_partial(db=db)
    assert _load(cls, db, m._id).tags == ["a", "b"]


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_a_partial_save_of_one_field_leaves_the_others_of_the_row(db, cls):
    m = cls(key="k").save(db=db)
    db.adapter.execute(
        f"UPDATE {cls.__tablename__} SET data = json_set(data, '$.note', 'theirs') WHERE _id = ?",
        [m._id],
    )
    db.adapter.auto_commit()
    m.value = "mine"
    m.save_partial(db=db)  # on a versioned base too: that write did not move the version
    loaded = _load(cls, db, m._id)
    assert (loaded.value, loaded.note) == ("mine", "theirs")


def test_a_row_read_from_another_table_is_written_there(db):
    PsDoc(key="mine").save(db=db)  # the class's own table
    db._ensure_table("other_docs")
    db.insert_document("other_docs", PsDoc(key="theirs")._dump_with_relations())
    m = PsDoc.using(db, "other_docs").first()
    m.value = "changed"
    m.save_partial(db=db)
    assert [d.value for d in PsDoc.using(db, "other_docs").all()] == ["changed"]
    assert [d.value for d in PsDoc.using(db).all()] == ["v"]


class PsNeverBound(PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_never_bound"
    name: str = ""


def test_a_class_never_bound_is_partially_saved_with_db_and_refused_without(db):
    m = PsNeverBound(name="a").save(db=db)
    m.name = "b"
    with pytest.raises(NotBoundError) as by_save:
        PsNeverBound(name="x").save()
    with pytest.raises(NotBoundError) as by_partial:
        m.save_partial()
    assert str(by_partial.value) == str(by_save.value)
    m.save_partial(db=db)
    assert [d.name for d in PsNeverBound.using(db).all()] == ["b"]


_ODD_NAMES = ["a.b", "c'd", "名前", "x y", "$d", "[0]", "e-f"]


def _odd_class():
    # a class body cannot declare most of these names; create_model can, and a full save stores them
    return create_model(
        "PsOdd",
        __base__=(PartialUpdateMixin, TrackedModel, SQLerModel),
        **{name: (str, "") for name in _ODD_NAMES},
    )


@pytest.mark.parametrize("name", _ODD_NAMES)
def test_a_field_whatever_its_name_is_written_as_save_writes_it(db, name):
    Odd = _odd_class()
    start = {n: "v" for n in _ODD_NAMES}
    partial, full = Odd(**start).save(db=db), Odd(**start).save(db=db)
    for row in (partial, full):
        setattr(row, name, "z")
    partial.save_partial(db=db)
    full.save(db=db)
    assert json.loads(_row(db, "psodds", partial._id)["data"]) == {**start, name: "z"}
    assert _row(db, "psodds", partial._id) == _row(db, "psodds", full._id)


@pytest.mark.parametrize("name", ['g"h', "i\\j"])
def test_a_field_name_with_a_double_quote_or_a_backslash_is_refused_before_any_statement(db, name):
    # no JSON path every supported SQLite reads the same way names such a key
    Odd = create_model(
        "PsOdder", __base__=(PartialUpdateMixin, TrackedModel, SQLerModel), **{name: (str, ""), "plain": (str, "")}
    )
    m = Odd(**{name: "x"}).save(db=db)
    before = _row(db, "psodders", m._id)
    m2 = _load(Odd, db, m._id)
    setattr(m2, name, "z")
    with query_logger.capture() as sent, pytest.raises(InvalidFieldNameError) as refused:
        m2.save_partial(db=db)
    assert refused.value.field == name
    assert [q.sql for q in sent if q.sql.lstrip().upper().startswith("UPDATE")] == []
    assert _row(db, "psodders", m._id) == before
    assert m2.changed_fields == {name}
    m2.save(db=db)  # a full save stores it
    assert json.loads(_row(db, "psodders", m._id)["data"]) == {name: "z", "plain": ""}


class PsLegacy(PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_legacies"
    name: str = ""
    added: str = ""


def test_a_key_the_model_has_no_field_for_is_left_by_a_partial_save(db):
    """A row written by another version of the program: a partial save writes the fields
    that differ from the row (also a field of the model the row did not hold yet) and leaves
    a key it has no field for; a full save writes the model's own document."""
    PsLegacy(name="seed").save(db=db)  # makes the table
    rid = db.insert_document("ps_legacies", {"name": "a", "legacy": 1})
    m = _load(PsLegacy, db, rid)
    m.name = "b"
    m.save_partial(db=db)
    assert json.loads(_row(db, "ps_legacies", rid)["data"]) == {"name": "b", "legacy": 1, "added": ""}
    m.added = "x"
    m.save_partial(db=db)
    assert json.loads(_row(db, "ps_legacies", rid)["data"]) == {"name": "b", "legacy": 1, "added": "x"}
    m.name = "c"
    m.save(db=db)
    assert json.loads(_row(db, "ps_legacies", rid)["data"]) == {"name": "c", "added": "x"}


class PsHolder(PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_holders"
    owner: Optional[PsOwner] = None


def test_a_related_model_never_saved_is_refused_as_save_refuses_it(db):
    partial, full = PsHolder().save(db=db), PsHolder().save(db=db)
    partial.owner, full.owner = PsOwner(name="new"), PsOwner(name="new")
    with pytest.raises(ValueError) as by_save:
        full.save(db=db)
    with pytest.raises(ValueError) as by_partial:
        partial.save_partial(db=db)
    assert str(by_partial.value) == str(by_save.value)
    assert partial.changed_fields == {"owner"}


class PsSubDoc(PsDoc):
    __tablename__ = "ps_sub_docs"
    extra: str = ""


def test_a_subclass_field_is_written(db):
    partial, full = _twins(PsSubDoc, db)
    partial.extra, full.extra = "x", "x"
    partial.save_partial(db=db)
    full.save(db=db)
    assert _values(_load(PsSubDoc, db, partial._id)) == _values(_load(PsSubDoc, db, full._id))
    assert _load(PsSubDoc, db, partial._id).extra == "x"


# ---- 2. the version check ----


@pytest.mark.parametrize("cls", VERSIONED, ids=lambda c: c.__name__)
def test_a_stale_partial_save_is_refused_as_a_stale_save(db, cls):
    partial, full = _twins(cls, db)
    for m in (partial, full):
        other = _load(cls, db, m._id)
        other.flag = True
        other.save(db=db)
        m.value = "stale"
    with pytest.raises(StaleVersionError) as by_save:
        full.save(db=db)
    with pytest.raises(StaleVersionError) as by_partial:
        partial.save_partial(db=db)
    assert str(by_partial.value) == str(by_save.value)
    assert _row(db, cls.__tablename__, partial._id)["_version"] == 1
    assert partial.changed_fields == {"value"}


@pytest.mark.parametrize("cls", VERSIONED, ids=lambda c: c.__name__)
def test_a_partial_save_bumps_the_version_as_save_does(db, cls):
    partial, full = _twins(cls, db)
    for m in (partial, full):
        m.value = "new"
    partial.save_partial(db=db)
    full.save(db=db)
    assert partial._version == full._version == 1
    assert _row(db, cls.__tablename__, partial._id)["_version"] == 1
    # the next stale full save is caught
    old = _load(cls, db, partial._id)
    partial.value = "again"
    partial.save_partial(db=db)
    old.flag = True
    with pytest.raises(StaleVersionError):
        old.save(db=db)


@pytest.mark.parametrize("cls", UNVERSIONED, ids=lambda c: c.__name__)
def test_two_instances_that_change_other_fields_both_land_on_a_plain_base(db, cls):
    m = cls(key="k").save(db=db)
    a, b = _load(cls, db, m._id), _load(cls, db, m._id)
    a.value = "from a"
    b.flag = True
    a.save_partial(db=db)
    b.save_partial(db=db)
    loaded = _load(cls, db, m._id)
    assert (loaded.value, loaded.flag) == ("from a", True)


@pytest.mark.parametrize("cls", VERSIONED, ids=lambda c: c.__name__)
def test_two_instances_that_change_other_fields_second_is_stale_on_a_versioned_base(db, cls):
    m = cls(key="k").save(db=db)
    a, b = _load(cls, db, m._id), _load(cls, db, m._id)
    a.value = "from a"
    b.flag = True
    a.save_partial(db=db)
    with pytest.raises(StaleVersionError):
        b.save_partial(db=db)
    loaded = _load(cls, db, m._id)
    assert (loaded.value, loaded.flag) == ("from a", False)


class PsCounter(PartialUpdateMixin, TrackedModel, SQLerSafeModel):
    __tablename__ = "ps_counters"
    _rebase_config = RebaseConfig(enabled=True, allowed_fields={"hits"}, max_delta=5)
    hits: int = 0
    label: str = ""


def test_a_stale_partial_save_of_a_counter_is_rebased_as_save_rebases_it(db):
    partial, full = PsCounter().save(db=db), PsCounter().save(db=db)
    for m in (partial, full):
        other = _load(PsCounter, db, m._id)
        other.hits = 10
        other.save(db=db)
        m.hits = 2
    full.save(db=db)
    partial.save_partial(db=db)
    assert _row(db, "ps_counters", partial._id) == _row(db, "ps_counters", full._id)
    assert (partial.hits, partial._version) == (full.hits, full._version) == (12, 2)


# ---- 3. a row that is gone, a model never saved ----


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_a_partial_save_of_a_row_that_is_gone_raises_not_found(db, cls):
    m = cls(key="k").save(db=db)
    db.adapter.execute(f"DELETE FROM {cls.__tablename__} WHERE _id = ?", [m._id])
    db.adapter.auto_commit()
    m.value = "new"
    with pytest.raises(NotFoundError):
        m.save_partial(db=db)
    assert _row(db, cls.__tablename__, m._id) is None
    assert m.changed_fields == {"value"}
    assert m.get_dirty_fields() == {"value"}


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_a_model_never_saved_is_saved_in_full_once(db, cls):
    m = cls(key="k", value="first")
    with query_logger.capture() as sent:
        m.save_partial(db=db)
    assert len([q for q in sent if q.sql.lstrip().upper().startswith("INSERT")]) == 1
    assert _load(cls, db, m._id).value == "first"
    assert m.is_dirty is False


# ---- 5. clean by both answers ----


@pytest.mark.parametrize("cls", BASES, ids=lambda c: c.__name__)
def test_after_a_partial_save_the_model_is_clean_by_both_answers(db, cls):
    m, full = _twins(cls, db)
    m.value = full.value = "new"
    m.save_partial(db=db)
    full.save(db=db)
    assert m.is_dirty is False
    assert m.changed_fields == set()
    assert m.get_dirty_fields() == set()
    # a full save straight after sends what the save of a model a full save left sends
    with query_logger.capture() as after_partial:
        m.save(db=db)
    with query_logger.capture() as after_full:
        full.save(db=db)
    assert len(after_partial) == len(after_full) >= 1
    assert [(q.sql, q.params[:1]) for q in after_partial] == [(q.sql, q.params[:1]) for q in after_full]


def test_is_dirty_stays_a_property_on_a_tracked_model(db):
    m = PsDoc(key="k").save(db=db)
    assert m.is_dirty is False
    with pytest.raises(TypeError, match="'bool' object is not callable"):
        m.is_dirty()
    assert SQLerModel.is_dirty(m) is False
    m.value = "new"
    assert m.is_dirty is True
    assert SQLerModel.is_dirty(m) is True


def test_a_partial_save_rolled_back_leaves_the_model_as_save_leaves_it(db):
    partial, full = _twins(PsDoc, db)
    before = _row(db, "ps_docs", partial._id)
    for m, write in ((partial, lambda m: m.save_partial(db=db)), (full, lambda m: m.save(db=db))):
        m.value = "rolled back"
        with pytest.raises(RuntimeError):
            with db.transaction():
                write(m)
                raise RuntimeError("undo")
    assert _row(db, "ps_docs", partial._id) == before
    assert _row(db, "ps_docs", full._id) == before
    assert (partial.is_dirty, SQLerModel.is_dirty(partial)) == (full.is_dirty, SQLerModel.is_dirty(full))
    assert (partial.is_dirty, SQLerModel.is_dirty(partial)) == (False, False)


def test_two_threads_that_each_change_another_field_both_land(tmp_path):
    path = str(tmp_path / "threads.db")
    setup = SQLerDB.on_disk(path)
    rid = PsDoc(key="k").save(db=setup)._id
    setup.close()
    loaded = threading.Barrier(2)
    errors = []

    def change(name, value):
        db = SQLerDB.on_disk(path)
        try:
            m = _load(PsDoc, db, rid)
            loaded.wait()
            setattr(m, name, value)
            m.save_partial(db=db)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=change, args=a) for a in (("value", "t1"), ("flag", True))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    check = SQLerDB.on_disk(path)
    loaded_row = _load(PsDoc, check, rid)
    assert (loaded_row.value, loaded_row.flag) == ("t1", True)
    check.close()


# ---- 4. the wrappers around save() ----


class PsStamped(TimestampMixin, AuditLogMixin, PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_stamped"
    __promoted__ = {"title": "TEXT"}
    title: str = ""
    body: str = ""


class PsStampedFirst(PartialUpdateMixin, TrackedModel, TimestampMixin, AuditLogMixin, SQLerModel):
    __tablename__ = "ps_stamped_first"
    __promoted__ = {"title": "TEXT"}
    title: str = ""
    body: str = ""


@pytest.mark.parametrize("cls", [PsStamped, PsStampedFirst], ids=lambda c: c.__name__)
def test_timestamps_and_the_audit_log_hold_for_a_partial_save(db, cls):
    m = cls(title="t", body="b").save(db=db)
    stamped = m.updated_at
    m.body = "b2"
    m.title = "t2"
    m.save_partial(db=db)
    assert m.updated_at > stamped
    loaded = _load(cls, db, m._id)
    assert (loaded.updated_at, loaded.title, loaded.body) == (m.updated_at, "t2", "b2")
    log = m.get_audit_log(db=db)
    assert [e["action"] for e in log] == ["update", "create"]
    assert set(log[0]["changes"]) == {"body", "title", "updated_at"}
    assert log[0]["changes"]["body"] == {"old": "b", "new": "b2"}
    assert log[0]["snapshot"]["title"] == "t2"
    assert (m.is_dirty, SQLerModel.is_dirty(m)) == (False, False)


def test_the_audit_row_lists_only_what_the_partial_save_wrote(db):
    m = PsStamped(title="t", body="b").save(db=db)
    other = _load(PsStamped, db, m._id)
    other.title = "theirs"
    other.save(db=db)
    m.body = "mine"
    m.save_partial(db=db)
    changes = m.get_audit_log(db=db)[0]["changes"]
    assert set(changes) == {"body", "updated_at"}
    assert _load(PsStamped, db, m._id).title == "theirs"


def test_a_partial_save_and_its_audit_row_roll_back_together(db):
    m = PsStamped(title="t", body="b").save(db=db)
    before = _row(db, "ps_stamped", m._id)
    m.body = "b2"
    with pytest.raises(RuntimeError):
        with db.transaction():
            m.save_partial(db=db)
            raise RuntimeError("undo")
    assert _row(db, "ps_stamped", m._id) == before
    assert [e["action"] for e in m.get_audit_log(db=db)] == ["create"]


class PsHooked(HooksMixin, PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_hooked"
    name: str = ""

    def before_save(self):
        type(self).seen.append("before")
        return True

    def after_save(self):
        type(self).seen.append("after")


def test_hooks_run_for_a_partial_save(db):
    PsHooked.seen = []
    m = PsHooked(name="a").save(db=db)
    m.name = "b"
    m.save_partial(db=db)
    assert PsHooked.seen == ["before", "after", "before", "after"]


class PsCached(CacheAwareModel, PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_cached"
    value: str = ""

    class Meta:
        cache_table = "ps_cached"


def test_a_cached_read_after_a_partial_save_returns_the_new_value(db):
    m = PsCached(value="old").save(db=db)

    @cached_query(table="ps_cached", key="ps_cached_value")
    def read():
        return _load(PsCached, db, m._id).value

    get_cache().invalidate("ps_cached_value")
    assert read() == "old"
    m.value = "new"
    m.save_partial(db=db)
    assert read() == "new"


class PsManualArticle(SearchableMixin, PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_manual_articles"
    title: str = ""
    body: str = ""


class PsSyncedArticle(SearchableMixin, PartialUpdateMixin, TrackedModel, SQLerModel):
    __tablename__ = "ps_synced_articles"
    title: str = ""
    body: str = ""


@pytest.mark.parametrize("cls,content_sync", [(PsManualArticle, False), (PsSyncedArticle, True)])
def test_the_search_index_finds_the_text_of_a_partial_save(db, cls, content_sync):
    cls._fts_index = FTSIndex(cls, ["title", "body"], db=db, content_sync=content_sync)
    cls._fts_index.create(db)
    try:
        m = cls(title="t", body="apple").save(db=db)
        m.body = "zebra"
        m.save_partial(db=db)
        assert [a._id for a in cls._fts_index.search("zebra")] == [m._id]
        assert cls._fts_index.search("apple") == []
    finally:
        cls._fts_index = None


# ---- 6. the sync classes are sync ----


def test_the_sync_classes_have_no_async_methods():
    for name in ("asave", "afrom_id"):
        assert not hasattr(TrackedModel, name)
    assert not hasattr(PartialUpdateMixin, "asave_partial")
