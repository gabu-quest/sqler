"""The async twin of ``tests/test_rows_read_by_id.py``.

A row read by its id through ``AsyncSQLerDB`` and the async bases is the whole row: its
promoted columns, and for a versioned table its version, as ``find_document`` gives them.
"""

import sqlite3
import warnings
from dataclasses import dataclass
from typing import Any, Optional

import pytest
import pytest_asyncio
from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
)
from sqler.logging import query_logger

pytestmark = pytest.mark.asyncio


class ArbiJob(AsyncSQLerModel):
    __tablename__ = "arbi_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


class ArbiHolder(AsyncSQLerModel):
    __tablename__ = "arbi_holders"
    name: str = ""
    typed: Optional[ArbiJob] = None
    untyped: Any = None


class ArbiSafeJob(AsyncSQLerSafeModel):
    __tablename__ = "arbi_safe_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


class ArbiSafeHolder(AsyncSQLerModel):
    __tablename__ = "arbi_safe_holders"
    name: str = ""
    typed: Optional[ArbiSafeJob] = None
    untyped: Any = None


@dataclass
class ArbiLiteJob(AsyncSQLerLiteModel):
    __tablename__ = "arbi_lite_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


@dataclass
class ArbiLiteHolder(AsyncSQLerLiteModel):
    __tablename__ = "arbi_lite_holders"
    name: str = ""
    untyped: Any = None


@dataclass
class ArbiLiteSafeJob(AsyncSQLerLiteSafeModel):
    __tablename__ = "arbi_lite_safe_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


@dataclass
class ArbiLiteSafeHolder(AsyncSQLerLiteModel):
    __tablename__ = "arbi_lite_safe_holders"
    name: str = ""
    untyped: Any = None


class ArbiPlain(AsyncSQLerModel):
    __tablename__ = "arbi_plains"
    name: str = ""


class ArbiSafe(AsyncSQLerSafeModel):
    __tablename__ = "arbi_safes"
    name: str = ""


@dataclass
class ArbiLiteSafe(AsyncSQLerLiteSafeModel):
    __tablename__ = "arbi_lite_safes"
    name: str = ""


class ArbiLate(AsyncSQLerModel):
    """Declares a promoted column its table was made without; the async set_db adds nothing."""

    __tablename__ = "arbi_lates"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: Optional[str] = "unset"


class ArbiLateHolder(AsyncSQLerModel):
    __tablename__ = "arbi_late_holders"
    untyped: Any = None


FAMILIES = {
    "pydantic": (ArbiJob, ArbiHolder),
    "safe": (ArbiSafeJob, ArbiSafeHolder),
    "lite": (ArbiLiteJob, ArbiLiteHolder),
    "lite-safe": (ArbiLiteSafeJob, ArbiLiteSafeHolder),
}


@pytest_asyncio.fixture
async def path(tmp_path):
    return str(tmp_path / "arbi.db")


@pytest_asyncio.fixture
async def db(path):
    d = AsyncSQLerDB.on_disk(path)
    await d.connect()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for cls in (
            ArbiJob, ArbiHolder, ArbiSafeJob, ArbiSafeHolder, ArbiLiteJob, ArbiLiteHolder,
            ArbiLiteSafeJob, ArbiLiteSafeHolder, ArbiPlain, ArbiSafe, ArbiLiteSafe,
            ArbiLate, ArbiLateHolder,
        ):
            cls.set_db(d)
    try:
        yield d
    finally:
        await d.close()


def _stored(path, table, _id):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT status FROM {table} WHERE _id = ?", [_id]).fetchone()[0]


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _status(value):
    return value["status"] if isinstance(value, dict) else value.status


async def test_find_documents_carries_the_promoted_columns_as_find_document_does(db):
    await ArbiJob(name="a", status="ready").save()
    await ArbiJob(name="b", status="done").save()
    got = await db.find_documents("arbi_jobs", [2, 99, 1, 2])
    assert got == [
        {"name": "b", "_id": 2, "status": "done"},
        {"name": "a", "_id": 1, "status": "ready"},
        {"name": "b", "_id": 2, "status": "done"},
    ]
    assert got == [await db.find_document("arbi_jobs", i) for i in (2, 1, 2)]


