"""A row read by its id is the whole row: its promoted columns, and for a versioned table its version.

Every by-id read gives what ``SQLerDB.find_document`` gives for the same row: the stored JSON,
then ``_id``, then each promoted column the database knows, over a JSON key of the same name.
What is stored is checked with ``sqlite3`` alone.
"""

import sqlite3
import warnings
from dataclasses import dataclass
from typing import Any, Optional

import pytest
from sqler import SQLerDB, SQLerLiteModel, SQLerLiteSafeModel, SQLerModel, SQLerSafeModel
from sqler.logging import query_logger


class RbiJob(SQLerModel):
    __tablename__ = "rbi_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


class RbiHolder(SQLerModel):
    __tablename__ = "rbi_holders"
    name: str = ""
    typed: Optional[RbiJob] = None
    untyped: Any = None


class RbiSafeJob(SQLerSafeModel):
    __tablename__ = "rbi_safe_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


class RbiSafeHolder(SQLerModel):
    __tablename__ = "rbi_safe_holders"
    name: str = ""
    typed: Optional[RbiSafeJob] = None
    untyped: Any = None


@dataclass
class RbiLiteJob(SQLerLiteModel):
    __tablename__ = "rbi_lite_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


@dataclass
class RbiLiteHolder(SQLerLiteModel):
    __tablename__ = "rbi_lite_holders"
    name: str = ""
    untyped: Any = None


@dataclass
class RbiLiteSafeJob(SQLerLiteSafeModel):
    __tablename__ = "rbi_lite_safe_jobs"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


@dataclass
class RbiLiteSafeHolder(SQLerLiteModel):
    __tablename__ = "rbi_lite_safe_holders"
    name: str = ""
    untyped: Any = None


class RbiPlain(SQLerModel):
    __tablename__ = "rbi_plains"
    name: str = ""


class RbiSafe(SQLerSafeModel):
    __tablename__ = "rbi_safes"
    name: str = ""


@dataclass
class RbiLiteSafe(SQLerLiteSafeModel):
    __tablename__ = "rbi_lite_safes"
    name: str = ""


class RbiBare(SQLerModel):
    """Declares no promoted column; its table is made with one."""

    __tablename__ = "rbi_bares"
    name: str = ""
    status: str = "unset"


class RbiBareHolder(SQLerModel):
    __tablename__ = "rbi_bare_holders"
    untyped: Any = None


class RbiLate(SQLerModel):
    """Declares a promoted column its table was made without."""

    __tablename__ = "rbi_lates"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: Optional[str] = "unset"


FAMILIES = {
    "pydantic": (RbiJob, RbiHolder),
    "safe": (RbiSafeJob, RbiSafeHolder),
    "lite": (RbiLiteJob, RbiLiteHolder),
    "lite-safe": (RbiLiteSafeJob, RbiLiteSafeHolder),
}


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "rbi.db")


