"""A model query copies the rows it matches into another table (``copy_to``) and deletes
them (``delete``), on every base, sync and async.

``copy_to(table)`` copies the rows ``all()`` would return, as they are stored (``_id``,
``data``, ``_version``, every promoted column), in one ``INSERT ... SELECT``; the target is
made in the source's layout or brought to it, and refused where the rows cannot go into it as
they are. ``delete()`` is ``delete_all()`` under its approved name. Both refuse a query with a
limit or an offset. Every test runs its calls through ``_v``, which awaits what an async base
returns, so one body covers the ten bases.

The fixture rows (``_seed``): five jobs ``n`` 0 to 4, ``tag`` ``t<n>``, ``done`` for an even
``n``; their ids are 1 to 5. The ``done`` filter matches ids 1, 3 and 5.
"""

import asyncio
import inspect
import itertools
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerMsgspecModel,
    AsyncSQLerSafeModel,
    CacheAwareModel,
    FTSIndex,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteModelBase,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
    UniqueConstraintError,
    cached_query,
)
from sqler.exceptions import InvalidTableNameError, ReferentialIntegrityError
from sqler.query import F

BASES = {
    "pydantic": SQLerModel,
    "pydantic-safe": SQLerSafeModel,
    "async-pydantic": AsyncSQLerModel,
    "async-pydantic-safe": AsyncSQLerSafeModel,
    "lite": SQLerLiteModel,
    "lite-safe": SQLerLiteSafeModel,
    "async-lite": AsyncSQLerLiteModel,
    "async-lite-safe": AsyncSQLerLiteSafeModel,
    "msgspec": SQLerMsgspecModel,
    "async-msgspec": AsyncSQLerMsgspecModel,
}
SAFE = {"pydantic-safe", "async-pydantic-safe", "lite-safe", "async-lite-safe"}
PYDANTIC = ["pydantic", "async-pydantic"]
# One base of every other kind: lite, lite async, msgspec, msgspec async, and a safe base.
OTHER_KINDS = ["lite", "async-lite", "msgspec", "async-msgspec", "pydantic-safe"]


def _refusal_rows(*cases: Any) -> list:
    """The rows of a refusal test. A refusal is written once, behind the two query set classes
    every base reaches: every case runs on one sync and one async pydantic base, and the first
    case on one base of each other kind, which proves that kind's query set reaches it."""
    return [(k, c) for k in PYDANTIC for c in cases] + [(k, cases[0]) for k in OTHER_KINDS]


_names = itertools.count()


async def _v(value: Any) -> Any:
    """``value``, awaited when an async base returned an awaitable."""
    return await value if inspect.isawaitable(value) else value


def _is_async(kind: str) -> bool:
    return kind.startswith("async")


def _model(kind: str, fields: dict[str, tuple[Any, Any]], *, table: str, mixins: tuple = (), extra: Optional[dict] = None) -> type:
    """A class of base ``kind`` storing in ``table`` with ``fields`` ({name: (annotation,
    default)}), published in this module so a string annotation can name it."""
    base = BASES[kind]
    name = f"Ct{next(_names)}"

    def body(ns):
        ns["__module__"] = __name__
        ns["__annotations__"] = {k: t for k, (t, _) in fields.items()}
        for k, (_, d) in fields.items():
            ns[k] = d
        ns["__tablename__"] = table
        ns.update(extra or {})

    cls = types.new_class(name, (*mixins, base), {}, body)
    if issubclass(base, SQLerLiteModelBase):
        cls = dataclass(cls)
    globals()[name] = cls
    return cls


JOB = {"n": (int, 0), "done": (bool, False), "tag": (str, "")}


def _job(kind: str, table: str = "ct_jobs", **kw) -> type:
    return _model(kind, JOB, table=table, **kw)


async def _db(kind: str, path: Optional[str] = None):
    if _is_async(kind):
        db = AsyncSQLerDB.on_disk(path) if path else AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        return db
    return SQLerDB.on_disk(path) if path else SQLerDB.in_memory(shared=False)


async def _close(db) -> None:
    await _v(db.close())


async def _raw(db, sql: str, params: Optional[list] = None) -> list[tuple]:
    """The rows of ``sql`` read with the database's adapter."""
    cur = await _v(db.adapter.execute(sql, params or []))
    rows = await _v(cur.fetchall())
    return [tuple(r) for r in rows]


