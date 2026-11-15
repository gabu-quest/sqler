"""Tests for async referential integrity support, and for what a delete does about the rows
that refer to its row on every base, sync and async."""

import inspect
import itertools
import json
import sqlite3
import types
from dataclasses import dataclass
from typing import Optional

import pytest
import sqler.models as models
from pydantic import BaseModel
from sqler import SQLerDB
from sqler.db.async_db import AsyncSQLerDB
from sqler.models import (
    AsyncSQLerModel,
    BrokenRef,
    ReferentialIntegrityError,
    SQLerLiteModelBase,
    SQLerMsgspecModelBase,
    async_cascade_delete,
    async_find_referrers,
    async_set_null_referrers,
    async_validate_references,
)
from sqler.models.mixins import (
    AsyncAuditLogMixin,
    AsyncHooksMixin,
    AuditLogMixin,
    HooksMixin,
)


class IntegrityAddress(AsyncSQLerModel):
    city: str


class IntegrityUser(AsyncSQLerModel):
    name: str
    address: IntegrityAddress | None = None
    friends: list[IntegrityAddress | None] | None = None


@pytest.mark.asyncio
async def test_delete_with_policy_restrict_refuses_a_row_that_is_referred_to(
    async_db: AsyncSQLerDB,
):
    IntegrityAddress.set_db(async_db, "integrity_address")
    IntegrityUser.set_db(async_db, "integrity_user")

    a = IntegrityAddress(city="Kyoto")
    await a.save()
    u = IntegrityUser(name="Alice", address=a)
    await u.save()

    with pytest.raises(
        ReferentialIntegrityError,
        match=r"^Cannot delete integrity_address:1; referenced by 1 row\(s\)$",
    ):
        await a.delete_with_policy(on_delete="restrict")

    assert a._id == 1
    assert (await IntegrityAddress.from_id(1)).city == "Kyoto"
    assert (await IntegrityUser.from_id(u._id)).address.city == "Kyoto"


@pytest.mark.asyncio
async def test_delete_with_policy_set_null(async_db: AsyncSQLerDB):
    """Test set_null mode nullifies references before deleting."""
    IntegrityAddress.set_db(async_db, "integrity_address2")
    IntegrityUser.set_db(async_db, "integrity_user2")

    a1 = IntegrityAddress(city="Kyoto")
    await a1.save()
    a2 = IntegrityAddress(city="Osaka")
    await a2.save()
    u = IntegrityUser(name="Bob", address=a1, friends=[a1, a2])
    await u.save()

    await a1.delete_with_policy(on_delete="set_null")

    # Address should be deleted
    assert await IntegrityAddress.from_id(a1._id) is None

    # User should still exist with null references
    u2 = await IntegrityUser.from_id(u._id)
    assert u2 is not None
    assert u2.address is None
    assert u2.friends[0] is None
    # a2 reference should be intact
    assert u2.friends[1].city == "Osaka"


@pytest.mark.asyncio
async def test_delete_with_policy_cascade(async_db: AsyncSQLerDB):
    """Test cascade mode deletes referrers recursively."""
    IntegrityAddress.set_db(async_db, "integrity_address3")
    IntegrityUser.set_db(async_db, "integrity_user3")

    a = IntegrityAddress(city="Tokyo")
    await a.save()
    u = IntegrityUser(name="Alice", address=a)
    await u.save()

    # Cascade should delete the user first (referrer), then the address
    await a.delete_with_policy(on_delete="cascade")

    assert await IntegrityAddress.from_id(a._id) is None
    assert await IntegrityUser.from_id(u._id) is None


@pytest.mark.asyncio
async def test_async_find_referrers(async_db: AsyncSQLerDB):
    """Test async_find_referrers finds all references to a row."""
    IntegrityAddress.set_db(async_db, "integrity_address4")
    IntegrityUser.set_db(async_db, "integrity_user4")

    a = IntegrityAddress(city="Kyoto")
    await a.save()
    u1 = IntegrityUser(name="Alice", address=a)
    await u1.save()
    u2 = IntegrityUser(name="Bob", address=a)
    await u2.save()

    referrers = await async_find_referrers(async_db, "integrity_address4", a._id)
    assert len(referrers) == 2

    ref_ids = {r[1] for r in referrers}
    assert u1._id in ref_ids
    assert u2._id in ref_ids