async def test_find_documents_on_a_missing_table_is_empty_and_creates_nothing(db, path):
    assert await db.find_documents("arbi_absent", [1, 2]) == []
    assert await db.find_documents("arbi_absent", []) == []
    assert "arbi_absent" not in _tables(path)


async def test_a_class_that_declares_a_column_its_table_was_made_without(db):
    await db.insert_document("arbi_lates", {"name": "l", "status": "json"})
    one = await ArbiLate.from_id(1)
    assert one.status == "json"
    assert [r.status for r in await ArbiLate.from_ids([1])] == ["json"]
    await ArbiLateHolder(untyped={"_table": "arbi_lates", "_id": 1}).save()
    assert (await ArbiLateHolder.from_id(1)).untyped.status == "json"
    assert (await ArbiLateHolder.from_ids([1]))[0].untyped["status"] == "json"
    assert (await ArbiLateHolder.query().all())[0].untyped["status"] == "json"


WAYS = ["from_id", "from_ids", "all", "first"]


async def _load(holder, way):
    if way == "from_id":
        return await holder.from_id(1)
    if way == "from_ids":
        return (await holder.from_ids([1]))[0]
    if way == "all":
        return (await holder.query().all())[0]
    return await holder.query().first()


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("family", list(FAMILIES))
async def test_a_referenced_row_carries_its_promoted_column(db, family, way):
    job_cls, holder_cls = FAMILIES[family]
    job = job_cls(name="j", status="ready")
    await job.save()
    fields = {"untyped": job}
    if "typed" in getattr(holder_cls, "model_fields", {}):
        fields["typed"] = job
    await holder_cls(name="h", **fields).save()
    got = await _load(holder_cls, way)
    assert _status(got.untyped) == "ready"
    if "typed" in fields:
        assert got.typed.status == "ready"


@pytest.mark.parametrize("family", list(FAMILIES))
async def test_from_ids_gives_the_stored_column_and_save_keeps_it(db, path, family):
    job_cls, _ = FAMILIES[family]
    table = job_cls.__tablename__
    await job_cls(name="j", status="ready").save()
    got = await job_cls.from_ids([1])
    assert [g.status for g in got] == ["ready"]
    await got[0].save()
    assert _stored(path, table, 1) == "ready"


@pytest.mark.parametrize("cls", [ArbiSafe, ArbiLiteSafe])
async def test_from_ids_holds_the_version_and_save_moves_it_by_one(db, path, cls):
    row = cls(name="a")
    await row.save()
    for name in ("b", "c"):
        row.name = name
        await row.save()
    got = await cls.from_ids([1])
    assert [g._version for g in got] == [2]
    got[0].name = "d"
    await got[0].save()
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"SELECT _version FROM {cls.__tablename__} WHERE _id = 1").fetchone() == (3,)


async def test_from_ids_of_the_safe_base_sets_what_it_set_before_and_no_snapshot(db):
    await ArbiSafe(name="a").save()
    got = (await ArbiSafe.from_ids([1]))[0]
    assert (got._id, got.name, got._snapshot) == (1, "a", None)


@pytest.mark.parametrize("cls", [ArbiSafe, ArbiLiteSafe])
async def test_from_ids_on_a_table_made_before_versioning_does_what_from_id_does(db, path, cls):
    table = cls.__tablename__
    await db.insert_document(table, {"name": "old"})
    got = await cls.from_ids([1])
    assert [(g.name, g._version) for g in got] == [("old", 0)]
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"SELECT _version FROM {table} WHERE _id = 1").fetchone() == (0,)


@pytest.mark.parametrize("cls", [ArbiPlain, ArbiSafe, ArbiLiteSafe])
async def test_from_ids_with_an_id_twice_and_a_gone_id_keeps_the_order_asked(db, cls):
    for name in ("a", "b", "c"):
        await cls(name=name).save()
    got = await cls.from_ids([3, 9, 1, 3])
    assert [(g._id, g.name) for g in got] == [(3, "c"), (1, "a"), (3, "c")]


