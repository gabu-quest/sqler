"""A unique index that refuses a row raises ``UniqueConstraintError`` (async adapter and AsyncSQLerDB)."""

import sqlite3

import pytest
import pytest_asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel, AsyncSQLerSafeModel, UniqueConstraintError


@pytest_asyncio.fixture
async def adb():
    d = AsyncSQLerDB.in_memory(shared=False)
    await d.connect()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def emails(adb):
    """Table ``users`` with a unique index on ``$.email`` and one row ``a@x``."""
    await adb.create_index("users", "email", unique=True)
    await adb.insert_document("users", {"email": "a@x"})
    return adb


async def _rows(db, sql):
    cur = await db.adapter.execute(sql)
    try:
        rows = await cur.fetchall()
    finally:
        await cur.close()
    await db.adapter.auto_commit()
    return [tuple(r) for r in rows]


async def _raw(db, sql):
    cur = await db.adapter.execute(sql)
    await cur.close()
    await db.adapter.auto_commit()


@pytest.mark.asyncio
async def test_insert_document(emails):
    with pytest.raises(UniqueConstraintError) as info:
        await emails.insert_document("users", {"email": "a@x"})
    err = info.value
    assert isinstance(err, sqlite3.IntegrityError)
    assert type(err.__cause__) is sqlite3.IntegrityError
    assert str(err) == "UNIQUE constraint failed: index 'idx_users_email'"
    assert (err.index, err.table, err.columns) == ("idx_users_email", None, [])


@pytest.mark.asyncio
async def test_upsert_document_update(emails):
    second = await emails.insert_document("users", {"email": "b@x"})
    with pytest.raises(UniqueConstraintError):
        await emails.upsert_document("users", second, {"email": "a@x"})


@pytest.mark.asyncio
async def test_raw_execute_and_executemany(adb):
    await _raw(adb, "CREATE TABLE t (a INTEGER, b INTEGER, UNIQUE (a, b))")
    await _raw(adb, "INSERT INTO t VALUES (1, 2)")
    with pytest.raises(UniqueConstraintError) as info:
        await adb.adapter.execute("INSERT INTO t VALUES (1, 2)")
    assert (info.value.table, info.value.columns) == ("t", ["a", "b"])
    with pytest.raises(UniqueConstraintError):
        await adb.adapter.executemany("INSERT INTO t VALUES (?, ?)", [[5, 5], [5, 5]])
    assert await _rows(adb, "SELECT a, b FROM t") == [(1, 2)]


@pytest.mark.asyncio
async def test_executescript(adb):
    with pytest.raises(UniqueConstraintError):
        await adb.adapter.executescript(
            "CREATE TABLE t (v TEXT UNIQUE); INSERT INTO t VALUES ('a'); INSERT INTO t VALUES ('a');"
        )


@pytest.mark.asyncio
async def test_promoted_and_versioned_writes(adb):
    await adb._ensure_table_with_promoted("jobs", {"ulid": "TEXT NOT NULL DEFAULT ''"})
    await adb._ensure_versioned_table("jobs")
    await _raw(adb, "CREATE UNIQUE INDEX jobs_ulid ON jobs (ulid)")
    await adb.upsert_document_promoted("jobs", None, {"ulid": "u1"}, ["ulid"])
    other, _ = await adb.upsert_with_version_promoted("jobs", None, {"ulid": "u2"}, None, ["ulid"])
    with pytest.raises(UniqueConstraintError) as info:
        await adb.upsert_document_promoted("jobs", None, {"ulid": "u1"}, ["ulid"])
    assert (info.value.table, info.value.columns) == ("jobs", ["ulid"])
    with pytest.raises(UniqueConstraintError):
        await adb.insert_many_promoted("jobs", [{"ulid": "u1"}], ["ulid"])
    with pytest.raises(UniqueConstraintError):
        await adb.upsert_with_version_promoted("jobs", None, {"ulid": "u1"}, None, ["ulid"])
    with pytest.raises(UniqueConstraintError):
        await adb.upsert_with_version_promoted("jobs", other, {"ulid": "u1"}, 0, ["ulid"])
    assert (await adb.find_document_with_version("jobs", other))["ulid"] == "u2"


@pytest.mark.asyncio
async def test_model_and_safe_model_save(adb):
    class APerson(AsyncSQLerModel):
        email: str

    class ASafePerson(AsyncSQLerSafeModel):
        email: str

    APerson.set_db(adb, "apeople")
    ASafePerson.set_db(adb, "asafe_people")
    await adb.create_index("apeople", "email", unique=True)
    await adb.create_index("asafe_people", "email", unique=True)
    await APerson(email="a@x").save()
    with pytest.raises(UniqueConstraintError):
        await APerson(email="a@x").save()
    await ASafePerson(email="a@x").save()
    second = await ASafePerson(email="b@x").save()
    second.email = "a@x"
    with pytest.raises(UniqueConstraintError):
        await ASafePerson(email="a@x").save()
    with pytest.raises(UniqueConstraintError):
        await second.save()


