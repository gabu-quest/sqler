"""``Model.beside(row)``: the queryset ``using`` returns, on the database (and, for a row of
the same class, the table) the row was read from or last written to.

Each test runs on all six bases. The body is written once as a coroutine; ``_maybe`` awaits
what an async base returns and passes a sync value through.
"""

import asyncio
import copy
import inspect
import pickle
import warnings
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
from sqler.exceptions import NotBoundError
from sqler.query import F


class BsPyJob(SQLerModel):
    __tablename__ = "bs_py_jobs"
    name: str = ""


class BsPyOrder(SQLerModel):
    __tablename__ = "bs_py_orders"
    name: str = ""


class BsPySub(BsPyJob):
    __tablename__ = "bs_py_subs"


@dataclass
class BsLiteJob(SQLerLiteModel):
    __tablename__ = "bs_lite_jobs"
    name: str = ""


@dataclass
class BsLiteOrder(SQLerLiteModel):
    __tablename__ = "bs_lite_orders"
    name: str = ""


@dataclass
class BsLiteSub(BsLiteJob):
    __tablename__ = "bs_lite_subs"


class BsMsJob(SQLerMsgspecModel):
    __tablename__ = "bs_ms_jobs"
    name: str = ""


class BsMsOrder(SQLerMsgspecModel):
    __tablename__ = "bs_ms_orders"
    name: str = ""


class BsMsSub(BsMsJob):
    __tablename__ = "bs_ms_subs"


class BsAsJob(AsyncSQLerModel):
    __tablename__ = "bs_as_jobs"
    name: str = ""


class BsAsOrder(AsyncSQLerModel):
    __tablename__ = "bs_as_orders"
    name: str = ""


class BsAsSub(BsAsJob):
    __tablename__ = "bs_as_subs"


@dataclass
class BsALiteJob(AsyncSQLerLiteModel):
    __tablename__ = "bs_alite_jobs"
    name: str = ""


@dataclass
class BsALiteOrder(AsyncSQLerLiteModel):
    __tablename__ = "bs_alite_orders"
    name: str = ""


@dataclass
class BsALiteSub(BsALiteJob):
    __tablename__ = "bs_alite_subs"


class BsAMsJob(AsyncSQLerMsgspecModel):
    __tablename__ = "bs_ams_jobs"
    name: str = ""


class BsAMsOrder(AsyncSQLerMsgspecModel):
    __tablename__ = "bs_ams_orders"
    name: str = ""


class BsAMsSub(BsAMsJob):
    __tablename__ = "bs_ams_subs"


# (job, order, subclass of job, the other kind's job, is async)
KITS = {
    "pydantic": (BsPyJob, BsPyOrder, BsPySub, BsAsJob, False),
    "lite": (BsLiteJob, BsLiteOrder, BsLiteSub, BsALiteJob, False),
    "msgspec": (BsMsJob, BsMsOrder, BsMsSub, BsAMsJob, False),
    "async-pydantic": (BsAsJob, BsAsOrder, BsAsSub, BsPyJob, True),
    "async-lite": (BsALiteJob, BsALiteOrder, BsALiteSub, BsLiteJob, True),
    "async-msgspec": (BsAMsJob, BsAMsOrder, BsAMsSub, BsMsJob, True),
}
KIND = pytest.mark.parametrize("kind", list(KITS))


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


async def _open(is_async):
    if is_async:
        d = AsyncSQLerDB.in_memory(shared=False)
        await d.connect()
        return d
    return SQLerDB.in_memory(shared=False)


def _run(kind, body):
    """Run ``body(kit, open_db)`` with the databases it opens closed afterwards."""
    job, order, sub, other, is_async = KITS[kind]
    opened = []

    async def open_db(of_async=is_async):
        d = await _open(of_async)
        opened.append(d)
        return d

    async def main():
        try:
            await body(job, order, sub, other, open_db)
        finally:
            for d in opened:
                await _maybe(d.close())

    asyncio.run(main())