@pytest.mark.parametrize("cls", [ArbiPlain, ArbiSafe, ArbiLiteSafe])
async def test_from_ids_on_a_missing_table_is_empty_and_creates_nothing(db, tmp_path, cls):
    other = AsyncSQLerDB.on_disk(str(tmp_path / "empty.db"))
    await other.connect()
    try:
        assert await cls.from_ids([1, 2], db=other) == []
    finally:
        await other.close()
    assert cls.__tablename__ not in _tables(str(tmp_path / "empty.db"))


async def test_from_ids_of_the_safe_base_reads_with_one_statement(db):
    await ArbiSafe(name="a").save()
    await ArbiSafe(name="b").save()
    with query_logger.capture() as captured:
        await ArbiSafe.from_ids([1, 2])
    assert len(captured) == 1


# --- a database object learns a table's columns from the table -----------------------------
#
# ``fresh`` is a second database object on the same file, as a second process would make it: it
# never ensures a table, so what it knows about a table it learns from the table.


@pytest_asyncio.fixture
async def fresh(path, db):
    d = AsyncSQLerDB.on_disk(path)
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


async def _with_ready_job(status="ready"):
    job = ArbiJob(name="j", status=status)
    await job.save()
    await ArbiHolder(name="h", typed=job, untyped=job).save()


def _reads(captured):
    return [q.sql for q in captured]


async def test_a_fresh_database_object_reads_the_promoted_column_of_a_row(db, fresh):
    await _with_ready_job()
    assert await fresh.find_document("arbi_jobs", 1) == {"name": "j", "_id": 1, "status": "ready"}
    assert await fresh.find_documents("arbi_jobs", [1]) == [{"name": "j", "_id": 1, "status": "ready"}]


async def test_from_ids_through_a_fresh_database_object_gives_the_column_and_save_keeps_it(db, fresh, path):
    await _with_ready_job()
    got = await ArbiJob.from_ids([1], db=fresh)
    assert [g.status for g in got] == ["ready"]
    await got[0].save(db=fresh)
    assert _stored(path, "arbi_jobs", 1) == "ready"


async def test_a_referenced_row_read_through_using_a_fresh_database_object_is_whole(db, fresh):
    await _with_ready_job()
    got = await ArbiHolder.using(fresh).first()
    assert got.typed.status == "ready"
    assert got.untyped == {"name": "j", "_id": 1, "status": "ready"}
    assert [h.typed.status for h in await ArbiHolder.using(fresh).all()] == ["ready"]


async def test_set_db_on_a_fresh_database_object_then_from_id_gives_the_column(db, fresh, path):
    await _with_ready_job()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ArbiJob.set_db(fresh)
    got = await ArbiJob.from_id(1)
    assert got.status == "ready"
    await got.save()
    assert _stored(path, "arbi_jobs", 1) == "ready"


async def test_a_table_is_learned_once_and_a_known_table_asks_nothing(db, fresh):
    await _with_ready_job()
    with query_logger.capture() as first:
        await fresh.find_document("arbi_jobs", 1)
    with query_logger.capture() as second:
        await fresh.find_document("arbi_jobs", 1)
    assert _reads(first) == ['PRAGMA table_info("arbi_jobs");', "SELECT _id, data, status FROM arbi_jobs WHERE _id IN (?);"]
    assert _reads(second) == ["SELECT _id, data, status FROM arbi_jobs WHERE _id IN (?);"]


async def test_what_is_learned_inside_a_transaction_that_rolls_back_is_dropped(db, fresh, path):
    with pytest.raises(RuntimeError, match="undo"):
        async with fresh:
            await fresh.adapter.execute(
                "CREATE TABLE arbi_txn (_id INTEGER PRIMARY KEY, data JSON NOT NULL, status TEXT)"
            )
            await fresh.adapter.execute("""INSERT INTO arbi_txn (data, status) VALUES ('{"name": "t"}', 'in')""")
            assert await fresh.find_document("arbi_txn", 1) == {"name": "t", "_id": 1, "status": "in"}
            raise RuntimeError("undo")
    assert await fresh.find_document("arbi_txn", 1) is None
    assert "arbi_txn" not in _tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE arbi_txn (_id INTEGER PRIMARY KEY, data JSON NOT NULL, note TEXT)")
        conn.execute("""INSERT INTO arbi_txn (data, note) VALUES ('{"name": "u"}', 'after')""")
    assert await fresh.find_document("arbi_txn", 1) == {"name": "u", "_id": 1, "note": "after"}


