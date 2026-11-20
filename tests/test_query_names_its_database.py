"""``db()`` on a query and on a queryset: the database its next read or write uses, as
``Model.db()`` names the one a model class is bound to.

The model tests run on all six bases (the kits of ``test_beside``). ``db()`` is a plain method
on the async classes too: it does no I/O.
"""

import asyncio
import inspect
import warnings

import pytest
from test_beside import KIND, KITS, _maybe, _run

from sqler import AsyncSQLerDB, SQLerDB, SQLerModel
from sqler.exceptions import NoAdapterError, NotBoundError
from sqler.models.async_queryset import AsyncSQLerQuerySet
from sqler.models.queryset import SQLerQuerySet
from sqler.query import F, SQLerQuery
from sqler.query.async_query import AsyncSQLerQuery


@KIND
def test_using_names_the_database_it_was_given(kind):
    async def body(job, order, sub, other, open_db):
        db1, db2 = await open_db(), await open_db()
        assert job.using(db1).db() is db1
        assert job.using(db2).db() is db2

    _run(kind, body)


@KIND
def test_using_another_database_leaves_the_models_own_binding_alone(kind):
    async def body(job, order, sub, other, open_db):
        db1, db2 = await open_db(), await open_db()
        saved = {k: order.__dict__[k] for k in ("_db", "_table") if k in order.__dict__}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            await _maybe(order.set_db(db1))
        try:
            assert order.query().db() is db1
            assert order.using(db2).db() is db2
            assert order.db() is db1
        finally:
            for k in ("_db", "_table"):
                if k in saved:
                    setattr(order, k, saved[k])
                else:
                    delattr(order, k)

    _run(kind, body)


@KIND
def test_the_model_query_names_the_models_database(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        saved = {k: order.__dict__[k] for k in ("_db", "_table") if k in order.__dict__}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            await _maybe(order.set_db(db))
        try:
            assert order.query().db() is order.db() is db
        finally:
            for k in ("_db", "_table"):
                if k in saved:
                    setattr(order, k, saved[k])
                else:
                    delattr(order, k)

    _run(kind, body)


@KIND
def test_beside_names_the_database_the_row_lives_in(kind):
    async def body(job, order, sub, other, open_db):
        db1, db2 = await open_db(), await open_db()
        await _maybe(job(name="one").save(db=db1))
        await _maybe(job(name="two").save(db=db2))
        row = await _maybe(job.using(db2).first())
        assert job.beside(row).db() is db2
        assert job.beside(row).db() is not db1

    _run(kind, body)


@KIND
def test_the_answer_survives_chaining_and_cloning(kind):
    async def body(job, order, sub, other, open_db):
        db1, db2 = await open_db(), await open_db()
        await _maybe(job(name="two").save(db=db2))
        qs = job.using(db2)
        chained = qs.filter(F("name") == "two").order_by("name").limit(3).resolve(False)
        assert chained.db() is db2
        assert qs.exclude(F("name") == "x").distinct().group_by("name").db() is db2
        assert qs._copy(qs._query).db() is db2
        got = await _maybe(chained.all())
        assert [x.name for x in got] == ["two"]
        assert job.using(db1).filter(F("name") == "two").db() is db1

    _run(kind, body)


@KIND
def test_db_is_a_plain_method_on_the_queryset(kind):
    async def body(job, order, sub, other, open_db):
        db = await open_db()
        qs = job.using(db)
        assert not inspect.iscoroutinefunction(type(qs).db)
        assert not inspect.isawaitable(qs.db())

    _run(kind, body)


@KIND
def test_a_model_never_bound_has_no_query(kind):
    async def body(job, order, sub, other, open_db):
        with pytest.raises(NotBoundError):
            job.query()

    _run(kind, body)


# ----- a query or queryset built with no database ----------------------------------------


@pytest.mark.parametrize("query_cls", [SQLerQuery, AsyncSQLerQuery])
def test_a_query_with_no_database_raises_no_adapter_error(query_cls):
    with pytest.raises(NoAdapterError, match="^No database set for query$"):
        query_cls("t").db()


@pytest.mark.parametrize("query_cls", [SQLerQuery, AsyncSQLerQuery])
def test_a_query_names_the_database_it_was_given(query_cls):
    sentinel = object()
    assert query_cls("t")._clone(db=sentinel).db() is sentinel
    assert query_cls("t")._clone(db=sentinel).filter(F("a") == 1).limit(2).db() is sentinel


def test_a_query_from_a_database_names_that_database():
    db = SQLerDB.in_memory()
    try:
        assert db.query("t").db() is db
        assert db.query("t").filter(F("a") == 1).limit(2).db() is db
    finally:
        db.close()


def test_an_async_query_from_a_database_names_that_database():
    async def body():
        db = AsyncSQLerDB.in_memory()
        await db.connect()
        try:
            query = await db.query("t")
            assert query.db() is db
            assert query.filter(F("a") == 1).limit(2).db() is db
        finally:
            await db.close()

    asyncio.run(body())


@pytest.mark.parametrize(
    "set_cls, query_cls", [(SQLerQuerySet, SQLerQuery), (AsyncSQLerQuerySet, AsyncSQLerQuery)]
)
def test_a_queryset_with_no_database_raises_what_its_query_raises(set_cls, query_cls):
    qs = set_cls(object, query_cls("t"))
    with pytest.raises(NoAdapterError, match="^No database set for query$"):
        qs.db()
    with pytest.raises(NoAdapterError, match="^No database set for query$"):
        qs.filter(F("a") == 1).limit(1).db()


@pytest.mark.parametrize(
    "set_cls, query_cls", [(SQLerQuerySet, SQLerQuery), (AsyncSQLerQuerySet, AsyncSQLerQuery)]
)
def test_a_queryset_over_a_query_with_a_database_names_it(set_cls, query_cls):
    sentinel = object()
    qs = set_cls(object, query_cls("t")._clone(db=sentinel))
    assert qs.db() is sentinel
    assert qs.limit(1).db() is sentinel


def test_the_kits_cover_six_bases():
    assert len(KITS) == 6


# ----- the README example (kept the same as the README's "beside" one) --------------------


class QnReadmeUser(SQLerModel):
    __tablename__ = "users"
    name: str


def test_readme_db_example():
    kyoto, osaka = SQLerDB.in_memory(), SQLerDB.in_memory()
    try:
        QnReadmeUser(name="Aiko").save(db=kyoto)
        QnReadmeUser(name="Ren").save(db=osaka)

        ren = QnReadmeUser.using(osaka).first()
        assert QnReadmeUser.beside(ren).db() is osaka
        assert QnReadmeUser.using(kyoto).filter(F("name") == "Aiko").db() is kyoto
    finally:
        kyoto.close()
        osaka.close()