@pytest.mark.asyncio
async def test_async_find_referrers_in_list(async_db: AsyncSQLerDB):
    """Test async_find_referrers finds references in lists."""
    IntegrityAddress.set_db(async_db, "integrity_address5")
    IntegrityUser.set_db(async_db, "integrity_user5")

    a1 = IntegrityAddress(city="Kyoto")
    await a1.save()
    a2 = IntegrityAddress(city="Osaka")
    await a2.save()
    u = IntegrityUser(name="Alice", friends=[a1, a2])
    await u.save()

    referrers = await async_find_referrers(async_db, "integrity_address5", a1._id)
    assert len(referrers) == 1
    assert referrers[0][1] == u._id
    assert "friends[0]" in referrers[0][2]["paths"][0]


@pytest.mark.asyncio
async def test_async_set_null_referrers(async_db: AsyncSQLerDB):
    """Test async_set_null_referrers nullifies references."""
    IntegrityAddress.set_db(async_db, "integrity_address6")
    IntegrityUser.set_db(async_db, "integrity_user6")

    a = IntegrityAddress(city="Kyoto")
    await a.save()
    u = IntegrityUser(name="Alice", address=a)
    await u.save()

    referrers = await async_find_referrers(async_db, "integrity_address6", a._id)
    await async_set_null_referrers(async_db, "integrity_address6", a._id, referrers)

    # Reload user and check reference is null
    u2 = await IntegrityUser.from_id(u._id)
    assert u2.address is None


@pytest.mark.asyncio
async def test_async_cascade_delete(async_db: AsyncSQLerDB):
    """Test async_cascade_delete recursively deletes referrers."""
    IntegrityAddress.set_db(async_db, "integrity_address7")
    IntegrityUser.set_db(async_db, "integrity_user7")

    a = IntegrityAddress(city="Kyoto")
    await a.save()
    u = IntegrityUser(name="Alice", address=a)
    await u.save()

    referrers = await async_find_referrers(async_db, "integrity_address7", a._id)
    await async_cascade_delete(async_db, referrers, set())

    # User should be deleted
    assert await IntegrityUser.from_id(u._id) is None
    # Address still exists (cascade_delete only deletes referrers)
    assert await IntegrityAddress.from_id(a._id) is not None


@pytest.mark.asyncio
async def test_async_validate_references_clean(async_db: AsyncSQLerDB):
    """Test async_validate_references on valid data returns empty."""
    IntegrityAddress.set_db(async_db, "integrity_address8")
    IntegrityUser.set_db(async_db, "integrity_user8")

    a = IntegrityAddress(city="Kyoto")
    await a.save()
    u = IntegrityUser(name="Alice", address=a)
    await u.save()

    broken = await async_validate_references(async_db)
    # Filter to our test tables only
    our_broken = [b for b in broken if b.table.startswith("integrity_")]
    assert len(our_broken) == 0


@pytest.mark.asyncio
async def test_async_validate_references_broken(async_db: AsyncSQLerDB):
    """Test async_validate_references detects dangling references."""
    IntegrityAddress.set_db(async_db, "integrity_address9")
    IntegrityUser.set_db(async_db, "integrity_user9")

    a = IntegrityAddress(city="Kyoto")
    await a.save()
    u = IntegrityUser(name="Alice", address=a)
    await u.save()

    # Delete address without updating user (creates dangling ref)
    await async_db.adapter.execute("DELETE FROM integrity_address9 WHERE _id = ?;", [a._id])
    await async_db.adapter.auto_commit()

    broken = await async_validate_references(async_db)
    # Filter to our test tables only
    our_broken = [b for b in broken if b.table.startswith("integrity_")]
    assert len(our_broken) == 1
    assert isinstance(our_broken[0], BrokenRef)
    assert our_broken[0].table == "integrity_user9"
    assert our_broken[0].target_table == "integrity_address9"
    assert our_broken[0].target_id == a._id


@pytest.mark.asyncio
async def test_delete_with_policy_invalid_mode(async_db: AsyncSQLerDB):
    """Test delete_with_policy raises ValueError for invalid mode."""
    IntegrityAddress.set_db(async_db, "integrity_address10")

    a = IntegrityAddress(city="Kyoto")
    await a.save()

    with pytest.raises(ValueError, match="on_delete must be"):
        await a.delete_with_policy(on_delete="invalid")