async def test_what_is_learned_inside_a_transaction_that_commits_is_kept(db, fresh):
    await _with_ready_job()
    async with fresh:
        assert (await fresh.find_document("arbi_jobs", 1))["status"] == "ready"
    with query_logger.capture() as captured:
        await fresh.find_document("arbi_jobs", 1)
    assert _reads(captured) == ["SELECT _id, data, status FROM arbi_jobs WHERE _id IN (?);"]


async def test_two_querysets_of_one_class_on_two_databases_read_their_own_child_rows(db, tmp_path):
    await _with_ready_job(status="here")
    other = AsyncSQLerDB.on_disk(str(tmp_path / "other.db"))
    await other.connect()
    try:
        job = ArbiJob(name="o", status="there")
        await job.save(db=other)
        await ArbiHolder(name="h2", typed=job, untyped=job).save(db=other)
        mine, theirs = await ArbiHolder.using(db).first(), await ArbiHolder.using(other).first()
    finally:
        await other.close()
    assert (mine.typed.name, mine.typed.status, mine.untyped["status"]) == ("j", "here", "here")
    assert (theirs.typed.name, theirs.typed.status, theirs.untyped["status"]) == ("o", "there", "there")


async def test_a_queryset_built_by_hand_reads_child_rows_as_before(db):
    """Pinned: with no database handed over, a child row is read as ``_id`` and ``data``."""
    from sqler.models.async_queryset import AsyncSQLerQuerySet
    from sqler.query.async_query import AsyncSQLerQuery

    await _with_ready_job()
    qs = AsyncSQLerQuerySet(ArbiHolder, AsyncSQLerQuery(table="arbi_holders", adapter=db.adapter))
    assert (await qs.first()).untyped == {"name": "j", "_id": 1}


# --- a queryset's copy carries everything the queryset holds -------------------------------

_REF = {"_table": "arbi_jobs", "_id": 1}


def _name_is(value):
    from sqler.query import SQLerField

    return SQLerField("name") == value


CHAINS = {
    "filter": lambda qs: qs.filter(_name_is("h")),
    "exclude": lambda qs: qs.exclude(_name_is("x")),
    "or_filter": lambda qs: qs.or_filter(_name_is("h")),
    "distinct": lambda qs: qs.distinct(),
    "select": lambda qs: qs.select("name", "untyped"),
    "order_by": lambda qs: qs.order_by("name"),
    "limit": lambda qs: qs.limit(5),
    "offset": lambda qs: qs.limit(5).offset(0),
    "group_by": lambda qs: qs.group_by("name"),
    "having": lambda qs: qs.group_by("name").having(_name_is("h")),
    "resolve": lambda qs: qs.resolve(qs._resolve),
}
ROWS = {"filter", "exclude", "or_filter", "distinct", "select", "order_by", "limit", "offset", "resolve"}


@pytest.mark.parametrize("method", list(CHAINS))
async def test_every_chaining_method_keeps_resolve_off(db, method):
    await _with_ready_job()
    qs = CHAINS[method](ArbiHolder.query().resolve(False))
    assert qs._resolve is False
    assert qs._db is db
    if method in ROWS:
        assert (await qs.first()).untyped == _REF
        assert [h.untyped for h in await qs.all()] == [_REF]


@pytest.mark.parametrize("method", list(CHAINS))
async def test_every_chaining_method_keeps_the_database_of_using(db, fresh, method):
    await _with_ready_job()
    qs = CHAINS[method](ArbiHolder.using(fresh))
    assert qs._resolve is True
    assert qs._db is fresh
    if method in ROWS:
        assert (await qs.first()).untyped == {"name": "j", "_id": 1, "status": "ready"}


# --- the async reader's own path: the shapes the sync file pins ---------------------------


