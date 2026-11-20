"""``first_or_create`` under a unique index: the refused insert reads again.

Two callers with the same lookup started together give exactly one row and
exactly one ``created`` true. An insert refused by a unique index on a field
that is not in the lookup reaches the caller as ``UniqueConstraintError``.
"""

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass

import pytest

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerModel,
    AsyncSQLerMsgspecModel,
    SQLerDB,
    SQLerLiteModel,
    SQLerModel,
    SQLerMsgspecModel,
)
from sqler.adapter import AsyncSQLiteAdapter
from sqler.exceptions import UniqueConstraintError


class FuPy(SQLerModel):
    email: str = ""
    handle: str = ""
    name: str = ""


@dataclass
class FuLite(SQLerLiteModel):
    email: str = ""
    handle: str = ""
    name: str = ""


class FuMs(SQLerMsgspecModel):
    email: str = ""
    handle: str = ""
    name: str = ""


class FuAs(AsyncSQLerModel):
    email: str = ""
    handle: str = ""
    name: str = ""


@dataclass
class FuALite(AsyncSQLerLiteModel):
    email: str = ""
    handle: str = ""
    name: str = ""


class FuAMs(AsyncSQLerMsgspecModel):
    email: str = ""
    handle: str = ""
    name: str = ""


SYNC = [FuPy, FuLite, FuMs]
SYNC_IDS = ["pydantic", "lite", "msgspec"]
ASYNC = [FuAs, FuALite, FuAMs]
ASYNC_IDS = ["async-pydantic", "async-lite", "async-msgspec"]


def _sync_db(tmp_path, cls, *fields):
    db = SQLerDB.on_disk(str(tmp_path / "fu.db"))
    table = cls._resolve_binding(db)[1]
    for f in fields:
        db.create_index(table, f, unique=True)
    return db, table


async def _async_db(tmp_path, cls, *fields):
    db = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / "fu.db"), pool_size=2))
    await db.connect()
    table = cls._resolve_binding(db)[1]
    for f in fields:
        await db.create_index(table, f, unique=True)
    return db, table


ROUNDS = 20
THREADS = 6


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_threads_racing_on_the_same_lookup_create_one_row(tmp_path, cls):
    db, table = _sync_db(tmp_path, cls, "email")
    try:
        for rnd in range(ROUNDS):
            email = f"r{rnd}@x"
            barrier = threading.Barrier(THREADS, timeout=30)
            results: list = []
            errors: list = []

            def worker():
                try:
                    barrier.wait()
                    results.append(cls.first_or_create({"email": email}, {"name": "n"}, db=db))
                except BaseException as exc:  # recorded and asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert errors == []
            assert len(results) == THREADS
            assert sum(1 for _, created in results if created) == 1
            assert {inst._id for inst, _ in results} == {results[0][0]._id}
        assert cls.using(db).count() == ROUNDS
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_tasks_racing_on_the_same_lookup_create_one_row(tmp_path, cls):
    db, table = await _async_db(tmp_path, cls, "email")
    try:
        for rnd in range(ROUNDS):
            email = f"r{rnd}@x"
            start = asyncio.Event()

            async def caller():
                await start.wait()
                return await cls.first_or_create({"email": email}, {"name": "n"}, db=db)

            tasks = [asyncio.create_task(caller()) for _ in range(2)]
            start.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)

            assert sum(1 for _, created in results if created) == 1
            assert results[0][0]._id == results[1][0]._id
        assert await cls.using(db).count() == ROUNDS
    finally:
        await db.close()


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_insert_refused_by_another_unique_index_reaches_the_caller(tmp_path, cls):
    db, table = _sync_db(tmp_path, cls, "email", "handle")
    try:
        cls(email="a@x", handle="taken").save(db=db)
        with pytest.raises(UniqueConstraintError) as info:
            cls.first_or_create({"email": "b@x"}, {"handle": "taken"}, db=db)
        assert info.value.index == f"idx_{table}_handle"
        assert cls.using(db).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_insert_refused_by_another_unique_index_reaches_the_caller(tmp_path, cls):
    db, table = await _async_db(tmp_path, cls, "email", "handle")
    try:
        await cls(email="a@x", handle="taken").save(db=db)
        with pytest.raises(UniqueConstraintError):
            await cls.first_or_create({"email": "b@x"}, {"handle": "taken"}, db=db)
        assert await cls.using(db).count() == 1
    finally:
        await db.close()


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_without_a_unique_index_nothing_changes(tmp_path, cls):
    db = SQLerDB.on_disk(str(tmp_path / "plain.db"))
    try:
        first, created1 = cls.first_or_create({"email": "p@x"}, db=db)
        again, created2 = cls.first_or_create({"email": "p@x"}, db=db)
        assert (created1, created2) == (True, False)
        assert again._id == first._id
        assert cls.using(db).count() == 1
    finally:
        db.close()


# ----- inside the caller's own transaction ---------------------------------------------
#
# Another connection inserted the lookup's row first. Inside ``db.transaction()``
# first_or_create returns that row with ``created`` false, the caller's earlier write
# in the same transaction survives, and the transaction commits (another connection
# sees both rows afterwards).


def _other_connection_inserts(path, table, doc):
    other = sqlite3.connect(path, isolation_level=None)
    try:
        other.execute(f"INSERT INTO {table} (data) VALUES (json(?))", [json.dumps(doc)])
    finally:
        other.close()


def _committed_rows(path, table):
    reader = sqlite3.connect(path)
    try:
        rows = reader.execute(
            f"SELECT json_extract(data, '$.email'), json_extract(data, '$.name') FROM {table} ORDER BY _id"
        ).fetchall()
    finally:
        reader.close()
    return rows


@pytest.mark.parametrize("cls", SYNC, ids=SYNC_IDS)
def test_inside_the_callers_transaction_the_existing_row_is_returned(tmp_path, cls):
    db, table = _sync_db(tmp_path, cls, "email")
    path = str(tmp_path / "fu.db")
    try:
        _other_connection_inserts(path, table, {"email": "t@x", "handle": "", "name": "theirs"})
        with db.transaction():
            cls(email="mine@x", name="earlier").save(db=db)
            row, created = cls.first_or_create({"email": "t@x"}, {"name": "mine"}, db=db)
        assert (created, row.name, row._id) == (False, "theirs", 1)
        assert _committed_rows(path, table) == [("t@x", "theirs"), ("mine@x", "earlier")]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ASYNC, ids=ASYNC_IDS)
async def test_async_inside_the_callers_transaction_the_existing_row_is_returned(tmp_path, cls):
    db, table = await _async_db(tmp_path, cls, "email")
    path = str(tmp_path / "fu.db")
    try:
        _other_connection_inserts(path, table, {"email": "t@x", "handle": "", "name": "theirs"})
        async with db.transaction():
            await cls(email="mine@x", name="earlier").save(db=db)
            row, created = await cls.first_or_create({"email": "t@x"}, {"name": "mine"}, db=db)
        assert (created, row.name, row._id) == (False, "theirs", 1)
        assert _committed_rows(path, table) == [("t@x", "theirs"), ("mine@x", "earlier")]
    finally:
        await db.close()