@pytest.mark.asyncio
async def test_delete_with_policy_unsaved_model(async_db: AsyncSQLerDB):
    """Test delete_with_policy raises ValueError for unsaved model."""
    IntegrityAddress.set_db(async_db, "integrity_address11")

    a = IntegrityAddress(city="Kyoto")
    # Not saved, so _id is None

    with pytest.raises(ValueError, match="Cannot delete unsaved model"):
        await a.delete_with_policy(on_delete="restrict")


# ----- what a delete does about the rows that refer to its row, on every base -------------

# Every public base class, as the package exports it: a base added later is in this list,
# and fails until ``declare`` knows how to write a class on it.
BASES = [
    base
    for base in (getattr(models, name) for name in models.__all__)
    if isinstance(base, type) and hasattr(base, "from_id") and hasattr(base, "delete_with_policy")
]
SYNC_BASES = [b for b in BASES if not inspect.iscoroutinefunction(b.delete)]
ASYNC_BASES = [b for b in BASES if inspect.iscoroutinefunction(b.delete)]

_tables = itertools.count()


def declare(base, name: str, fields: dict, mixins: tuple = ()):
    """A class on ``base`` with ``fields`` (each ``name: (type, default)``) and its own table."""
    table = f"dib_{name.lower()}_{next(_tables)}"
    ns = {
        "__tablename__": table,
        "__annotations__": {k: t for k, (t, _) in fields.items()},
        **{k: d for k, (_, d) in fields.items()},
    }
    bases = (*mixins, base)
    if issubclass(base, SQLerLiteModelBase):
        return dataclass(types.new_class(name, bases, {}, lambda body: body.update(ns)))
    if issubclass(base, SQLerMsgspecModelBase):
        return types.new_class(name, bases, {"kw_only": True}, lambda body: body.update(ns))
    if issubclass(base, BaseModel):
        return types.new_class(name, bases, {}, lambda body: body.update(ns))
    raise AssertionError(f"no way to declare a class on {base.__name__}: add its family")


async def settle(value):
    return await value if inspect.isawaitable(value) else value


async def open_db(base, path=None):
    if base in ASYNC_BASES:
        db = AsyncSQLerDB.on_disk(str(path)) if path else AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        return db
    return SQLerDB.on_disk(str(path)) if path else SQLerDB.in_memory(shared=False)


async def rows(db, sql: str) -> list:
    cur = await settle(db.adapter.execute(sql))
    found = [tuple(r) for r in await settle(cur.fetchall())]
    if inspect.iscoroutinefunction(getattr(cur, "close", None)):
        await cur.close()
    return found


async def holds(db, table: str, path: str = "$.target._id") -> list:
    """Each row of ``table`` as ``(_id, what path holds)``."""
    return await rows(db, f"SELECT _id, json_extract(data, '{path}') FROM {table} ORDER BY _id")


async def in_transaction(db, act):
    if isinstance(db, AsyncSQLerDB):
        async with db.transaction():
            await act()
    else:
        with db.transaction():
            await act()


def pair(base, mixins: tuple = ()):
    Target = declare(base, "Target", {"name": (str, "")}, mixins)
    Referrer = declare(
        base, "Referrer", {"title": (str, ""), "target": (Optional[Target], None)}, mixins
    )
    return Target, Referrer


WAYS = {
    "delete()": lambda t, db: t.delete(db=db),
    "delete_with_policy()": lambda t, db: t.delete_with_policy(db=db),
    "delete_with_policy('restrict')": lambda t, db: t.delete_with_policy(
        db=db, on_delete="restrict"
    ),
}


def test_the_table_holds_every_public_base():
    assert sorted(b.__name__ for b in BASES) == [
        "AsyncSQLerLiteModel",
        "AsyncSQLerLiteSafeModel",
        "AsyncSQLerModel",
        "AsyncSQLerSafeModel",
        "SQLerLiteModel",
        "SQLerLiteSafeModel",
        "SQLerModel",
        "SQLerMsgspecModel",
        "SQLerSafeModel",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["alone", "raised out of a transaction", "caught in a transaction"])
