"""A model saved with ``save(db=db)`` and never bound can be referenced and searched.

With no ``set_db()`` call anywhere: a model holding another model is saved and
read back through ``using(db)`` with the related model in place, and a search
index given the database finds the row.
"""

import sqlite3
import warnings
from dataclasses import dataclass
from typing import Optional, Union

import pytest
import pytest_asyncio
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerModel,
    FTSIndex,
    SQLerDB,
    SQLerLiteModel,
    SQLerModel,
    SQLerMsgspecModel,
)
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.ref import as_ref
from sqler.models.safe import SQLerSafeModel


class UrPyAddress(SQLerModel):
    city: str = ""


class UrPyUser(SQLerModel):
    name: str = ""
    address: Optional[UrPyAddress] = None


@dataclass
class UrLiteAddress(SQLerLiteModel):
    city: str = ""


@dataclass
class UrLiteUser(SQLerLiteModel):
    name: str = ""
    address: Optional[UrLiteAddress] = None


class UrMsAddress(SQLerMsgspecModel):
    city: str = ""


class UrMsUser(SQLerMsgspecModel):
    name: str = ""
    address: Optional[UrMsAddress] = None


class UrAsAddress(AsyncSQLerModel):
    city: str = ""


class UrAsUser(AsyncSQLerModel):
    name: str = ""
    address: Optional[UrAsAddress] = None


@dataclass
class UrALiteAddress(AsyncSQLerLiteModel):
    city: str = ""


@dataclass
class UrALiteUser(AsyncSQLerLiteModel):
    name: str = ""
    address: Optional[UrALiteAddress] = None


class UrArticle(SQLerModel):
    title: str = ""
    body: str = ""


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    try:
        yield d
    finally:
        d.close()


@pytest_asyncio.fixture
async def adb():
    d = AsyncSQLerDB.in_memory(shared=False)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


@pytest.mark.parametrize(
    "user_cls,addr_cls,addr_table",
    [
        (UrPyUser, UrPyAddress, "urpyaddress"),
        (UrLiteUser, UrLiteAddress, "urliteaddress"),
        (UrMsUser, UrMsAddress, "urmsaddress"),
    ],
    ids=["pydantic", "lite", "msgspec"],
)
def test_unbound_model_holding_another_round_trips_through_using(db, user_cls, addr_cls, addr_table):
    addr = addr_cls(city="Kyoto").save(db=db)
    user_cls(name="Alice", address=addr).save(db=db)

    stored = user_cls.using(db).resolve(False).first()
    raw = db.find_document(user_cls._resolve_binding(db)[1], stored._id)
    assert raw["address"] == {"_table": addr_table, "_id": addr._id}

    got = user_cls.using(db).first()
    assert got is not None
    assert type(got.address) is addr_cls
    assert got.address.city == "Kyoto"
    assert got.name == "Alice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_cls,addr_cls,addr_table",
    [
        (UrAsUser, UrAsAddress, "urasaddress"),
        (UrALiteUser, UrALiteAddress, "uraliteaddress"),
    ],
    ids=["async-pydantic", "async-lite"],
)
async def test_async_unbound_model_holding_another_round_trips_through_using(
    adb, user_cls, addr_cls, addr_table
):
    addr = addr_cls(city="Kyoto")
    await addr.save(db=adb)
    user = user_cls(name="Alice", address=addr)
    await user.save(db=adb)

    raw = await adb.find_document(user_cls._resolve_binding(adb)[1], user._id)
    assert raw["address"] == {"_table": addr_table, "_id": addr._id}

    got = await user_cls.using(adb).first()
    assert got is not None
    assert type(got.address) is addr_cls
    assert got.address.city == "Kyoto"


def test_as_ref_of_an_unbound_saved_model_names_its_table(db):
    # "address" already ends in "s", so the default rule leaves it as it is
    addr = UrPyAddress(city="Osaka").save(db=db)
    assert as_ref(addr) == {"_table": "urpyaddress", "_id": addr._id}


def test_as_ref_of_an_unsaved_model_is_still_refused():
    with pytest.raises(ValueError, match="saved"):
        as_ref(UrPyAddress(city="nowhere"))