# ----- where the row lives -------------------------------------------------------------


@KIND
def test_two_databases_with_the_same_id_each_reach_their_own(kind):
    async def body(job, order, sub, other, open_db):
        db1, db2 = await open_db(), await open_db()
        a = await _maybe(job(name="in-one").save(db=db1))
        b = await _maybe(job(name="in-two").save(db=db2))
        assert a._id == b._id == 1
        r1 = await _maybe(job.using(db1).first())
        r2 = await _maybe(job.using(db2).first())
        got1 = await _maybe(job.beside(r1).all())
        got2 = await _maybe(job.beside(r2).all())
        assert ([x.name for x in got1], [x.name for x in got2]) == (["in-one"], ["in-two"])

    _run(kind, body)


@KIND
def test_a_row_read_from_a_named_table_is_queried_in_that_table(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        await _maybe(job(name="live").save(db=db))
        await _maybe(db.insert_document("bs_archive", {"name": "old-1"}))
        await _maybe(db.insert_document("bs_archive", {"name": "old-2"}))
        row = await _maybe(job.using(db, "bs_archive").filter(F("name") == "old-1").first())
        got = await _maybe(job.beside(row).order_by("name").all())
        assert [x.name for x in got] == ["old-1", "old-2"]

    _run(kind, body)


@KIND
def test_a_row_saved_with_db_and_never_read_is_queried_there(kind):
    async def body(job, order, sub, other, open_db):
        db, other_db = await open_db(), await open_db()
        await _maybe(job(name="elsewhere").save(db=other_db))
        row = await _maybe(job(name="here").save(db=db))
        got = await _maybe(job.beside(row).all())
        assert [x.name for x in got] == ["here"]

    _run(kind, body)


@KIND
def test_a_refreshed_row_keeps_its_place(kind):
    async def body(job, order, sub, other, open_db):
        db, other_db = await open_db(), await open_db()
        await _maybe(job(name="elsewhere").save(db=other_db))
        row = await _maybe(job(name="here").save(db=db))
        await _maybe(row.refresh())
        got = await _maybe(job.beside(row).all())
        assert [x.name for x in got] == ["here"]

    _run(kind, body)


@KIND
def test_a_deep_copied_row_keeps_its_place(kind):
    async def body(job, order, sub, other, open_db):
        db, other_db = await open_db(), await open_db()
        await _maybe(job(name="elsewhere").save(db=other_db))
        row = copy.deepcopy(await _maybe(job(name="here").save(db=db)))
        got = await _maybe(job.beside(row).all())
        assert [x.name for x in got] == ["here"]

    _run(kind, body)


@KIND
def test_a_row_of_another_class_gives_the_callers_table_in_its_database(kind):
    async def body(job, order, sub, other, open_db):
        db, other_db = await open_db(), await open_db()
        await _maybe(order(name="theirs").save(db=other_db))
        await _maybe(order(name="ours").save(db=db))
        user = await _maybe(job(name="u").save(db=db))
        got = await _maybe(order.beside(user).all())
        assert [x.name for x in got] == ["ours"]

    _run(kind, body)


@KIND
def test_a_subclass_row_given_to_its_parent_gives_the_parents_table(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        await _maybe(job(name="parent-row").save(db=db))
        row = await _maybe(sub(name="sub-row").save(db=db))
        got = await _maybe(job.beside(row).all())
        assert [x.name for x in got] == ["parent-row"]

    _run(kind, body)


@KIND
def test_a_parent_row_given_to_its_subclass_gives_the_subclass_table(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        await _maybe(sub(name="sub-row").save(db=db))
        row = await _maybe(job(name="parent-row").save(db=db))
        got = await _maybe(sub.beside(row).all())
        assert [x.name for x in got] == ["sub-row"]

    _run(kind, body)


@KIND
def test_rows_read_through_beside_have_that_home(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        await _maybe(db.insert_document("bs_archive", {"name": "old"}))
        row = await _maybe(job.using(db, "bs_archive").first())
        again = await _maybe(job.beside(row).first())
        again.name = "renamed"
        await _maybe(again.save())
        doc = await _maybe(db.find_document("bs_archive", again._id))
        assert doc["name"] == "renamed"

    _run(kind, body)


@KIND
def test_update_and_delete_work_on_the_queryset(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        row = await _maybe(job(name="a").save(db=db))
        await _maybe(job(name="b").save(db=db))
        await _maybe(job.beside(row).filter(F("name") == "a").update(name="z"))
        await _maybe(job.beside(row).filter(F("name") == "b").delete())
        got = await _maybe(job.using(db).all())
        assert [x.name for x in got] == ["z"]

    _run(kind, body)


# ----- the errors ------------------------------------------------------------------------


@KIND
def test_a_never_saved_row_raises_not_bound(kind):
    async def body(job, order, sub, other, open_db):
        with pytest.raises(NotBoundError) as e:
            job.beside(job(name="new"))
        assert str(e.value) == (
            f"{job.__name__} instance has no database: it was never read or saved, or it was "
            "unpickled. Use .using(db) for a query on a database."
        )

    _run(kind, body)


@KIND
def test_an_unpickled_row_raises_not_bound(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        row = pickle.loads(pickle.dumps(await _maybe(job(name="p").save(db=db))))
        with pytest.raises(NotBoundError) as e:
            job.beside(row)
        assert str(e.value) == (
            f"{job.__name__} instance has no database: it was never read or saved, or it was "
            "unpickled. Use .using(db) for a query on a database."
        )

    _run(kind, body)


@KIND
def test_a_set_db_binding_is_not_a_fallback(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        saved = {k: order.__dict__[k] for k in ("_db", "_table") if k in order.__dict__}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            await _maybe(order.set_db(db))
        try:
            with pytest.raises(NotBoundError):
                order.beside(order(name="new"))
        finally:
            for k in ("_db", "_table"):
                if k in saved:
                    setattr(order, k, saved[k])
                else:
                    delattr(order, k)

    _run(kind, body)


@KIND
def test_a_row_of_the_other_kinds_database_raises_type_error(kind):
    async def body(job, order, sub, other, open_db):
        _, _, _, _, is_async = KITS[kind]
        odb = await open_db(not is_async)
        row = await _maybe(other(name="o").save(db=odb))
        takes, given = ("an AsyncSQLerDB", "SQLerDB") if is_async else ("a SQLerDB", "AsyncSQLerDB")
        with pytest.raises(TypeError) as e:
            job.beside(row)
        assert str(e.value) == (
            f"{job.__name__} takes {takes} as its database, not an object of type {given}: "
            "a sync model reads and writes through a SQLerDB, an async model through an "
            "AsyncSQLerDB."
        )

    _run(kind, body)


@pytest.mark.parametrize("arg", [None, {"_id": 1}], ids=["none", "dict"])
@KIND
def test_an_argument_that_is_no_model_raises_type_error(kind, arg):
    async def body(job, order, sub, other, open_db):
        with pytest.raises(TypeError) as e:
            job.beside(arg)
        assert str(e.value) == (
            f"{job.__name__}.beside() takes a model instance, not an object of type "
            f"{type(arg).__name__}."
        )

    _run(kind, body)


# ----- the README example (kept the same as the README's "Quick start" one) -------------


class BsReadmeUser(SQLerModel):
    __tablename__ = "users"
    name: str


def test_readme_beside_example():
    kyoto, osaka = SQLerDB.in_memory(), SQLerDB.in_memory()
    try:
        BsReadmeUser(name="Aiko").save(db=kyoto)
        BsReadmeUser(name="Ren").save(db=osaka)

        ren = BsReadmeUser.using(osaka).first()
        assert [u.name for u in BsReadmeUser.beside(ren).all()] == ["Ren"]
    finally:
        kyoto.close()
        osaka.close()