@pytest.mark.asyncio
async def test_except_driver_error_still_catches(emails):
    caught = None
    try:
        await emails.insert_document("users", {"email": "a@x"})
    except sqlite3.IntegrityError as exc:
        caught = exc
    assert type(caught) is UniqueConstraintError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ddl,insert,message",
    [
        ("CREATE TABLE t (v TEXT NOT NULL)", "INSERT INTO t VALUES (NULL)", "NOT NULL constraint failed: t.v"),
        ("CREATE TABLE t (v INTEGER CHECK (v > 0))", "INSERT INTO t VALUES (-1)", "CHECK constraint failed: v > 0"),
    ],
    ids=["not_null", "check"],
)
async def test_other_integrity_errors_unchanged(adb, ddl, insert, message):
    await _raw(adb, ddl)
    with pytest.raises(sqlite3.IntegrityError) as info:
        await adb.adapter.execute(insert)
    assert type(info.value) is sqlite3.IntegrityError
    assert str(info.value) == message


@pytest.mark.asyncio
async def test_insert_many_third_row_stores_nothing(emails):
    docs = [{"email": "b@x"}, {"email": "c@x"}, {"email": "a@x"}, {"email": "d@x"}]
    with pytest.raises(UniqueConstraintError):
        await emails.insert_many("users", docs)
    assert await _rows(emails, "SELECT data FROM users ORDER BY _id") == [('{"email":"a@x"}',)]


@pytest.mark.asyncio
async def test_violation_inside_transaction_keeps_it_open(emails):
    async with emails.transaction():
        await emails.insert_document("users", {"email": "b@x"})
        with pytest.raises(UniqueConstraintError):
            await emails.insert_document("users", {"email": "a@x"})
        assert emails.adapter.in_transaction is True
        await emails.insert_document("users", {"email": "c@x"})
    assert await _rows(emails, "SELECT data FROM users ORDER BY _id") == [
        ('{"email":"a@x"}',),
        ('{"email":"b@x"}',),
        ('{"email":"c@x"}',),
    ]


@pytest.mark.asyncio
async def test_violation_outside_transaction_returns_the_connection(emails):
    assert emails.pool_stats().max_connections == 1
    with pytest.raises(UniqueConstraintError):
        await emails.insert_document("users", {"email": "a@x"})
    stats = emails.pool_stats()
    assert (stats.available_connections, stats.in_use_connections) == (1, 0)
    assert await emails.insert_document("users", {"email": "z@x"}) == 2  # the failed row was rolled back


async def _trigger(db, table, text, when=""):
    await _raw(
        db,
        f"CREATE TRIGGER t_deny BEFORE INSERT ON {table} {when} BEGIN SELECT RAISE(ABORT, '{text}'); END",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["UNIQUE constraint failed: audit", "UNIQUE constraint failed: a.b"])
async def test_trigger_text_is_not_a_violation_insert_document(adb, text):
    await adb.create_index("audit_src", "k")
    await adb.insert_document("audit_src", {"k": 0})
    await _trigger(adb, "audit_src", text)
    with pytest.raises(sqlite3.IntegrityError) as info:
        await adb.insert_document("audit_src", {"k": 1})
    assert type(info.value) is sqlite3.IntegrityError
    assert str(info.value) == text


@pytest.mark.asyncio
async def test_trigger_text_is_not_a_violation_raw_execute(adb):
    await adb.insert_document("audit_src", {"k": 0})
    await _trigger(adb, "audit_src", "UNIQUE constraint failed: audit")
    with pytest.raises(sqlite3.IntegrityError) as info:
        await adb.adapter.execute("INSERT INTO audit_src (data) VALUES ('{}')")
    assert type(info.value) is sqlite3.IntegrityError


@pytest.mark.asyncio
async def test_trigger_text_is_not_a_violation_model_save(adb):
    class Row(AsyncSQLerModel):
        k: int

    Row.set_db(adb, "audit_src")
    await adb.insert_document("audit_src", {"k": 0})
    await _trigger(adb, "audit_src", "UNIQUE constraint failed: audit")
    with pytest.raises(sqlite3.IntegrityError) as info:
        await Row(k=1).save()
    assert type(info.value) is sqlite3.IntegrityError


@pytest.mark.asyncio
async def test_real_violation_on_a_table_with_the_trigger_is_converted(adb):
    await adb.create_index("u", "email", unique=True)
    await adb.insert_document("u", {"email": "a@x"})
    await _trigger(
        adb, "u", "UNIQUE constraint failed: audit", "WHEN json_extract(NEW.data, '$.email') = 'deny'"
    )
    with pytest.raises(UniqueConstraintError):
        await adb.insert_document("u", {"email": "a@x"})
    with pytest.raises(sqlite3.IntegrityError) as info:
        await adb.insert_document("u", {"email": "deny"})
    assert type(info.value) is sqlite3.IntegrityError