async def _columns(db, table: str) -> list[str]:
    return [r[1] for r in await _raw(db, f'PRAGMA table_xinfo("{table}")')]


async def _seed(cls, db) -> None:
    for n in range(5):
        await _v(cls(n=n, done=n % 2 == 0, tag=f"t{n}").save(db=db))


def _rows(models) -> list[tuple]:
    return [(m._id, m.n, m.done, m.tag) for m in models]


DONE_ROWS = [(1, 0, True, "t0"), (3, 2, True, "t2"), (5, 4, True, "t4")]


class _Trace:
    """Every statement sent on the database's connections while it is entered."""

    def __init__(self, db):
        self.db = db
        self.sent: list[str] = []

    async def __aenter__(self):
        if isinstance(self.db, AsyncSQLerDB):
            for conn in self.db.adapter._all_conns:
                await conn.set_trace_callback(self.sent.append)
        else:
            self.db.adapter._conn().set_trace_callback(self.sent.append)
        return self.sent

    async def __aexit__(self, *exc):
        if isinstance(self.db, AsyncSQLerDB):
            for conn in self.db.adapter._all_conns:
                await conn.set_trace_callback(None)
        else:
            self.db.adapter._conn().set_trace_callback(None)
        return False


@pytest.fixture(params=list(BASES))
def kind(request) -> str:
    return request.param


# ----- what copy_to copies ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_to_copies_the_matching_rows_with_their_ids(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        copied = await _v(cls.using(db).filter(F("done") == True).copy_to("ct_archive"))  # noqa: E712
        archived = await _v(cls.using(db, "ct_archive").order_by("n").all())
        assert (copied, _rows(archived), await _v(cls.using(db).count())) == (3, DONE_ROWS, 5)
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_copy_to_keeps_versions_and_promoted_columns(kind):
    db = await _db(kind)
    try:
        cls = _job(kind, extra={"__promoted__": {"tag": "TEXT NOT NULL DEFAULT ''"}})
        await _seed(cls, db)
        if kind in SAFE:  # row 3 saved twice more: version 2
            row = await _v(cls.from_id(3, db=db))
            await _v(row.save(db=db))
            await _v(row.save(db=db))
        await _v(cls.using(db).filter(F("done") == True).copy_to("ct_archive"))  # noqa: E712
        source = await _raw(db, "SELECT * FROM ct_jobs WHERE _id IN (1, 3, 5) ORDER BY _id")
        target = await _raw(db, "SELECT * FROM ct_archive ORDER BY _id")
        expected_columns = ["_id", "data", "tag", "_version"] if kind in SAFE else ["_id", "data", "tag"]
        assert await _columns(db, "ct_archive") == expected_columns
        assert target == source
        if kind in SAFE:
            assert [r[3] for r in target] == [0, 2, 0]
    finally:
        await _close(db)