async def test_a_null_column_and_a_json_key_of_its_name_read_as_find_document_reads_them(db):
    await db.ensure_table("arbi_jobs", promoted={"status": "TEXT"})  # the async set_db makes no table
    await db.adapter.execute(
        "INSERT INTO arbi_jobs (data, status) VALUES (?, NULL)", ['{"name": "n", "status": "json"}']
    )
    await db.adapter.execute(
        "INSERT INTO arbi_jobs (data, status) VALUES (?, ?)", ['{"name": "m", "status": "json"}', "col"]
    )
    await db.adapter.commit()
    one = [await db.find_document("arbi_jobs", 1), await db.find_document("arbi_jobs", 2)]
    assert one == [
        {"name": "n", "status": None, "_id": 1},
        {"name": "m", "status": "col", "_id": 2},
    ]
    assert await db.find_documents("arbi_jobs", [1, 2]) == one
    await ArbiHolder(untyped={"_table": "arbi_jobs", "_id": 1}).save()
    await ArbiHolder(untyped={"_table": "arbi_jobs", "_id": 2}).save()
    assert [h.untyped["status"] for h in await ArbiHolder.query().all()] == [None, "col"]
    assert [h.untyped["status"] for h in await ArbiHolder.from_ids([1, 2])] == [None, "col"]


class ArbiBare(AsyncSQLerModel):
    """Declares no promoted column; its table is made with one."""

    __tablename__ = "arbi_bares"
    name: str = ""
    status: str = "unset"


class ArbiBareHolder(AsyncSQLerModel):
    __tablename__ = "arbi_bare_holders"
    untyped: Any = None