def test_search_index_given_the_database_finds_an_unbound_models_row(db):
    UrArticle(title="python tutorial", body="intro").save(db=db)
    UrArticle(title="rust guide", body="ownership").save(db=db)

    fts = FTSIndex(UrArticle, ["title", "body"], db=db)
    assert fts.table == "urarticles"
    fts.create()
    assert fts.rebuild() == 2

    ranked = fts.search_ranked("python")
    assert [r.model.title for r in ranked] == ["python tutorial"]
    assert fts.count("ownership") == 1


def test_a_synced_index_on_a_class_whose_table_does_not_exist_yet_creates_it(db):
    class UrNeverSaved(SQLerModel):
        title: str = ""

    fts = FTSIndex(UrNeverSaved, ["title"], db=db)
    assert fts.table == "urneversaveds"
    fts.create()
    assert _exists(db, "urneversaveds") is True
    assert fts.rebuild() == 0
    assert fts.search("anything") == []
    assert fts.count("anything") == 0


def test_a_manual_index_on_a_class_whose_table_does_not_exist_leaves_it_missing(db):
    class UrNeverSavedManual(SQLerModel):
        title: str = ""

    fts = FTSIndex(UrNeverSavedManual, ["title"], db=db, content_sync=False)
    fts.create()
    assert fts.rebuild() == 0
    assert fts.search("anything") == []
    assert fts.count("anything") == 0
    assert _exists(db, "urneversavedmanuals") is False


def test_from_ids_with_a_database_on_an_unbound_class(db):
    a = UrArticle(title="one").save(db=db)
    b = UrArticle(title="two").save(db=db)
    got = UrArticle.from_ids([b._id, a._id], db=db)
    assert sorted(x.title for x in got) == ["one", "two"]


def test_reference_to_a_class_defined_after_the_row_was_written(db):
    """The row names a table whose class does not exist yet; defining it later is enough."""

    @dataclass
    class UrLiteHolder(SQLerLiteModel):
        name: str = ""
        thing: "Optional[UrLateThing]" = None  # noqa: F821  (defined below, after the write)

    thing_id = db.insert_document("ur_late_things", {"label": "late"})
    UrLiteHolder(name="h", thing={"_table": "ur_late_things", "_id": thing_id}).save(db=db)

    @dataclass
    class UrLateThing(SQLerLiteModel):
        __tablename__ = "ur_late_things"
        label: str = ""

    got = UrLiteHolder.using(db).first()
    assert type(got.thing) is UrLateThing
    assert got.thing.label == "late"


# ----- a reference whose row cannot be read: sync and async agree ----------------------
#
# The holder is bound; the referenced class was never bound, so its row is read from
# the holder's database. When its table is missing, the stored reference stays the
# dict it was (and the table is not created); any other error reaches the caller.


def _bind(cls, db):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)


class UmPyPart(SQLerModel):
    label: str


class UmPyBox(SQLerModel):
    name: str = ""
    part: Union[dict, UmPyPart, None] = None  # a typed position that also takes a stored reference


@dataclass
class UmLitePart(SQLerLiteModel):
    label: str


@dataclass
class UmLiteBox(SQLerLiteModel):
    name: str = ""
    part: Union[dict, UmLitePart, None] = None  # a typed position that also takes a stored reference


class UmAsPart(AsyncSQLerModel):
    label: str


class UmAsBox(AsyncSQLerModel):
    name: str = ""
    part: Union[dict, UmAsPart, None] = None  # a typed position that also takes a stored reference


@dataclass
class UmALitePart(AsyncSQLerLiteModel):
    label: str


@dataclass
class UmALiteBox(AsyncSQLerLiteModel):
    name: str = ""
    part: Union[dict, UmALitePart, None] = None  # a typed position that also takes a stored reference


def _exists(db, table):
    cur = db.adapter.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table])
    return cur.fetchone() is not None


@pytest.mark.parametrize(
    "box_cls,part_cls", [(UmPyBox, UmPyPart), (UmLiteBox, UmLitePart)], ids=["pydantic", "lite"]
)
def test_reference_to_a_missing_table_stays_a_reference(db, box_cls, part_cls):
    _bind(box_cls, db)
    part = part_cls(label="p").save(db=db)
    part_table = part_cls._resolve_binding(db)[1]
    box = box_cls(name="b", part=part).save()
    db.adapter.execute(f"DROP TABLE {part_table}")

    ref = {"_table": part_table, "_id": part._id}
    assert box_cls.from_id(box._id).part == ref
    box.refresh()
    assert box.part == ref
    assert _exists(db, part_table) is False