@pytest.mark.parametrize("kind", PYDANTIC)
@pytest.mark.asyncio
async def test_copy_to_is_one_insert_select_and_reads_no_row(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        await _v(db.ensure_table("ct_archive"))
        async with _Trace(db) as sent:
            await _v(cls.using(db).filter(F("done") == True).copy_to("ct_archive"))  # noqa: E712
        writes = [s for s in sent if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "SELECT"))]
        guard = "CASE WHEN 1 THEN 1 ELSE (SELECT (`_id`, `data`) IN (SELECT * FROM ct_jobs) FROM ct_jobs) END"
        assert writes == [
            "INSERT INTO ct_archive (_id, data) SELECT _id, data FROM ct_jobs "
            f"WHERE JSON_EXTRACT(data, '$.done') = 1 AND {guard}"
        ]
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_copy_to_through_exclude_and_or_filter(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        q = cls.using(db).exclude(F("done") == True).or_filter(F("n") == 4)  # noqa: E712
        copied = await _v(q.copy_to("ct_archive"))
        archived = await _v(cls.using(db, "ct_archive").order_by("n").all())
        assert (copied, _rows(archived)) == (3, [(2, 1, False, "t1"), (4, 3, False, "t3"), (5, 4, True, "t4")])
    finally:
        await _close(db)


@pytest.mark.parametrize("kind", PYDANTIC)
@pytest.mark.asyncio
async def test_copy_to_through_a_reference_filter_from_the_class_table_and_from_another(kind):
    db = await _db(kind)
    try:
        owner_cls = _model(kind, {"name": (str, "")}, table="ct_owners")
        pet_cls = _model(kind, {"name": (str, ""), "owner": (Optional[owner_cls], None)}, table="ct_pets")
        a = await _v(owner_cls(name="a").save(db=db))
        b = await _v(owner_cls(name="b").save(db=db))
        for name, owner in (("p1", a), ("p2", b), ("p3", a)):
            await _v(pet_cls(name=name, owner=owner).save(db=db))
        of_a = pet_cls.ref("owner").field("name") == "a"
        first = await _v(pet_cls.using(db).filter(of_a).copy_to("ct_pets_archive"))
        second = await _v(pet_cls.using(db, "ct_pets_archive").filter(of_a).copy_to("ct_pets_cold"))
        cold = await _v(pet_cls.using(db, "ct_pets_cold").order_by("name").all())
        assert (first, second, [(p._id, p.name, p.owner.name) for p in cold]) == (2, 2, [(1, "p1", "a"), (3, "p3", "a")])
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_copy_to_with_order_by_copies_every_match(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        copied = await _v(cls.using(db).filter(F("done") == True).order_by("n", desc=True).copy_to("ct_archive"))  # noqa: E712
        assert copied == 3
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_a_filter_that_matches_nothing_copies_0_and_makes_the_target(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        copied = await _v(cls.using(db).filter(F("n") == 99).copy_to("ct_archive"))
        assert (copied, await _columns(db, "ct_archive")) == (
            0,
            ["_id", "data", "_version"] if kind in SAFE else ["_id", "data"],
        )
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_a_copy_of_a_missing_table_copies_0_and_makes_nothing(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        copied = await _v(cls.using(db).copy_to("ct_archive"))
        assert (copied, await _columns(db, "ct_archive")) == (0, [])
    finally:
        await _close(db)


# ----- refusals ----------------------------------------------------------------------


@pytest.mark.parametrize("kind,name", _refusal_rows("ct'archive", "ct archive", "ct;archive", ""))
@pytest.mark.asyncio
async def test_a_target_name_sqler_does_not_take_is_refused_before_any_statement(kind, name):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        async with _Trace(db) as sent:
            with pytest.raises(InvalidTableNameError, match="(?i)^(invalid )?table name"):
                await _v(cls.using(db).copy_to(name))
        assert sent == []
    finally:
        await _close(db)


@pytest.mark.parametrize("kind,name", _refusal_rows("ct_jobs", "CT_JOBS"))
@pytest.mark.asyncio
async def test_the_source_table_as_target_is_refused(kind, name):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        with pytest.raises(ValueError, match=f"^copy_to: {name} is the table the query reads"):
            await _v(cls.using(db).copy_to(name))
        assert await _v(cls.using(db).count()) == 5
    finally:
        await _close(db)


_WHOLE_ROWS = [
    (lambda q: q.limit(2), "a limit or an offset"),
    (lambda q: q.offset(1), "a limit or an offset"),
    (lambda q: q.order_by("n").limit(2).offset(1), "a limit or an offset"),
    (lambda q: q.select("n"), r"select\(\)"),
    (lambda q: q.group_by("done"), r"group_by\(\)"),
]


@pytest.mark.parametrize(
    "kind,case",
    _refusal_rows(*_WHOLE_ROWS),
    ids=lambda v: v if isinstance(v, str) else None,
)
@pytest.mark.asyncio
async def test_copy_to_refuses_a_query_whose_rows_are_not_its_whole_matches(kind, case):
    shape, word = case
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        async with _Trace(db) as sent:
            with pytest.raises(ValueError, match=f"^copy_to: .*{word}"):
                await _v(shape(cls.using(db)).copy_to("ct_archive"))
        assert sent == []
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_a_second_copy_fails_whole_and_the_target_keeps_the_first(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        q = cls.using(db).filter(F("done") == True)  # noqa: E712
        await _v(q.copy_to("ct_archive"))
        await _v(cls(n=6, done=True, tag="t6").save(db=db))  # a new match: id 6
        with pytest.raises(UniqueConstraintError, match=r"^UNIQUE constraint failed: ct_archive\._id$"):
            await _v(q.copy_to("ct_archive"))
        assert await _raw(db, "SELECT _id FROM ct_archive ORDER BY _id") == [(1,), (3,), (5,)]
    finally:
        await _close(db)


# ----- the target table ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_existing_target_without_a_promoted_column_of_the_source_gets_it(kind):
    db = await _db(kind)
    try:
        cls = _job(kind, extra={"__promoted__": {"tag": "TEXT"}})
        await _seed(cls, db)
        await _v(db.ensure_table("ct_archive", versioned=kind in SAFE))
        await _v(cls.using(db).filter(F("done") == True).copy_to("ct_archive"))  # noqa: E712
        assert await _raw(db, "SELECT _id, tag FROM ct_archive ORDER BY _id") == [(1, "t0"), (3, "t2"), (5, "t4")]
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_a_target_with_a_promoted_column_the_source_lacks_is_refused(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        await _v(db.ensure_table("ct_archive", promoted={"owner": "TEXT"}, versioned=kind in SAFE))
        with pytest.raises(ValueError, match=r"^copy_to: ct_archive has the column owner, which ct_jobs has not"):
            await _v(cls.using(db).copy_to("ct_archive"))
        assert await _raw(db, "SELECT COUNT(*) FROM ct_archive") == [(0,)]
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_a_target_versioned_unlike_the_source_is_refused(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        await _v(db.ensure_table("ct_archive", versioned=kind not in SAFE))
        said = "has no _version column and ct_jobs has" if kind in SAFE else "has a _version column and ct_jobs has not"
        with pytest.raises(ValueError, match=f"^copy_to: ct_archive {said}"):
            await _v(cls.using(db).copy_to("ct_archive"))
        assert await _raw(db, "SELECT COUNT(*) FROM ct_archive") == [(0,)]
    finally:
        await _close(db)


@pytest.mark.parametrize("kind", PYDANTIC)
@pytest.mark.asyncio
async def test_a_new_target_takes_the_class_checks(kind):
    db = await _db(kind)
    try:
        cls = _job(
            kind,
            extra={"__promoted__": {"n": "INTEGER NOT NULL DEFAULT 0"}, "__checks__": {"n_small": "n < 100"}},
        )
        await _seed(cls, db)
        await _v(cls.using(db).copy_to("ct_archive"))
        with pytest.raises(Exception, match="CHECK constraint failed"):
            await _v(db.adapter.execute("INSERT INTO ct_archive (data, n) VALUES ('{}', 500)"))
    finally:
        await _close(db)


# ----- what is stored in a row ------------------------------------------------------------


@dataclass
class CtHome:
    city: str = ""
    since: Optional[datetime] = None


@pytest.mark.parametrize("kind", ["pydantic", "async-pydantic", "lite", "async-lite"])
@pytest.mark.asyncio
async def test_a_row_with_a_datetime_a_dataclass_and_a_reference_loads_equal_from_the_target(kind):
    db = await _db(kind)
    try:
        owner_cls = _model(kind, {"name": (str, "")}, table="ct_owners")
        when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        holder_cls = _model(
            kind,
            {"when": (Optional[datetime], None), "home": (Optional[CtHome], None), "owner": (Optional[owner_cls], None)},
            table="ct_holders",
        )
        owner = await _v(owner_cls(name="ada").save(db=db))
        await _v(holder_cls(when=when, home=CtHome("Kyoto", when), owner=owner).save(db=db))
        await _v(holder_cls.using(db).copy_to("ct_holders_archive"))
        (loaded,) = await _v(holder_cls.using(db, "ct_holders_archive").all())
        assert (loaded._id, loaded.when, loaded.home, loaded.owner._id, loaded.owner.name) == (
            1,
            when,
            CtHome("Kyoto", when),
            1,
            "ada",
        )
    finally:
        await _close(db)


# ----- delete() ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_deletes_every_match_and_returns_the_count(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        deleted = await _v(cls.using(db).filter(F("done") == True).order_by("n").delete())  # noqa: E712
        left = await _v(cls.using(db).order_by("n").all())
        assert (deleted, _rows(left)) == (3, [(2, 1, False, "t1"), (4, 3, False, "t3")])
    finally:
        await _close(db)


@pytest.mark.parametrize("kind,shape", _refusal_rows(lambda q: q.limit(2), lambda q: q.offset(1)))
@pytest.mark.asyncio
async def test_delete_refuses_a_limit_or_an_offset(kind, shape):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        with pytest.raises(ValueError, match=r"^delete: a query with a limit or an offset"):
            await _v(shape(cls.using(db).order_by("n")).delete())
        assert await _v(cls.using(db).count()) == 5
    finally:
        await _close(db)


@pytest.mark.parametrize("call", ["delete", "delete_all"])
@pytest.mark.parametrize(
    "policy,expected",
    [
        ("restrict", "refused"),
        ("set_null", (1, [("ct_owner_b", None)], ["b"])),
        ("cascade", (1, [], ["b"])),
    ],
)
@pytest.mark.parametrize("kind", PYDANTIC)
@pytest.mark.asyncio
async def test_delete_meets_the_policy_as_delete_all_does(kind, call, policy, expected):
    db = await _db(kind)
    try:
        owner_cls = _model(kind, {"name": (str, "")}, table="ct_owners")
        pet_cls = _model(kind, {"name": (str, ""), "owner": (Optional[owner_cls], None)}, table="ct_pets")
        a = await _v(owner_cls(name="a").save(db=db))
        await _v(owner_cls(name="b").save(db=db))
        await _v(pet_cls(name="ct_owner_b", owner=a).save(db=db))  # outside the set, refers into it
        q = owner_cls.using(db).filter(F("name") == "a")
        if expected == "refused":
            with pytest.raises(ReferentialIntegrityError):
                await _v(getattr(q, call)(on_delete=policy))
            assert await _v(owner_cls.using(db).count()) == 2
            return
        deleted = await _v(getattr(q, call)(on_delete=policy))
        pets = await _v(pet_cls.using(db).all())
        owners = await _v(owner_cls.using(db).all())
        assert (deleted, [(p.name, p.owner) for p in pets], [o.name for o in owners]) == expected
    finally:
        await _close(db)


# ----- copy then delete: one unit when the caller makes it one -------------------------


async def _in_transaction(db, body, fail: bool):
    class _Stop(Exception):
        pass

    try:
        if isinstance(db, AsyncSQLerDB):
            async with db.transaction():
                await body()
                if fail:
                    raise _Stop
        else:
            with db.transaction():
                await body()
                if fail:
                    raise _Stop
    except _Stop:
        pass


@pytest.mark.parametrize("fail,expected", [(False, (2, 3)), (True, (5, 0))], ids=["commit", "rollback"])
@pytest.mark.asyncio
async def test_copy_then_delete_inside_a_transaction_is_one_unit(kind, fail, expected):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        await _v(db.ensure_table("ct_archive", versioned=kind in SAFE))
        q = cls.using(db).filter(F("done") == True)  # noqa: E712

        async def body():
            await _v(q.copy_to("ct_archive"))
            await _v(q.delete())

        await _in_transaction(db, body, fail)
        assert (await _v(cls.using(db).count()), await _v(cls.using(db, "ct_archive").count())) == expected
    finally:
        await _close(db)


@pytest.mark.asyncio
async def test_a_copy_rolled_back_leaves_no_target_table(kind):
    """The target the copy made inside the transaction goes with its rollback (SQLite's
    CREATE TABLE is part of the transaction)."""
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)

        async def body():
            await _v(cls.using(db).filter(F("done") == True).copy_to("ct_archive"))  # noqa: E712

        await _in_transaction(db, body, True)
        assert await _columns(db, "ct_archive") == []
        assert await _v(cls.using(db).filter(F("done") == True).copy_to("ct_archive")) == 3  # noqa: E712
    finally:
        await _close(db)


@pytest.mark.parametrize("kind", PYDANTIC)
@pytest.mark.asyncio
async def test_a_second_database_object_on_the_file_reads_the_copy(kind, tmp_path):
    path = str(tmp_path / "ct.db")
    db = await _db(kind, path)
    other = await _db(kind, path)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        await _v(cls.using(other).count())  # the other object has seen the source
        await _v(cls.using(db).filter(F("done") == True).copy_to("ct_archive"))  # noqa: E712
        assert _rows(await _v(cls.using(other, "ct_archive").order_by("n").all())) == DONE_ROWS
    finally:
        await _close(other)
        await _close(db)


@pytest.mark.asyncio
async def test_eight_tasks_copying_disjoint_filters_into_one_new_target_copy_every_row_once(tmp_path):
    for run in range(20):
        db = AsyncSQLerDB.on_disk(str(tmp_path / f"ct{run}.db"))
        await db.connect()
        try:
            cls = _job("async-pydantic")
            await cls.save_many([cls(n=n, tag=f"t{n}") for n in range(80)], db=db)
            counts = await asyncio.gather(
                *(cls.using(db).filter((F("n") >= 10 * k) & (F("n") < 10 * k + 10)).copy_to("ct_archive") for k in range(8))
            )
            ids = await _raw(db, "SELECT _id FROM ct_archive ORDER BY _id")
            assert (counts, ids) == ([10] * 8, [(i,) for i in range(1, 81)])
        finally:
            await db.close()


# ----- what a write does besides the rows ---------------------------------------------


def test_a_cached_query_of_the_target_class_reads_the_copy():
    db = SQLerDB.in_memory(shared=False)
    try:
        cls = _job("pydantic")
        archive_cls = _job("pydantic", table="ct_cached_archive", mixins=(CacheAwareModel,))
        db.ensure_table("ct_cached_archive")

        @cached_query(table=archive_cls)
        def archived() -> int:
            return archive_cls.using(db).count()

        _run_sync_seed(cls, db)
        before = archived()
        cls.using(db).filter(F("done") == True).copy_to("ct_cached_archive")  # noqa: E712
        assert (before, archived()) == (0, 3)
    finally:
        db.close()


def _run_sync_seed(cls, db) -> None:
    for n in range(5):
        cls(n=n, done=n % 2 == 0, tag=f"t{n}").save(db=db)


def test_a_search_index_on_the_target_finds_the_copy():
    db = SQLerDB.in_memory(shared=False)
    try:
        cls = _job("pydantic")
        archive_cls = _job("pydantic", table="ct_search_archive")
        db.ensure_table("ct_search_archive")
        FTSIndex(archive_cls, ["tag"], db=db).create()
        _run_sync_seed(cls, db)
        cls.using(db).filter(F("done") == True).copy_to("ct_search_archive")  # noqa: E712
        found = FTSIndex(archive_cls, ["tag"], db=db).search("t2")
        assert [(m._id, m.tag) for m in found] == [(3, "t2")]
    finally:
        db.close()


@pytest.mark.parametrize("kind", PYDANTIC)
@pytest.mark.asyncio
async def test_a_unique_index_on_the_target_refuses_a_copied_row_as_it_refuses_a_save(kind):
    db = await _db(kind)
    try:
        cls = _job(kind)
        await _seed(cls, db)
        await _v(db.ensure_table("ct_archive"))
        await _v(db.create_index("ct_archive", "tag", unique=True))
        await _v(_job(kind, table="ct_archive")(n=9, tag="t2").save(db=db))  # _id 1, the tag of _id 3
        with pytest.raises(UniqueConstraintError, match="^UNIQUE constraint failed: index"):
            await _v(cls.using(db).filter(F("n") == 2).copy_to("ct_archive"))
        assert await _raw(db, "SELECT _id, json_extract(data, '$.tag') FROM ct_archive") == [(1, "t2")]
    finally:
        await _close(db)


# ----- a class that can refer to rows of the table it copies from is refused -------------
#
# A copied row keeps its references: a copy of a row that refers into the source table still
# refers there, and a delete of those rows would count the copy as a referrer (restrict
# refuses, cascade deletes the copy, set_null clears it). Until what a copy should do with
# such references is decided, copy_to() refuses such a class, before any statement.


def _sub(parent: type, fields: dict[str, tuple[Any, Any]]) -> type:
    """A subclass of ``parent`` with ``fields``, storing where ``parent`` stores."""
    name = f"Ct{next(_names)}"

    def body(ns):
        ns["__module__"] = __name__
        ns["__annotations__"] = {k: t for k, (t, _) in fields.items()}
        for k, (_, d) in fields.items():
            ns[k] = d

    cls = types.new_class(name, (parent,), {}, body)
    if issubclass(cls, SQLerLiteModelBase):
        cls = dataclass(cls)
    globals()[name] = cls
    return cls


def _self_referring(kind: str, shape: str) -> tuple[type, str]:
    """A class storing in ``ct_nodes`` whose field (named by the second item) can refer to a
    row of ``ct_nodes``."""
    if shape == "optional":
        name = f"Ct{next(_names) + 1}"  # the name _model gives the class it makes next
        return _model(kind, {**JOB, "parent": (f"Optional[{name}]", None)}, table="ct_nodes"), "parent"
    if shape == "list":
        name = f"Ct{next(_names) + 1}"
        return _model(kind, {**JOB, "kids": (f"Optional[list[{name}]]", None)}, table="ct_nodes"), "kids"
    if shape == "dict value":
        name = f"Ct{next(_names) + 1}"
        return _model(kind, {**JOB, "by_tag": (f"Optional[dict[str, {name}]]", None)}, table="ct_nodes"), "by_tag"
    if shape == "a parent class stored in the same table":
        parent = _model(kind, JOB, table="ct_nodes")
        return _sub(parent, {"up": (Optional[parent], None)}), "up"
    # another class that only shares the table name, by its __tablename__
    other = _model(kind, {"label": (str, "")}, table="ct_nodes")
    return _model(kind, {**JOB, "peer": (Optional[other], None)}, table="ct_nodes"), "peer"


@pytest.mark.parametrize(
    "shape", ["optional", "list", "dict value", "a parent class stored in the same table", "a class sharing the table name"]
)
@pytest.mark.asyncio
async def test_copy_to_refuses_a_class_that_can_refer_to_rows_of_its_source(kind, shape):
    db = await _db(kind)
    try:
        cls, field = _self_referring(kind, shape)
        await _seed(cls, db)
        async with _Trace(db) as sent:
            with pytest.raises(ValueError) as refused:
                await _v(cls.using(db).filter(F("done") == True).copy_to("ct_nodes_archive"))  # noqa: E712
        tables = await _raw(db, "SELECT name FROM sqlite_master WHERE name = 'ct_nodes_archive'")
        assert (str(refused.value), sent, tables, await _v(cls.using(db).count())) == (
            f"copy_to: {cls.__name__}.{field} can refer to rows of ct_nodes: its copies would still refer to "
            "the rows of ct_nodes, and a delete of those rows would count the copies as referrers",
            [],
            [],
            5,
        )
    finally:
        await _close(db)


@pytest.mark.parametrize("policy", ["restrict", "cascade", "set_null"])
@pytest.mark.asyncio
async def test_an_archived_row_refers_to_what_its_original_referred_to(kind, policy):
    """Jobs refer to owners (another table), so they copy. Job 1 (owner a) is moved to the
    archive; a delete of owner a then meets the archived job as a referrer, as it would meet
    any job: restrict refuses, cascade deletes the archived job, set_null clears its owner."""
    db = await _db(kind)
    try:
        owner_cls = _model(kind, {"name": (str, "")}, table="ct_owners")
        job_cls = _model(kind, {"name": (str, ""), "owner": (Optional[owner_cls], None)}, table="ct_jobs")
        a = await _v(owner_cls(name="a").save(db=db))
        b = await _v(owner_cls(name="b").save(db=db))
        await _v(job_cls(name="j1", owner=a).save(db=db))
        await _v(job_cls(name="j2", owner=b).save(db=db))
        moved = job_cls.using(db).filter(F("name") == "j1")
        assert (await _v(moved.copy_to("ct_jobs_archive")), await _v(moved.delete())) == (1, 1)
        of_a = owner_cls.using(db).filter(F("name") == "a")
        if policy == "restrict":
            with pytest.raises(ReferentialIntegrityError, match=r"^Cannot delete ct_owners:1; referenced by 1 row\(s\)$"):
                await _v(of_a.delete(on_delete=policy))
        else:
            assert await _v(of_a.delete(on_delete=policy)) == 1
        archive = await _raw(db, "SELECT _id, json_extract(data, '$.name'), json_extract(data, '$.owner._id') FROM ct_jobs_archive")
        owners = await _raw(db, "SELECT json_extract(data, '$.name') FROM ct_owners ORDER BY _id")
        jobs = await _raw(db, "SELECT _id, json_extract(data, '$.name') FROM ct_jobs")
        assert (archive, owners, jobs) == {
            "restrict": ([(1, "j1", 1)], [("a",), ("b",)], [(2, "j2")]),
            "cascade": ([], [("b",)], [(2, "j2")]),
            "set_null": ([(1, "j1", None)], [("b",)], [(2, "j2")]),
        }[policy]
    finally:
        await _close(db)