async def test_a_table_with_promoted_columns_read_through_a_class_that_declares_none(db):
    await db.ensure_table("arbi_bares", promoted={"status": "TEXT"})
    await db.upsert_document_promoted("arbi_bares", None, {"name": "b", "status": "ready"}, ["status"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ArbiBare.set_db(db)
        ArbiBareHolder.set_db(db)
    assert (await ArbiBare.from_id(1)).status == "ready"
    assert [b.status for b in await ArbiBare.from_ids([1])] == ["ready"]
    await ArbiBareHolder(untyped={"_table": "arbi_bares", "_id": 1}).save()
    assert (await ArbiBareHolder.from_id(1)).untyped.status == "ready"
    assert (await ArbiBareHolder.from_ids([1]))[0].untyped["status"] == "ready"
    assert (await ArbiBareHolder.query().first()).untyped["status"] == "ready"


@pytest.mark.parametrize(
    "ddl, row, expected",
    [
        (
            "CREATE TABLE arbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL)",
            ("""'{"name": "a"}'""",),
            {"name": "a", "_id": 1},
        ),
        (
            "CREATE TABLE arbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL, "
            "_version INTEGER NOT NULL DEFAULT 0)",
            ("""'{"name": "a"}'""",),
            {"name": "a", "_id": 1},
        ),
        (
            "CREATE TABLE arbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL, extra TEXT)",
            ("""'{"name": "a", "extra": "json"}'""", "'col'"),
            {"name": "a", "_id": 1, "extra": "col"},
        ),
        (
            'CREATE TABLE arbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL, "order" TEXT, '
            '"two words" TEXT)',
            ("""'{"name": "a"}'""", "'o'", "'w'"),
            {"name": "a", "_id": 1},
        ),
    ],
    ids=["id-and-data", "version-only", "column-added-by-hand", "names-sqler-never-makes"],
)
async def test_which_columns_of_a_table_count(db, fresh, path, ddl, row, expected):
    with sqlite3.connect(path) as conn:
        conn.execute(ddl)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(arbi_shape)") if r[1] not in ("_id", "_version")]
        names = ", ".join(f'"{c}"' for c in cols)
        conn.execute(f"INSERT INTO arbi_shape ({names}) VALUES ({', '.join(row)})")
    assert await fresh.find_document("arbi_shape", 1) == expected
    assert await fresh.find_documents("arbi_shape", [1]) == [expected]


async def test_a_hostile_table_name_is_refused_before_any_statement(db, fresh):
    from sqler.exceptions import InvalidTableNameError

    hostile = "arbi_jobs; DROP TABLE arbi_jobs"
    with query_logger.capture() as captured:
        with pytest.raises(InvalidTableNameError):
            await fresh.find_document(hostile, 1)
        with pytest.raises(InvalidTableNameError):
            await fresh.find_documents(hostile, [1])
    assert captured == []


async def test_a_column_promoted_later_on_the_same_object_is_read_next(db, fresh):
    await _with_ready_job()
    assert (await fresh.find_document("arbi_jobs", 1))["status"] == "ready"
    await fresh.ensure_table("arbi_jobs", promoted={"note": "TEXT"})
    await fresh.adapter.execute("UPDATE arbi_jobs SET note = 'n' WHERE _id = 1")
    await fresh.adapter.commit()
    assert await fresh.find_document("arbi_jobs", 1) == {"name": "j", "_id": 1, "status": "ready", "note": "n"}


async def test_a_missing_table_is_not_remembered_and_not_created(db, fresh, path):
    assert await fresh.find_documents("arbi_later", [1]) == []
    assert await fresh.find_document("arbi_later", 1) is None
    assert "arbi_later" not in _tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE arbi_later (_id INTEGER PRIMARY KEY, data JSON NOT NULL, kind TEXT)")
        conn.execute("""INSERT INTO arbi_later (data, kind) VALUES ('{"name": "l"}', 'k')""")
    assert await fresh.find_document("arbi_later", 1) == {"name": "l", "_id": 1, "kind": "k"}


# --- a by-id read does not fail on the number of ids -----------------------------------------

MANY = 40_000


@dataclass
class ArbiLitePlain(AsyncSQLerLiteModel):
    __tablename__ = "arbi_lite_plains"
    name: str = ""


async def _save_many(cls, instances):
    many = getattr(cls, "asave_many", None) or cls.save_many
    await many(instances)


@pytest.mark.parametrize("cls", [ArbiPlain, ArbiSafe, ArbiLitePlain, ArbiLiteSafe], ids=lambda c: c.__name__)
async def test_from_ids_and_find_documents_of_40000_ids(db, cls):
    """More ids than SQLite takes as variables in one statement: read in groups, order and
    repeats as asked."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)
    await _save_many(cls, [cls(name=f"n{i}") for i in range(MANY)])
    ids = [*range(MANY, 0, -1), 7, MANY + 5]
    got = await cls.from_ids(ids)
    assert len(got) == MANY + 1
    assert [g._id for g in got] == ids[:-1]
    assert (got[0].name, got[-1].name, got[-2].name) == (f"n{MANY - 1}", "n6", "n0")
    docs = await db.find_documents(cls.__tablename__, ids)
    assert len(docs) == MANY + 1
    assert [d["_id"] for d in docs] == ids[:-1]


async def test_ids_up_to_the_bound_are_one_variable_each_and_past_it_one_json_array(db):
    await _save_many(ArbiPlain, [ArbiPlain(name=f"n{i}") for i in range(1000)])
    await db.find_documents("arbi_plains", [1])  # learns the table's columns
    with query_logger.capture() as under:
        await db.find_documents("arbi_plains", list(range(1, 1000)))
    with query_logger.capture() as over:
        await db.find_documents("arbi_plains", list(range(1, 1001)))
    assert [s.count("?") for s in _reads(under)] == [999]
    assert _reads(over) == ["SELECT _id, data FROM arbi_plains WHERE _id IN (SELECT value FROM json_each(?));"]


# --- a save inside a transaction that rolls back ------------------------------------------


class ArbiUndone(AsyncSQLerModel):
    __tablename__ = "arbi_undones"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


async def test_a_save_in_a_transaction_that_rolls_back_then_a_read_and_a_save_through_the_same_object(
    db, fresh, path
):
    with pytest.raises(RuntimeError, match="undo"):
        async with fresh:
            await ArbiUndone(name="a", status="gone").save(db=fresh)
            raise RuntimeError("undo")
    assert "arbi_undones" not in _tables(path)
    assert await ArbiUndone.from_ids([1], db=fresh) == []
    assert await fresh.find_document("arbi_undones", 1) is None
    await ArbiUndone(name="b", status="ready").save(db=fresh)
    assert _stored(path, "arbi_undones", 1) == "ready"
    assert await fresh.find_document("arbi_undones", 1) == {"name": "b", "_id": 1, "status": "ready"}
    got = await ArbiUndone.from_ids([1], db=fresh)
    assert [(g.name, g.status) for g in got] == [("b", "ready")]
    got[0].name = "c"
    await got[0].save(db=fresh)
    assert _stored(path, "arbi_undones", 1) == "ready"
    assert (await fresh.find_document("arbi_undones", 1))["name"] == "c"