@pytest.mark.parametrize(
    "box_cls,part_cls,error",
    [(UmPyBox, UmPyPart, ValueError), (UmLiteBox, UmLitePart, TypeError)],
    ids=["pydantic", "lite"],
)
def test_reference_whose_row_does_not_fit_its_class_raises(db, box_cls, part_cls, error):
    _bind(box_cls, db)
    part = part_cls(label="p").save(db=db)
    part_table = part_cls._resolve_binding(db)[1]
    box = box_cls(name="b", part=part).save()
    db.adapter.execute(f"UPDATE {part_table} SET data = json('{{}}') WHERE _id = ?", [part._id])
    db.adapter.commit()

    with pytest.raises(error, match="label"):
        box_cls.from_id(box._id)
    with pytest.raises(error, match="label"):
        box.refresh()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "box_cls,part_cls", [(UmAsBox, UmAsPart), (UmALiteBox, UmALitePart)], ids=["async-pydantic", "async-lite"]
)
async def test_async_reference_to_a_missing_table_stays_a_reference(adb, box_cls, part_cls):
    _bind(box_cls, adb)
    part = await part_cls(label="p").save(db=adb)
    part_table = part_cls._resolve_binding(adb)[1]
    box = await box_cls(name="b", part=part).save()
    await adb.adapter.execute(f"DROP TABLE {part_table}")
    await adb.adapter.auto_commit()

    ref = {"_table": part_table, "_id": part._id}
    assert (await box_cls.from_id(box._id)).part == ref
    await box.refresh()
    assert box.part == ref
    cur = await adb.adapter.execute("SELECT 1 FROM sqlite_master WHERE name=?", [part_table])
    assert await cur.fetchone() is None
    await cur.close()
    await adb.adapter.auto_commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "box_cls,part_cls,error",
    [(UmAsBox, UmAsPart, ValueError), (UmALiteBox, UmALitePart, TypeError)],
    ids=["async-pydantic", "async-lite"],
)
async def test_async_reference_whose_row_does_not_fit_its_class_raises(adb, box_cls, part_cls, error):
    _bind(box_cls, adb)
    part = await part_cls(label="p").save(db=adb)
    part_table = part_cls._resolve_binding(adb)[1]
    box = await box_cls(name="b", part=part).save()
    await adb.adapter.execute(f"UPDATE {part_table} SET data = json('{{}}') WHERE _id = ?", [part._id])
    await adb.adapter.auto_commit()

    with pytest.raises(error, match="label"):
        await box_cls.from_id(box._id)
    with pytest.raises(error, match="label"):
        await box.refresh()


# ----- a search index given a database loads its rows -----------------------------------


class UsArticle(SQLerModel):
    title: str = ""


@pytest.mark.parametrize("method", ["search", "search_with_highlights", "search_ranked"])
def test_every_search_on_an_unbound_class_returns_the_saved_row(db, method):
    saved = UsArticle(title="python tutorial").save(db=db)
    UsArticle(title="rust guide").save(db=db)
    fts = FTSIndex(UsArticle, ["title"], db=db)
    fts.create()
    assert fts.rebuild() == 2

    found = getattr(fts, method)("python")
    models = found if method == "search" else [r.model for r in found]
    assert [(type(m), m._id, m.title) for m in models] == [(UsArticle, saved._id, "python tutorial")]


@pytest.mark.parametrize("method", ["search", "search_with_highlights", "search_ranked"])
def test_every_search_on_a_missing_source_table_is_empty(db, method):
    class UsNever(SQLerModel):
        title: str = ""

    # an index without triggers: one with them creates the table it follows
    fts = FTSIndex(UsNever, ["title"], db=db, content_sync=False)
    fts.create()
    # an index row whose source row (and table) does not exist
    db.adapter.execute(f"INSERT INTO {fts.index_table}(rowid, title) VALUES (1, 'python')")
    db.adapter.commit()

    assert getattr(fts, method)("python") == []
    assert _exists(db, "usnevers") is False


def test_search_on_a_missing_index_table_still_raises(db):
    fts = FTSIndex(UsArticle, ["title"], db=db, index_name="us_no_index")
    with pytest.raises(sqlite3.OperationalError, match="no such table: us_no_index"):
        fts.search_ranked("python")