@pytest.fixture
def db(path):
    d = SQLerDB.on_disk(path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for cls in (
            RbiJob, RbiHolder, RbiSafeJob, RbiSafeHolder, RbiLiteJob, RbiLiteHolder,
            RbiLiteSafeJob, RbiLiteSafeHolder, RbiPlain, RbiSafe, RbiLiteSafe,
        ):
            cls.set_db(d)
    yield d
    d.close()


def _stored(path, table, _id):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT status, data FROM {table} WHERE _id = ?", [_id]).fetchone()


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _status(value):
    return value["status"] if isinstance(value, dict) else value.status


# --- find_documents gives what find_document gives -------------------------------------


def test_find_documents_carries_the_promoted_columns_as_find_document_does(db):
    RbiJob(name="a", status="ready").save()
    RbiJob(name="b", status="done").save()
    got = db.find_documents("rbi_jobs", [2, 99, 1, 2])
    assert got == [
        {"name": "b", "_id": 2, "status": "done"},
        {"name": "a", "_id": 1, "status": "ready"},
        {"name": "b", "_id": 2, "status": "done"},
    ]
    assert got == [db.find_document("rbi_jobs", i) for i in (2, 1, 2)]


def test_find_documents_on_a_table_without_promoted_columns_keeps_order_and_skips_gone_ids(db):
    for name in ("a", "b", "c"):
        RbiPlain(name=name).save()
    assert db.find_documents("rbi_plains", [3, 7, 1, 3]) == [
        {"name": "c", "_id": 3},
        {"name": "a", "_id": 1},
        {"name": "c", "_id": 3},
    ]
    assert db.find_documents("rbi_plains", []) == []


def test_find_documents_on_a_missing_table_is_empty_and_creates_nothing(db, path):
    assert db.find_documents("rbi_absent", [1, 2]) == []
    assert "rbi_absent" not in _tables(path)


def test_a_null_column_and_a_json_key_of_its_name_read_as_find_document_reads_them(db):
    db.adapter.execute(
        "INSERT INTO rbi_jobs (data, status) VALUES (?, NULL)", ['{"name": "n", "status": "json"}']
    )
    db.adapter.execute(
        "INSERT INTO rbi_jobs (data, status) VALUES (?, ?)", ['{"name": "m", "status": "json"}', "col"]
    )
    db.adapter.commit()
    one = [db.find_document("rbi_jobs", 1), db.find_document("rbi_jobs", 2)]
    assert one == [
        {"name": "n", "status": None, "_id": 1},
        {"name": "m", "status": "col", "_id": 2},
    ]
    assert db.find_documents("rbi_jobs", [1, 2]) == one
    RbiHolder(untyped={"_table": "rbi_jobs", "_id": 1}).save()
    RbiHolder(untyped={"_table": "rbi_jobs", "_id": 2}).save()
    assert [h.untyped["status"] for h in RbiHolder.query().all()] == [None, "col"]
    assert [h.untyped["status"] for h in RbiHolder.from_ids([1, 2])] == [None, "col"]


def test_a_table_with_promoted_columns_read_through_a_class_that_declares_none(db):
    db.ensure_table("rbi_bares", promoted={"status": "TEXT"})
    db.upsert_document_promoted("rbi_bares", None, {"name": "b", "status": "ready"}, ["status"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        RbiBare.set_db(db)
        RbiBareHolder.set_db(db)
    assert RbiBare.from_id(1).status == "ready"
    assert [b.status for b in RbiBare.from_ids([1])] == ["ready"]
    RbiBareHolder(untyped={"_table": "rbi_bares", "_id": 1}).save()
    assert RbiBareHolder.from_id(1).untyped.status == "ready"
    assert RbiBareHolder.from_ids([1])[0].untyped["status"] == "ready"
    assert RbiBareHolder.query().first().untyped["status"] == "ready"


def test_a_class_that_declares_a_column_its_table_was_made_without(db):
    db.insert_document("rbi_lates", {"name": "l", "status": "json"})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        RbiLate.set_db(db)  # adds the column, NULL in the old row
    assert RbiLate.from_id(1).status is None
    assert [r.status for r in RbiLate.from_ids([1])] == [None]
    assert db.find_documents("rbi_lates", [1]) == [db.find_document("rbi_lates", 1)]


# --- a referenced row is read whole, on every way in --------------------------------------


WAYS = {
    "from_id": lambda holder: holder.from_id(1),
    "from_ids": lambda holder: holder.from_ids([1])[0],
    "all": lambda holder: holder.query().all()[0],
    "first": lambda holder: holder.query().first(),
}


@pytest.mark.parametrize("way", list(WAYS))
@pytest.mark.parametrize("family", list(FAMILIES))
def test_a_referenced_row_carries_its_promoted_column(db, family, way):
    job_cls, holder_cls = FAMILIES[family]
    job = job_cls(name="j", status="ready")
    job.save()
    fields = {"untyped": job}
    if "typed" in getattr(holder_cls, "model_fields", {}):
        fields["typed"] = job
    holder_cls(name="h", **fields).save()
    got = WAYS[way](holder_cls)
    assert _status(got.untyped) == "ready"
    if "typed" in fields:
        assert got.typed.status == "ready"


# --- from_ids gives the stored values, and saving them changes nothing ---------------------


@pytest.mark.parametrize("family", list(FAMILIES))
def test_from_ids_gives_the_stored_column_and_save_keeps_it(db, path, family):
    job_cls, _ = FAMILIES[family]
    table = job_cls.__tablename__
    job_cls(name="j", status="ready").save()
    assert _stored(path, table, 1)[0] == "ready"
    got = job_cls.from_ids([1])
    assert [g.status for g in got] == ["ready"]
    got[0].save()
    assert _stored(path, table, 1)[0] == "ready"


# --- from_ids on a versioned base holds the row's version ----------------------------------


@pytest.mark.parametrize("cls", [RbiSafe, RbiLiteSafe])
def test_from_ids_holds_the_version_and_save_moves_it_by_one(db, path, cls):
    row = cls(name="a")
    row.save()
    for name in ("b", "c"):
        row.name = name
        row.save()
    got = cls.from_ids([1])
    assert [g._version for g in got] == [2]
    got[0].name = "d"
    got[0].save()
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"SELECT _version FROM {cls.__tablename__} WHERE _id = 1").fetchone() == (3,)
    assert got[0]._version == 3


def test_from_ids_of_the_safe_base_reads_the_version_column_not_the_json_key(db):
    row = RbiSafe(name="a")
    row.save()
    row.name = "b"
    row.save()
    db.adapter.execute("UPDATE rbi_safes SET _version = 5 WHERE _id = 1")
    db.adapter.commit()
    assert RbiSafe.from_id(1)._version == 5
    assert [g._version for g in RbiSafe.from_ids([1])] == [5]


def test_from_ids_of_the_safe_base_sets_what_it_set_before_and_no_snapshot(db):
    RbiSafe(name="a").save()
    got = RbiSafe.from_ids([1])[0]
    assert (got._id, got.name, got._snapshot) == (1, "a", None)


@pytest.mark.parametrize("cls", [RbiSafe, RbiLiteSafe])
def test_from_ids_on_a_table_made_before_versioning_does_what_from_id_does(db, path, cls):
    table = cls.__tablename__
    db.adapter.execute(f"DROP TABLE {table}")
    db.adapter.execute(f"CREATE TABLE {table} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)")
    db.adapter.execute(f"INSERT INTO {table} (data) VALUES (?)", ['{"name": "old"}'])
    db.adapter.commit()
    got = cls.from_ids([1])
    assert [(g.name, g._version) for g in got] == [("old", 0)]
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"SELECT _version FROM {table} WHERE _id = 1").fetchone() == (0,)
    got[0].name = "new"
    got[0].save()
    assert got[0]._version == 1


@pytest.mark.parametrize("cls", [RbiPlain, RbiSafe, RbiLiteSafe])
def test_from_ids_with_an_id_twice_and_a_gone_id_keeps_the_order_asked(db, cls):
    for name in ("a", "b", "c"):
        cls(name=name).save()
    got = cls.from_ids([3, 9, 1, 3])
    assert [(g._id, g.name) for g in got] == [(3, "c"), (1, "a"), (3, "c")]
    assert got[0] is not got[2]


@pytest.mark.parametrize("cls", [RbiPlain, RbiSafe])
def test_from_ids_of_1500_ids_in_reverse(db, cls):
    cls.save_many([cls(name=f"n{i}") for i in range(1500)])
    ids = list(range(1500, 0, -1))
    got = cls.from_ids(ids)
    assert len(got) == 1500
    assert [g._id for g in got] == ids
    assert got[0].name == "n1499"


@pytest.mark.parametrize("cls", [RbiPlain, RbiSafe, RbiLiteSafe])
def test_from_ids_on_a_missing_table_is_empty_and_creates_nothing(db, tmp_path, cls):
    other = SQLerDB.on_disk(str(tmp_path / "empty.db"))
    try:
        assert cls.from_ids([1, 2], db=other) == []
    finally:
        other.close()
    assert cls.__tablename__ not in _tables(str(tmp_path / "empty.db"))


# --- no new statement --------------------------------------------------------------------


def _reads(fn):
    with query_logger.capture() as captured:
        fn()
    return [q.sql for q in captured]


def test_from_ids_of_a_plain_class_reads_with_one_statement(db):
    """The first read of a table the object ensured without promoted columns also learns its
    columns, once; every read after it is one statement."""
    RbiPlain(name="a").save()
    RbiPlain(name="b").save()
    assert _reads(lambda: RbiPlain.from_ids([1, 2])) == [
        'PRAGMA table_info("rbi_plains");',
        "SELECT _id, data FROM rbi_plains WHERE _id IN (?,?);",
    ]
    assert _reads(lambda: RbiPlain.from_ids([1, 2])) == ["SELECT _id, data FROM rbi_plains WHERE _id IN (?,?);"]


def test_from_ids_of_the_safe_base_reads_with_one_statement(db):
    RbiSafe(name="a").save()
    RbiSafe(name="b").save()
    sql = _reads(lambda: RbiSafe.from_ids([1, 2]))
    assert len(sql) == 1


def test_a_parent_with_a_referenced_row_reads_one_statement_per_table(db):
    job = RbiJob(name="j", status="ready")
    job.save()
    RbiHolder(name="h", typed=job, untyped=job).save()
    assert len(_reads(lambda: RbiHolder.from_ids([1]))) == 3  # the holders' columns learned once
    assert len(_reads(lambda: RbiHolder.from_ids([1]))) == 2
    assert len(_reads(lambda: RbiHolder.query().all())) == 2


# --- a database object learns a table's columns from the table -----------------------------
#
# ``fresh`` is a second database object on the same file, as a second process would make it: it
# never ensures a table, so what it knows about a table it learns from the table.


@pytest.fixture
def fresh(path, db):
    d = SQLerDB.on_disk(path)
    yield d
    d.close()


def _with_ready_job(holder_cls=RbiHolder, job_cls=RbiJob, status="ready"):
    job = job_cls(name="j", status=status)
    job.save()
    holder_cls(name="h", typed=job, untyped=job).save()


def _column(path, table, column, _id=1):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT {column} FROM {table} WHERE _id = ?", [_id]).fetchone()[0]


def test_a_fresh_database_object_reads_the_promoted_column_of_a_row(db, fresh):
    _with_ready_job()
    assert fresh.find_document("rbi_jobs", 1) == {"name": "j", "_id": 1, "status": "ready"}
    assert fresh.find_documents("rbi_jobs", [1]) == [{"name": "j", "_id": 1, "status": "ready"}]


def test_from_ids_through_a_fresh_database_object_gives_the_column_and_save_keeps_it(db, fresh, path):
    _with_ready_job()
    got = RbiJob.from_ids([1], db=fresh)
    assert [g.status for g in got] == ["ready"]
    got[0].save(db=fresh)
    assert _column(path, "rbi_jobs", "status") == "ready"


def test_a_referenced_row_read_through_using_a_fresh_database_object_is_whole(db, fresh, path):
    _with_ready_job()
    got = RbiHolder.using(fresh).first()
    assert got.typed.status == "ready"
    assert got.untyped == {"name": "j", "_id": 1, "status": "ready"}
    got.typed.save(db=fresh)
    assert _column(path, "rbi_jobs", "status") == "ready"


def test_a_table_is_learned_once_and_a_known_table_asks_nothing(db, fresh):
    _with_ready_job()
    RbiPlain(name="p").save()
    first = _reads(lambda: fresh.find_document("rbi_jobs", 1))
    assert len(first) == 2
    assert _reads(lambda: fresh.find_document("rbi_jobs", 1)) == first[1:]
    assert len(_reads(lambda: fresh.find_documents("rbi_plains", [1]))) == 2
    assert len(_reads(lambda: fresh.find_documents("rbi_plains", [1]))) == 1  # remembered as having none
    assert len(_reads(lambda: db.find_document("rbi_jobs", 1))) == 1  # the object that ensured it


def test_a_missing_table_is_not_remembered_and_not_created(db, fresh, path):
    assert fresh.find_documents("rbi_later", [1]) == []
    assert fresh.find_document("rbi_later", 1) is None
    assert "rbi_later" not in _tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE rbi_later (_id INTEGER PRIMARY KEY, data JSON NOT NULL, kind TEXT)")
        conn.execute("""INSERT INTO rbi_later (data, kind) VALUES ('{"name": "l"}', 'k')""")
    assert fresh.find_document("rbi_later", 1) == {"name": "l", "_id": 1, "kind": "k"}


def test_what_is_learned_inside_a_transaction_that_rolls_back_is_dropped(db, fresh, path):
    with pytest.raises(RuntimeError, match="undo"):
        with fresh:
            fresh.adapter.execute(
                "CREATE TABLE rbi_txn (_id INTEGER PRIMARY KEY, data JSON NOT NULL, status TEXT)"
            )
            fresh.adapter.execute("""INSERT INTO rbi_txn (data, status) VALUES ('{"name": "t"}', 'in')""")
            assert fresh.find_document("rbi_txn", 1) == {"name": "t", "_id": 1, "status": "in"}
            raise RuntimeError("undo")
    assert fresh.find_document("rbi_txn", 1) is None
    assert "rbi_txn" not in _tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE rbi_txn (_id INTEGER PRIMARY KEY, data JSON NOT NULL, note TEXT)")
        conn.execute("""INSERT INTO rbi_txn (data, note) VALUES ('{"name": "u"}', 'after')""")
    assert fresh.find_document("rbi_txn", 1) == {"name": "u", "_id": 1, "note": "after"}


def test_what_is_learned_inside_a_transaction_that_commits_is_kept(db, fresh):
    _with_ready_job()
    with fresh:
        assert fresh.find_document("rbi_jobs", 1)["status"] == "ready"
    sql = _reads(lambda: fresh.find_document("rbi_jobs", 1))
    assert len(sql) == 1
    assert sql[0].startswith("SELECT _id, data, status FROM rbi_jobs")


def test_a_column_promoted_later_on_the_same_object_is_read_next(db, fresh):
    _with_ready_job()
    assert fresh.find_document("rbi_jobs", 1)["status"] == "ready"
    fresh.ensure_table("rbi_jobs", promoted={"note": "TEXT"})
    fresh.adapter.execute("UPDATE rbi_jobs SET note = 'n' WHERE _id = 1")
    fresh.adapter.commit()
    assert fresh.find_document("rbi_jobs", 1) == {"name": "j", "_id": 1, "status": "ready", "note": "n"}


def test_a_column_promoted_by_another_object_after_this_one_learned_is_not_seen(db, fresh):
    """Pinned, not fixed: the object learned the table before the column existed."""
    _with_ready_job()
    assert fresh.find_document("rbi_jobs", 1) == {"name": "j", "_id": 1, "status": "ready"}
    db.ensure_table("rbi_jobs", promoted={"note": "TEXT"})
    db.adapter.execute("UPDATE rbi_jobs SET note = 'n' WHERE _id = 1")
    db.adapter.commit()
    assert fresh.find_document("rbi_jobs", 1) == {"name": "j", "_id": 1, "status": "ready"}
    assert db.find_document("rbi_jobs", 1) == {"name": "j", "_id": 1, "status": "ready", "note": "n"}


def test_a_class_with_a_promoted_column_saves_through_an_object_that_learned_its_table_without_it(
    db, fresh, path
):
    db.insert_document("rbi_lates", {"name": "l"})
    assert fresh.find_document("rbi_lates", 1) == {"name": "l", "_id": 1}
    RbiLate(name="m", status="s").save(db=fresh)
    assert _column(path, "rbi_lates", "status", 2) == "s"
    assert fresh.find_document("rbi_lates", 2) == {"name": "m", "_id": 2, "status": "s"}


@pytest.mark.parametrize(
    "ddl, row, expected",
    [
        (
            "CREATE TABLE rbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL)",
            ("""'{"name": "a"}'""",),
            {"name": "a", "_id": 1},
        ),
        (
            "CREATE TABLE rbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL, "
            "_version INTEGER NOT NULL DEFAULT 0)",
            ("""'{"name": "a"}'""",),
            {"name": "a", "_id": 1},
        ),
        (
            "CREATE TABLE rbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL, extra TEXT)",
            ("""'{"name": "a", "extra": "json"}'""", "'col'"),
            {"name": "a", "_id": 1, "extra": "col"},
        ),
        (
            'CREATE TABLE rbi_shape (_id INTEGER PRIMARY KEY, data JSON NOT NULL, "order" TEXT, '
            '"two words" TEXT)',
            ("""'{"name": "a"}'""", "'o'", "'w'"),
            {"name": "a", "_id": 1},
        ),
    ],
    ids=["id-and-data", "version-only", "column-added-by-hand", "names-sqler-never-makes"],
)
def test_which_columns_of_a_table_count(db, fresh, path, ddl, row, expected):
    with sqlite3.connect(path) as conn:
        conn.execute(ddl)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(rbi_shape)") if r[1] not in ("_id", "_version")]
        names = ", ".join(f'"{c}"' for c in cols)
        conn.execute(f"INSERT INTO rbi_shape ({names}) VALUES ({', '.join(row)})")
    assert fresh.find_document("rbi_shape", 1) == expected
    assert fresh.find_documents("rbi_shape", [1]) == [expected]


def test_a_hostile_table_name_is_refused_before_any_statement(db, fresh):
    from sqler.exceptions import InvalidTableNameError

    hostile = "rbi_jobs; DROP TABLE rbi_jobs"
    with query_logger.capture() as captured:
        with pytest.raises(InvalidTableNameError):
            fresh.find_document(hostile, 1)
        with pytest.raises(InvalidTableNameError):
            fresh.find_documents(hostile, [1])
    assert captured == []


def test_two_querysets_of_one_class_on_two_databases_read_their_own_child_rows(db, tmp_path):
    _with_ready_job(status="here")
    other = SQLerDB.on_disk(str(tmp_path / "other.db"))
    try:
        job = RbiJob(name="o", status="there")
        job.save(db=other)
        RbiHolder(name="h2", typed=job, untyped=job).save(db=other)
        mine, theirs = RbiHolder.using(db).first(), RbiHolder.using(other).first()
    finally:
        other.close()
    assert (mine.typed.name, mine.typed.status, mine.untyped["status"]) == ("j", "here", "here")
    assert (theirs.typed.name, theirs.typed.status, theirs.untyped["status"]) == ("o", "there", "there")


def test_a_queryset_built_by_hand_reads_child_rows_as_before(db):
    """Pinned: with no database handed over, a child row is read as ``_id`` and ``data``."""
    from sqler.models.queryset import SQLerQuerySet
    from sqler.query import SQLerQuery

    _with_ready_job()
    qs = SQLerQuerySet(RbiHolder, SQLerQuery(table="rbi_holders", adapter=db.adapter))
    assert qs.first().untyped == {"name": "j", "_id": 1}


# --- a queryset's copy carries everything the queryset holds -------------------------------

_REF = {"_table": "rbi_jobs", "_id": 1}
CHAINS = {
    "filter": lambda qs: qs.filter(_name_is_h()),
    "exclude": lambda qs: qs.exclude(_name_is("x")),
    "or_filter": lambda qs: qs.or_filter(_name_is_h()),
    "distinct": lambda qs: qs.distinct(),
    "select": lambda qs: qs.select("name", "untyped"),
    "order_by": lambda qs: qs.order_by("name"),
    "limit": lambda qs: qs.limit(5),
    "offset": lambda qs: qs.limit(5).offset(0),
    "group_by": lambda qs: qs.group_by("name"),
    "having": lambda qs: qs.group_by("name").having(_name_is_h()),
    "resolve": lambda qs: qs.resolve(qs._resolve),
}
ROWS = {"filter", "exclude", "or_filter", "distinct", "select", "order_by", "limit", "offset", "resolve"}


def _name_is(value):
    from sqler.query import SQLerField

    return SQLerField("name") == value


def _name_is_h():
    return _name_is("h")


@pytest.mark.parametrize("method", list(CHAINS))
def test_every_chaining_method_keeps_resolve_off(db, method):
    _with_ready_job()
    qs = CHAINS[method](RbiHolder.query().resolve(False))
    assert qs._resolve is False
    assert qs._db is db
    if method in ROWS:
        assert qs.first().untyped == _REF
        assert [h.untyped for h in qs.all()] == [_REF]


@pytest.mark.parametrize("method", list(CHAINS))
def test_every_chaining_method_keeps_the_database_of_using(db, fresh, method):
    _with_ready_job()
    qs = CHAINS[method](RbiHolder.using(fresh))
    assert qs._resolve is True
    assert qs._db is fresh
    if method in ROWS:
        assert qs.first().untyped == {"name": "j", "_id": 1, "status": "ready"}


def test_a_model_bound_to_a_pooled_database_still_resolves_its_references(tmp_path):
    from sqler.pool import PooledSQLerDB

    pooled = PooledSQLerDB.on_disk(str(tmp_path / "pooled.db"))
    try:
        child = RbiPlain(name="p")
        child.save(db=pooled)
        RbiHolder(name="h", untyped=child).save(db=pooled)
        got = RbiHolder.using(pooled).first()
    finally:
        pooled.close()
    assert got.untyped == {"name": "p", "_id": 1}


# --- a by-id read does not fail on the number of ids -----------------------------------------

MANY = 40_000


@pytest.mark.parametrize("cls", [RbiPlain, RbiSafe, RbiLiteJob, RbiLiteSafe], ids=lambda c: c.__name__)
def test_from_ids_and_find_documents_of_40000_ids(db, cls):
    """More ids than SQLite takes as variables in one statement: read in groups, order and
    repeats as asked."""
    cls.save_many([cls(name=f"n{i}") for i in range(MANY)])
    ids = [*range(MANY, 0, -1), 7, MANY + 5]
    got = cls.from_ids(ids)
    assert len(got) == MANY + 1
    assert [g._id for g in got] == ids[:-1]
    assert (got[0].name, got[-1].name, got[-2].name) == (f"n{MANY - 1}", "n6", "n0")
    docs = db.find_documents(cls.__tablename__, ids)
    assert len(docs) == MANY + 1
    assert [d["_id"] for d in docs] == ids[:-1]
    assert docs[-1]["name"] == "n6"


def test_ids_up_to_the_bound_are_one_variable_each_and_past_it_one_json_array(db):
    """Past the bound the bulk inserts use (999 variables) the ids go as one JSON array: still
    one statement, as the thousand-reference counts in the relation tests pin."""
    RbiPlain.save_many([RbiPlain(name=f"n{i}") for i in range(1000)])
    db.find_documents("rbi_plains", [1])  # learns the table's columns
    under = _reads(lambda: db.find_documents("rbi_plains", list(range(1, 1000))))
    assert [s.count("?") for s in under] == [999]
    over = _reads(lambda: db.find_documents("rbi_plains", list(range(1, 1001))))
    assert over == ["SELECT _id, data FROM rbi_plains WHERE _id IN (SELECT value FROM json_each(?));"]


# --- a save inside a transaction that rolls back ------------------------------------------


class RbiUndone(SQLerModel):
    __tablename__ = "rbi_undones"
    __promoted__ = {"status": "TEXT"}
    name: str = ""
    status: str = "unset"


def test_a_save_in_a_transaction_that_rolls_back_then_a_read_and_a_save_through_the_same_object(
    db, fresh, path
):
    """The table is made inside the transaction and goes with it; what the object learned
    there goes too, so the read after it is a missing table's and the save makes it again."""
    with pytest.raises(RuntimeError, match="undo"):
        with fresh:
            RbiUndone(name="a", status="gone").save(db=fresh)
            raise RuntimeError("undo")
    assert "rbi_undones" not in _tables(path)
    assert RbiUndone.from_ids([1], db=fresh) == []
    assert fresh.find_document("rbi_undones", 1) is None
    RbiUndone(name="b", status="ready").save(db=fresh)
    assert _column(path, "rbi_undones", "status") == "ready"
    assert fresh.find_document("rbi_undones", 1) == {"name": "b", "_id": 1, "status": "ready"}
    got = RbiUndone.from_ids([1], db=fresh)
    assert [(g.name, g.status) for g in got] == [("b", "ready")]
    got[0].name = "c"
    got[0].save(db=fresh)
    assert _stored(path, "rbi_undones", 1) == ("ready", '{"name":"c"}')