@pytest.mark.parametrize("way", list(WAYS))
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_delete_of_a_row_that_is_referred_to_is_refused(base, way, where):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r", target=target).save(db=db))
        message = rf"^Cannot delete {Target.__tablename__}:1; referenced by 1 row\(s\)$"

        async def act():
            await settle(WAYS[way](target, db))

        if where == "alone":
            with pytest.raises(ReferentialIntegrityError, match=message):
                await act()
        elif where == "raised out of a transaction":
            with pytest.raises(ReferentialIntegrityError, match=message):
                await in_transaction(db, act)
        else:

            async def caught():
                with pytest.raises(ReferentialIntegrityError, match=message):
                    await act()

            await in_transaction(db, caught)

        assert target._id == 1
        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
        assert await holds(db, Referrer.__tablename__) == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,targets,referrers",
    [("set_null", [], [(1, None)]), ("cascade", [], [])],
)
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_set_null_and_cascade_do_what_they_say_on_every_base(
    base, policy, targets, referrers
):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r", target=target).save(db=db))

        await settle(target.delete_with_policy(db=db, on_delete=policy))

        assert target._id is None
        assert await holds(db, Target.__tablename__, "$.name") == targets
        assert await holds(db, Referrer.__tablename__) == referrers
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_delete_nothing_refers_to_goes_through(base):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r").save(db=db))

        await settle(target.delete(db=db))

        assert target._id is None
        assert await holds(db, Target.__tablename__, "$.name") == []
        assert await holds(db, Referrer.__tablename__) == [(1, None)]
    finally:
        await settle(db.close())


# ----- hostile inputs ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_referrer_in_a_list_field_and_one_in_a_dict_field_both_refuse(base):
    db = await open_db(base)
    try:
        Target = declare(base, "Target", {"name": (str, "")})
        Listing = declare(base, "Listing", {"items": (Optional[list[Target]], None)})
        Keyed = declare(base, "Keyed", {"by_key": (Optional[dict[str, Target]], None)})
        target = await settle(Target(name="t").save(db=db))
        await settle(Listing(items=[target]).save(db=db))
        await settle(Keyed(by_key={"k": target}).save(db=db))

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 2 row\(s\)$"):
            await settle(target.delete(db=db))

        assert target._id == 1
        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
        assert await holds(db, Listing.__tablename__, "$.items[0]._id") == [(1, 1)]
        assert await holds(db, Keyed.__tablename__, "$.by_key.k._id") == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_two_referring_classes_one_with_a_row_and_one_without(base):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        Empty = declare(base, "Empty", {"target": (Optional[Target], None)})
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r", target=target).save(db=db))
        await settle(Empty().save(db=db))

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(target.delete(db=db))

        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
        assert await holds(db, Referrer.__tablename__) == [(1, 1)]
        assert await holds(db, Empty.__tablename__) == [(1, None)]
    finally:
        await settle(db.close())