# ----- what docs/API.md says about a row read from a named table ------------------------


@pytest.mark.parametrize("cls", [UrPyAddress, UrLiteAddress, UrMsAddress], ids=["pydantic", "lite", "msgspec"])
def test_row_read_from_archive_names_archive_in_as_ref_and_in_a_holders_reference(db, cls):
    arch_id = db.insert_document("ur_archive", {"city": "Nara"})
    row = cls.using(db, "ur_archive").first()
    assert as_ref(row) == {"_table": "ur_archive", "_id": arch_id}


def test_holder_of_an_archive_row_stores_a_reference_to_archive(db):
    arch_id = db.insert_document("ur_archive", {"city": "Nara"})
    row = UrPyAddress.using(db, "ur_archive").first()
    user = UrPyUser(name="u", address=row).save(db=db)
    raw = db.find_document(UrPyUser._resolve_binding(db)[1], user._id)
    assert raw["address"] == {"_table": "ur_archive", "_id": arch_id}


@pytest.mark.parametrize("cls", [UrPyAddress, UrLiteAddress, UrMsAddress], ids=["pydantic", "lite", "msgspec"])
def test_row_read_from_archive_saved_to_another_db_writes_to_archive_there(db, cls):
    """save(db=other) updates the row with the same id in ``ur_archive`` of ``other``."""
    arch_id = db.insert_document("ur_archive", {"city": "Nara"})
    row = cls.using(db, "ur_archive").first()
    row.city = "Kobe"
    other = SQLerDB.in_memory(shared=False)
    try:
        assert other.insert_document("ur_archive", {"city": "old"}) == arch_id
        row.save(db=other)
        assert other.find_document("ur_archive", arch_id)["city"] == "Kobe"
        assert db.find_document("ur_archive", arch_id)["city"] == "Nara"
        assert _exists(other, cls._resolve_binding(other)[1]) is False
    finally:
        other.close()


# --- a search index and from_ids on the safe bases -------------------------------


class UsSafeNote(SQLerSafeModel):
    __tablename__ = "us_safe_notes"
    title: str = ""


@dataclass
class UsLiteSafeNote(SQLerLiteSafeModel):
    __tablename__ = "us_lite_safe_notes"
    title: str = ""


@dataclass
class UsALiteSafeNote(AsyncSQLerLiteSafeModel):
    __tablename__ = "us_alite_safe_notes"
    title: str = ""


def _bind(cls, db):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)


@pytest.mark.parametrize("cls", [UsSafeNote, UsLiteSafeNote], ids=["pydantic-safe", "lite-safe"])
@pytest.mark.parametrize("method", ["search", "search_with_highlights"])
def test_search_on_a_bound_safe_class_whose_index_was_created_with_a_database(db, cls, method):
    _bind(cls, db)
    saved = cls(title="python tutorial").save()
    cls(title="rust guide").save()
    fts = FTSIndex(cls, ["title"])
    fts.create(db)
    assert fts.rebuild() == 2

    found = getattr(fts, method)("python")
    models = found if method == "search" else [r.model for r in found]
    assert [(type(m), m._id, m.title, m._version) for m in models] == [
        (cls, saved._id, "python tutorial", 0)
    ]


def test_lite_safe_from_ids_with_a_database_on_an_unbound_class(db):
    @dataclass
    class UsLiteSafeLoose(SQLerLiteSafeModel):
        title: str = ""

    first = UsLiteSafeLoose(title="a").save(db=db)
    second = UsLiteSafeLoose(title="b").save(db=db)

    rows = UsLiteSafeLoose.from_ids([second._id, first._id], db=db)
    assert [(type(r), r._id, r.title, r._version) for r in rows] == [
        (UsLiteSafeLoose, second._id, "b", 0),
        (UsLiteSafeLoose, first._id, "a", 0),
    ]


@pytest.mark.asyncio
async def test_async_lite_safe_from_ids_with_a_database_on_an_unbound_class(adb):
    first = await UsALiteSafeNote(title="a").save(db=adb)
    second = await UsALiteSafeNote(title="b").save(db=adb)

    rows = await UsALiteSafeNote.from_ids([second._id, first._id], db=adb)
    assert [(type(r), r._id, r.title, r._version) for r in rows] == [
        (UsALiteSafeNote, second._id, "b", 0),
        (UsALiteSafeNote, first._id, "a", 0),
    ]