async def _refer_to_itself(db, table: str) -> None:
    ref = json.dumps({"_table": table, "_id": 1})
    cur = await settle(
        db.adapter.execute(
            f"UPDATE {table} SET data = json_set(data, '$.target', json(?)) WHERE _id = 1", [ref]
        )
    )
    if inspect.iscoroutinefunction(getattr(cur, "close", None)):
        await cur.close()
    await settle(db.adapter.auto_commit())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_that_refers_to_itself_refuses_restrict(base):
    db = await open_db(base)
    try:
        Node = declare(base, "Node", {"name": (str, ""), "target": (Optional[dict], None)})
        node = await settle(Node(name="n").save(db=db))
        await _refer_to_itself(db, Node.__tablename__)

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(node.delete(db=db))

        assert await holds(db, Node.__tablename__) == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,actions",
    [
        ("cascade", [("create",), ("delete",)]),
        # set_null nulls the row's reference to itself (an update), then deletes it
        ("set_null", [("create",), ("update",), ("delete",)]),
    ],
)
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_that_refers_to_itself_is_deleted_once_by_cascade_and_set_null(
    base, policy, actions
):
    db = await open_db(base)
    try:
        audit = AsyncAuditLogMixin if base in ASYNC_BASES else AuditLogMixin
        Node = declare(
            base, "Node", {"name": (str, ""), "target": (Optional[dict], None)}, (audit,)
        )
        node = await settle(Node(name="n").save(db=db))
        await _refer_to_itself(db, Node.__tablename__)

        await settle(node.delete_with_policy(db=db, on_delete=policy))

        assert node._id is None
        assert await holds(db, Node.__tablename__) == []
        audit = await rows(db, f"SELECT action FROM {Node.__tablename__}_audit ORDER BY _id")
        assert audit == actions
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_hooks_and_audit_rows_of_a_refused_delete(base):
    """A refused delete ran ``before_delete()`` only, and wrote no audit row on either side."""
    calls = []
    if base in ASYNC_BASES:

        class Hooks(AsyncHooksMixin):
            async def before_delete(self) -> bool:
                calls.append(("before_delete", type(self).__name__))
                return True

            async def after_delete(self) -> None:
                calls.append(("after_delete", type(self).__name__))

        mixins = (Hooks, AsyncAuditLogMixin)
    else:

        class Hooks(HooksMixin):
            def before_delete(self) -> bool:
                calls.append(("before_delete", type(self).__name__))
                return True

            def after_delete(self) -> None:
                calls.append(("after_delete", type(self).__name__))

        mixins = (Hooks, AuditLogMixin)
    db = await open_db(base)
    try:
        Target, Referrer = pair(base, mixins)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r", target=target).save(db=db))

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(target.delete(db=db))

        assert calls == [("before_delete", "Target")]
        for cls in (Target, Referrer):
            audit = await rows(db, f"SELECT action FROM {cls.__tablename__}_audit ORDER BY _id")
            assert audit == [("create",)]
        assert await holds(db, Referrer.__tablename__) == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["set_null", "cascade"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_delete_the_caller_rolls_back_leaves_both_tables_as_they_were(base, policy):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r", target=target).save(db=db))

        async def act():
            await settle(target.delete_with_policy(db=db, on_delete=policy))
            raise RuntimeError("the caller changes its mind")

        with pytest.raises(RuntimeError, match="changes its mind"):
            await in_transaction(db, act)

        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
        assert await holds(db, Referrer.__tablename__) == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_delete_of_an_instance_never_saved_raises_value_error(base):
    db = await open_db(base)
    try:
        Target, _ = pair(base)
        with pytest.raises(ValueError, match="^Cannot delete unsaved model"):
            await settle(Target(name="t").delete(db=db))
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_delete_of_a_row_another_connection_deleted_goes_through(base, tmp_path):
    path = tmp_path / "gone.db"
    db = await open_db(base, path)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r").save(db=db))
        other = sqlite3.connect(path)
        other.execute(f"DELETE FROM {Target.__tablename__} WHERE _id = 1")
        other.commit()
        other.close()

        await settle(target.delete(db=db))

        assert target._id is None
        assert await holds(db, Target.__tablename__, "$.name") == []
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_plain_dict_shaped_as_a_reference_in_an_untyped_field_refuses(base):
    db = await open_db(base)
    try:
        Target = declare(base, "Target", {"name": (str, "")})
        Loose = declare(base, "Loose", {"blob": (Optional[dict], None)})
        target = await settle(Target(name="t").save(db=db))
        await settle(Loose(blob={"_table": Target.__tablename__, "_id": 1}).save(db=db))

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(target.delete(db=db))

        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
        assert await holds(db, Loose.__tablename__, "$.blob._id") == [(1, 1)]
    finally:
        await settle(db.close())


async def _trace(db, callback) -> None:
    if isinstance(db, AsyncSQLerDB):
        for conn in db.adapter._all_conns:
            await conn.set_trace_callback(callback)
    else:
        db.adapter._conn().set_trace_callback(callback)


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", [False, True], ids=["alone", "in a transaction"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_referrer_written_by_another_connection_during_the_delete_is_kept_out(
    base, inside, tmp_path
):
    """The check and the delete are one unit: a referrer another connection tries to write
    as the row's DELETE starts is refused by the lock the unit holds, so no referrer is left
    pointing at no row."""
    path = tmp_path / "race.db"
    db = await open_db(base, path)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="no target").save(db=db))
        late = json.dumps({"title": "late", "target": {"_table": Target.__tablename__, "_id": 1}})
        other = sqlite3.connect(path, timeout=0, check_same_thread=False)
        refused = []

        def write_a_referrer(sql: str) -> None:
            if not sql.startswith(f"DELETE FROM {Target.__tablename__} ") or refused:
                return
            try:
                other.execute(f"INSERT INTO {Referrer.__tablename__} (data) VALUES (json(?))", [late])
                other.commit()
                refused.append("written")
            except sqlite3.OperationalError as exc:
                other.rollback()
                refused.append(str(exc))

        await _trace(db, write_a_referrer)
        try:
            if inside:

                async def act():
                    await settle(target.delete(db=db))

                await in_transaction(db, act)
            else:
                await settle(target.delete(db=db))
        finally:
            await _trace(db, None)
            other.close()

        assert refused == ["database is locked"]
        assert await holds(db, Target.__tablename__, "$.name") == []
        assert await holds(db, Referrer.__tablename__) == [(1, None)]
    finally:
        await settle(db.close())
