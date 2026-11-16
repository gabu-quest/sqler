"""Tests for async referential integrity support, and for what a delete does about the rows
that refer to its row on every base, sync and async."""

import inspect
import itertools
import json
import sqlite3
import sys
import types
from dataclasses import dataclass
from typing import Optional

import pytest
import sqler.models as models
from pydantic import BaseModel
from sqler import SQLerDB, StaleVersionError, registry
from sqler.db.async_db import AsyncSQLerDB
from sqler.logging import query_logger
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
from sqler.query import SQLerField as F


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


def declare(base, name: str, fields: dict, mixins: tuple = (), module: str = "", table=None):
    """A class on ``base`` with ``fields`` (each ``name: (type, default)``) and its own table,
    or ``table`` when one is given, or the table it inherits when ``table`` is False
    (defined in ``module``, where a string annotation is looked up, when one is given)."""
    if table is None:
        table = f"dib_{name.lower()}_{next(_tables)}"
    ns = {
        **({"__module__": module} if module else {}),
        **({"__tablename__": table} if table else {}),
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


def ref(table: str, row_id) -> dict:
    return {"_table": table, "_id": row_id}


async def put(db, table: str, row_id: int, path: str, value) -> None:
    """Write ``value`` at ``path`` of row ``row_id`` of ``table``, below every model."""
    cur = await settle(
        db.adapter.execute(
            f"UPDATE {table} SET data = json_set(data, '{path}', json(?)) WHERE _id = ?",
            [json.dumps(value), row_id],
        )
    )
    if inspect.iscoroutinefunction(getattr(cur, "close", None)):
        await cur.close()
    await settle(db.adapter.auto_commit())


def node_of(base, mixins: tuple = ()):
    """``Target`` and ``Node``, a subclass of it on its own table whose ``target`` field holds
    a ``Target``: a row of ``Node`` can refer to itself or to another ``Node``."""
    Target = declare(base, "Target", {"name": (str, "")}, mixins)
    return Target, declare(Target, "Node", {"target": (Optional[Target], None)})


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["delete()", "delete_all()"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_that_refers_to_itself_is_no_referrer_of_itself(base, way):
    db = await open_db(base)
    try:
        _, Node = node_of(base)
        node = await settle(Node(name="n").save(db=db))
        await put(db, Node.__tablename__, 1, "$.target", ref(Node.__tablename__, 1))

        if way == "delete()":
            await settle(node.delete(db=db))
        else:
            assert await settle(Node.using(db).delete_all()) == 1

        assert await holds(db, Node.__tablename__) == []
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_two_rows_that_refer_to_each_other_go_together_and_not_alone(base):
    db = await open_db(base)
    try:
        _, Node = node_of(base)
        table = Node.__tablename__
        one = await settle(Node(name="one").save(db=db))
        await settle(Node(name="two").save(db=db))
        await put(db, table, 1, "$.target", ref(table, 2))
        await put(db, table, 2, "$.target", ref(table, 1))

        with pytest.raises(ReferentialIntegrityError, match=rf"^Cannot delete {table}:1; referenced by 1 row\(s\)$"):
            await settle(one.delete(db=db))
        with pytest.raises(ReferentialIntegrityError, match=rf"^Cannot delete {table}:2; referenced by 1 row\(s\)$"):
            await settle(Node.using(db).filter(F("name") == "two").delete_all())
        assert await holds(db, table) == [(1, 2), (2, 1)]

        assert await settle(Node.using(db).delete_all()) == 2
        assert await holds(db, table) == []
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["set_null", "cascade"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_row_that_refers_to_itself_is_deleted_once_and_nulls_nothing(base, policy):
    db = await open_db(base)
    try:
        audit = AsyncAuditLogMixin if base in ASYNC_BASES else AuditLogMixin
        _, Node = node_of(base, (audit,))
        node = await settle(Node(name="n").save(db=db))
        await put(db, Node.__tablename__, 1, "$.target", ref(Node.__tablename__, 1))

        await settle(node.delete_with_policy(db=db, on_delete=policy))

        assert node._id is None
        assert await holds(db, Node.__tablename__) == []
        audit = await rows(db, f"SELECT action FROM {Node.__tablename__}_audit ORDER BY _id")
        assert audit == [("create",), ("delete",)]
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
@pytest.mark.parametrize("policy", ["restrict", "set_null", "cascade"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_dict_shaped_as_a_reference_in_an_untyped_field_is_data(base, policy):
    db = await open_db(base)
    try:
        Target = declare(base, "Target", {"name": (str, "")})
        Loose = declare(base, "Loose", {"blob": (Optional[dict], None)})
        target = await settle(Target(name="t").save(db=db))
        await settle(Loose(blob=ref(Target.__tablename__, 1)).save(db=db))

        await settle(target.delete_with_policy(db=db, on_delete=policy))

        assert await holds(db, Target.__tablename__, "$.name") == []
        assert await holds(db, Loose.__tablename__, "$.blob._id") == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [SQLerDB, AsyncSQLerDB], ids=["sync", "async"])
async def test_the_orphan_scan_reports_a_reference_and_never_a_dict_with_more_keys(base):
    """Both rows name a row that is not there; only the one holding a reference is reported."""
    db = await open_db(AsyncSQLerModel if base is AsyncSQLerDB else models.SQLerModel)
    try:
        Target, Referrer = pair(AsyncSQLerModel if base is AsyncSQLerDB else models.SQLerModel)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="ref", target=target).save(db=db))
        data = {**ref(Target.__tablename__, 1), "note": "data"}
        await settle(db.insert_document(Referrer.__tablename__, {"title": "data", "blob": [{"in": data}]}))
        await settle(db.delete_document(Target.__tablename__, 1))

        if base is AsyncSQLerDB:
            broken = await async_validate_references(db)
        else:
            from sqler.models.integrity import validate_references

            broken = validate_references(db)
        assert broken == [
            BrokenRef(
                table=Referrer.__tablename__, row_id=1, path="target", target_table=Target.__tablename__, target_id=1
            )
        ]
    finally:
        await settle(db.close())


async def _trace(db, callback) -> None:
    if isinstance(db, AsyncSQLerDB):
        for conn in db.adapter._all_conns:
            await conn.set_trace_callback(callback)
    else:
        db.adapter._conn().set_trace_callback(callback)


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["delete()", "delete_all()"])
@pytest.mark.parametrize("inside", [False, True], ids=["alone", "in a transaction"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_referrer_written_by_another_connection_during_the_delete_is_kept_out(
    base, inside, way, tmp_path
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
            async def act():
                if way == "delete()":
                    await settle(target.delete(db=db))
                else:
                    assert await settle(Target.using(db).delete_all()) == 1

            if inside:
                await in_transaction(db, act)
            else:
                await act()
        finally:
            await _trace(db, None)
            other.close()

        assert refused == ["database is locked"]
        assert await holds(db, Target.__tablename__, "$.name") == []
        assert await holds(db, Referrer.__tablename__) == [(1, None)]
    finally:
        await settle(db.close())


# ----- a referrer is a row that holds the reference where its class declares a model ---------


async def sent(act) -> list[str]:
    with query_logger.capture() as got:
        await act()
    return [e.sql for e in got]


def forward_module() -> str:
    """A module of its own, where a class's string annotation is looked up."""
    name = f"dib_forward_{next(_tables)}"
    sys.modules[name] = types.ModuleType(name)
    return name


def resolve(module: str, *classes) -> None:
    """Put ``classes`` into ``module``, where the string annotations of the first are looked
    up: they resolve, and so does each pydantic class that names one of them."""
    for cls in classes:
        setattr(sys.modules[module], cls.__name__, cls)
    for cls in classes:
        if issubclass(cls, BaseModel):
            cls.model_rebuild()


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_referrer_at_an_optional_a_list_a_dict_and_a_nested_position(base):
    db = await open_db(base)
    try:
        Target = declare(base, "Target", {"name": (str, "")})
        Holder = declare(
            base,
            "Holder",
            {
                "one": (Optional[Target], None),
                "items": (Optional[list[Target]], None),
                "by_key": (Optional[dict[str, Target]], None),
                "deep": (Optional[dict[str, list[Optional[Target]]]], None),
            },
        )
        target = await settle(Target(name="t").save(db=db))
        for _ in range(4):
            await settle(Holder().save(db=db))
        t = ref(Target.__tablename__, 1)
        table = Holder.__tablename__
        await put(db, table, 1, "$.one", t)
        await put(db, table, 2, "$.items", [t])
        await put(db, table, 3, "$.by_key", {"k": t})
        await put(db, table, 4, "$.deep", {"k": [None, t]})

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 4 row\(s\)$"):
            await settle(target.delete(db=db))
        await settle(target.delete_with_policy(db=db, on_delete="set_null"))

        stored = [json.loads(r[0]) for r in await rows(db, f"SELECT data FROM {table} ORDER BY _id")]
        assert [{k: v for k, v in d.items() if k != "_version"} for d in stored] == [
            d
            for d in (
                {"one": None, "items": None, "by_key": None, "deep": None},
                {"one": None, "items": [None], "by_key": None, "deep": None},
                {"one": None, "items": None, "by_key": {"k": None}, "deep": None},
                {"one": None, "items": None, "by_key": None, "deep": {"k": [None, None]}},
            )
        ]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_field_of_a_base_class_holding_a_row_of_a_subclass_refers_to_it(base):
    db = await open_db(base)
    try:
        Target, Sub = node_of(base)
        Holder = declare(base, "Holder", {"target": (Optional[Target], None)})
        sub = await settle(Sub(name="s").save(db=db))
        await settle(Holder().save(db=db))
        await put(db, Holder.__tablename__, 1, "$.target", ref(Sub.__tablename__, 1))

        with pytest.raises(
            ReferentialIntegrityError,
            match=rf"^Cannot delete {Sub.__tablename__}:1; referenced by 1 row\(s\)$",
        ):
            await settle(sub.delete(db=db))
        assert await holds(db, Sub.__tablename__, "$.name") == [(1, "s")]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_class_whose_annotation_resolves_after_the_first_delete(base):
    """While a name in an annotation of a class cannot be found, any position of its rows
    may hold a reference: its table is read whole. Once the name resolves, only the
    positions it declares as a model count."""
    db = await open_db(base)
    try:
        module = forward_module()
        Target = declare(base, "Target", {"name": (str, "")})
        later = f"Later{next(_tables)}"
        Holder = declare(
            base,
            "Holder",
            {
                "target": (Optional[Target], None),
                "blob": (Optional[dict], None),
                "later": (Optional[later], None),
            },
            module=module,
        )
        first = await settle(Target(name="first").save(db=db))
        second = await settle(Target(name="second").save(db=db))
        for doc in ({"blob": ref(Target.__tablename__, 1)}, {"target": ref(Target.__tablename__, 2)}):
            await settle(db.insert_document(Holder.__tablename__, doc))

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(first.delete(db=db))

        resolve(module, Holder, declare(base, later, {"n": (int, 0)}))
        await settle(first.delete(db=db))
        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(second.delete(db=db))

        assert await holds(db, Target.__tablename__, "$.name") == [(2, "second")]
        assert await holds(db, Holder.__tablename__, "$.blob._id") == [(1, 1), (2, None)]
    finally:
        await settle(db.close())


def define_after(module: str, name: str, base, fields: dict):
    """A class called ``name`` defined in ``module`` and bound there, as a ``class``
    statement of that module does it: nothing is rebuilt and no class is used."""
    cls = declare(base, name, fields, module=module)
    setattr(sys.modules[module], name, cls)
    return cls


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["delete()", "delete_all()"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_class_never_used_that_names_a_class_defined_after_it_costs_a_delete_no_read(base, way):
    """A module that defines ``Early`` before the ``Later`` it names, and uses neither: the
    delete of a row of a class nothing can refer to sends its own statement and no other."""
    db = await open_db(base)
    try:
        module = forward_module()
        later = f"Later{next(_tables)}"
        Early = declare(base, "Early", {"later": (Optional[later], None)}, module=module)
        define_after(module, later, base, {"n": (int, 0)})
        Alone = declare(base, "Alone", {"name": (str, "")})
        alone = await settle(Alone(name="a").save(db=db))
        await settle(db.insert_document(Early.__tablename__, {"later": None}))

        if way == "delete()":
            statements = await sent(lambda: settle(alone.delete(db=db)))
        else:
            statements = await sent(lambda: settle(Alone.using(db).delete_all()))

        assert [s for s in statements if Alone.__tablename__ not in s] == []
        assert [s.split()[0] for s in statements if s.split()[0] != "CREATE"] == ["DELETE"]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_class_never_used_that_names_a_class_defined_after_it_refers_where_it_declares(base):
    """The positions such a class declares are known with no use of it: a reference in an
    untyped field of its row is data, one in its typed field refuses the delete."""
    db = await open_db(base)
    try:
        module = forward_module()
        Target = declare(base, "Target", {"name": (str, "")})
        later = f"Later{next(_tables)}"
        Holder = declare(
            base,
            "Holder",
            {
                "target": (Optional[Target], None),
                "blob": (Optional[dict], None),
                "later": (Optional[later], None),
            },
            module=module,
        )
        Later = define_after(module, later, base, {"n": (int, 0)})
        first = await settle(Target(name="first").save(db=db))
        second = await settle(Target(name="second").save(db=db))
        there = await settle(Later(n=1).save(db=db))
        for doc in (
            {"blob": ref(Target.__tablename__, 1)},
            {"target": ref(Target.__tablename__, 2)},
            {"later": ref(Later.__tablename__, 1)},
        ):
            await settle(db.insert_document(Holder.__tablename__, doc))

        await settle(first.delete(db=db))
        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(second.delete(db=db))
        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(there.delete(db=db))

        assert await holds(db, Target.__tablename__, "$.name") == [(2, "second")]
        assert await holds(db, Later.__tablename__, "$.n") == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_class_registered_after_the_first_delete_is_looked_at(base):
    db = await open_db(base)
    try:
        Target = declare(base, "Target", {"name": (str, "")})
        first = await settle(Target(name="first").save(db=db))
        second = await settle(Target(name="second").save(db=db))
        await settle(first.delete(db=db))

        Late = declare(base, "Late", {"target": (Optional[Target], None)})
        await settle(Late(target=second).save(db=db))

        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(second.delete(db=db))
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_table_registered_again_by_a_class_with_no_typed_field_keeps_the_first_classes_fields(
    base,
):
    """The rows the first class wrote are still in the table: its fields still count."""
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(target=target).save(db=db))
        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(target.delete(db=db))

        Plain = declare(base, "Plain", {"target": (Optional[dict], None)})
        registry.register(Referrer.__tablename__, Plain)
        with pytest.raises(ReferentialIntegrityError, match=r"referenced by 1 row\(s\)$"):
            await settle(target.delete(db=db))

        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
        assert await holds(db, Referrer.__tablename__) == [(1, 1)]
    finally:
        await settle(db.close())


# ----- every class whose rows live in a table decides where they can hold a reference -----


def owner_of(Target) -> dict:
    return {"owner": (Optional[Target], None)}


async def _subclass_adds_the_field(base, db):
    Target = declare(base, "Target", {"name": (str, "")})
    Base = declare(base, "Base", {"name": (str, "")})
    Sub = declare(Base, "Sub", owner_of(Target), table=False)
    t = await settle(Target(name="t").save(db=db))
    await settle(Sub(owner=t).save(db=db))
    return t, Base.__tablename__, (Base, Sub)


async def _subclass_of_a_subclass_adds_the_field(base, db):
    Target = declare(base, "Target", {"name": (str, "")})
    Base = declare(base, "Base", {"name": (str, "")})
    Mid = declare(Base, "Mid", {"note": (str, "")}, table=False)
    Leaf = declare(Mid, "Leaf", owner_of(Target), table=False)
    t = await settle(Target(name="t").save(db=db))
    await settle(Leaf(owner=t).save(db=db))
    return t, Base.__tablename__, (Base, Mid, Leaf)


def _second_class_by_tablename(field_first: bool):
    async def scene(base, db):
        Target = declare(base, "Target", {"name": (str, "")})
        table = f"dib_shared_{next(_tables)}"
        made = {}
        for name in ("Holder", "Plain") if field_first else ("Plain", "Holder"):
            fields = owner_of(Target) if name == "Holder" else {"note": (str, "")}
            made[name] = declare(base, name, fields, table=table)
        t = await settle(Target(name="t").save(db=db))
        await settle(made["Holder"](owner=t).save(db=db))
        return t, table, tuple(made.values())

    return scene


def _second_class_by_set_db(field_first: bool):
    async def scene(base, db):
        Target = declare(base, "Target", {"name": (str, "")})
        Holder = declare(base, "Holder", owner_of(Target))
        Plain = declare(base, "Plain", {"note": (str, "")})
        table = f"dib_bound_{next(_tables)}"
        for cls in (Holder, Plain) if field_first else (Plain, Holder):
            cls.set_db(db, table)
        t = await settle(Target(name="t").save(db=db))
        await settle(Holder(owner=t).save(db=db))
        return t, table, (Holder, Plain)

    return scene


async def _class_defined_after_the_first_delete(base, db):
    Target = declare(base, "Target", {"name": (str, "")})
    Base = declare(base, "Base", {"name": (str, "")})
    first = await settle(Target(name="first").save(db=db))
    await settle(first.delete(db=db))
    Sub = declare(Base, "Sub", owner_of(Target), table=False)
    t = await settle(Target(name="t").save(db=db))
    await settle(Sub(owner=t).save(db=db))
    return t, Base.__tablename__, (Base, Sub)


async def _base_bound_elsewhere_after_its_subclass_wrote_rows(base, db):
    Target = declare(base, "Target", {"name": (str, "")})
    Base = declare(base, "Base", {"name": (str, "")})
    Sub = declare(Base, "Sub", owner_of(Target), table=False)
    t = await settle(Target(name="t").save(db=db))
    await settle(Sub(owner=t).save(db=db))
    table = Base.__tablename__
    Base.set_db(db, f"dib_elsewhere_{next(_tables)}")
    return t, table, (Base, Sub)


async def _subclass_field_of_the_same_name_is_no_reference(base, db):
    Target = declare(base, "Target", {"name": (str, "")})
    Base = declare(base, "Base", owner_of(Target))
    Sub = declare(Base, "Sub", {"owner": (Optional[str], None)}, table=False)
    t = await settle(Target(name="t").save(db=db))
    await settle(Base(owner=t).save(db=db))
    await settle(Sub(owner="text").save(db=db))
    return t, Base.__tablename__, (Base, Sub)


async def _subclass_with_a_string_annotation_resolved_later(base, db):
    module = forward_module()
    later = f"Later{next(_tables)}"
    Base = declare(base, "Base", {"name": (str, "")})
    Sub = declare(Base, "Sub", {"owner": (Optional[later], None)}, module=module, table=False)
    Target = declare(base, later, {"name": (str, "")})
    resolve(module, Sub, Target)
    t = await settle(Target(name="t").save(db=db))
    await settle(Sub(owner=t).save(db=db))
    return t, Base.__tablename__, (Base, Sub)


async def _rows_read_and_written_through_using_a_table(base, db):
    Target = declare(base, "Target", {"name": (str, "")})
    Holder = declare(base, "Holder", {"name": (str, ""), **owner_of(Target)})
    table = f"dib_other_{next(_tables)}"
    t = await settle(Target(name="t").save(db=db))
    await settle(db.insert_document(table, {"name": "h", "owner": ref(Target.__tablename__, t._id)}))
    h = await settle(Holder.using(db, table).first())
    h.name = "read"
    await settle(h.save())
    return t, table, (Holder,)


SHARED_TABLES = {
    "a subclass adds the field": _subclass_adds_the_field,
    "a subclass of a subclass adds the field": _subclass_of_a_subclass_adds_the_field,
    "a second class by __tablename__, defined after": _second_class_by_tablename(False),
    "a second class by __tablename__, defined before": _second_class_by_tablename(True),
    "a second class by set_db, bound after": _second_class_by_set_db(False),
    "a second class by set_db, bound before": _second_class_by_set_db(True),
    "a class defined after the first delete": _class_defined_after_the_first_delete,
    "a base bound elsewhere after its subclass wrote rows": _base_bound_elsewhere_after_its_subclass_wrote_rows,
    "a subclass field of the same name is no reference": _subclass_field_of_the_same_name_is_no_reference,
    "a subclass with a string annotation resolved later": _subclass_with_a_string_annotation_resolved_later,
    "rows read and written through using(db, table)": _rows_read_and_written_through_using_a_table,
}


async def owners(db, table: str) -> list:
    return [json.loads(r[0]).get("owner") for r in await rows(db, f"SELECT data FROM {table} ORDER BY _id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("scene", list(SHARED_TABLES))
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_the_fields_of_every_class_whose_rows_live_in_a_table_count(base, scene):
    """A row of a table refers to a target at a position any class whose rows live there
    declares as a model: restrict refuses, set_null nulls it, cascade deletes the row; by
    ``delete()`` and by ``delete_all()``."""
    for way, policy in itertools.product(("delete()", "delete_all()"), ("restrict", "set_null", "cascade")):
        db = await open_db(base)
        try:
            # the classes are held here, as a program holds the classes it defines: a
            # class nothing holds any more is not looked at
            t, table, _classes = await SHARED_TABLES[scene](base, db)
            Target = type(t)
            held = ref(Target.__tablename__, t._id)
            before = await owners(db, table)
            assert before.count(held) == 1
            if way == "delete()":
                def act():
                    return t.delete_with_policy(db=db, on_delete=policy)
            else:
                def act():
                    return Target.using(db).filter(F("name") == "t").delete_all(on_delete=policy)

            if policy == "restrict":
                with pytest.raises(
                    ReferentialIntegrityError,
                    match=rf"^Cannot delete {Target.__tablename__}:{t._id}; referenced by 1 row\(s\)$",
                ):
                    await settle(act())
                left, after = [(t._id, "t")], before
            else:
                await settle(act())
                left = []
                if policy == "set_null":
                    after = [None if o == held else o for o in before]
                else:
                    after = [o for o in before if o != held]

            assert (way, policy, await holds(db, Target.__tablename__, "$.name")) == (way, policy, left)
            assert (way, policy, await owners(db, table)) == (way, policy, after)
        finally:
            await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_referring_class_whose_table_is_not_in_this_database(base):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))

        await settle(target.delete(db=db))

        assert await holds(db, Target.__tablename__, "$.name") == []
        assert await rows(
            db, f"SELECT name FROM sqlite_master WHERE name = '{Referrer.__tablename__}'"
        ) == []
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_references_to_other_ids_and_to_the_same_id_of_another_table_are_not_referrers(base):
    db = await open_db(base)
    try:
        Target = declare(base, "Target", {"name": (str, "")})
        Other = declare(base, "Other", {"name": (str, "")})
        Holder = declare(
            base,
            "Holder",
            {
                "target": (Optional[Target], None),
                "items": (Optional[list[Target]], None),
                "other": (Optional[Other], None),
            },
        )
        target = await settle(Target(name="t").save(db=db))
        await settle(Other(name="o").save(db=db))
        await settle(Holder().save(db=db))
        await put(db, Holder.__tablename__, 1, "$.target", ref(Target.__tablename__, 10))
        await put(db, Holder.__tablename__, 1, "$.items", [ref(Target.__tablename__, 11)])
        await put(db, Holder.__tablename__, 1, "$.other", ref(Other.__tablename__, 1))

        await settle(target.delete(db=db))

        assert await holds(db, Target.__tablename__, "$.name") == []
        assert await holds(db, Holder.__tablename__) == [(1, 10)]
        assert await holds(db, Holder.__tablename__, "$.other._id") == [(1, 1)]
    finally:
        await settle(db.close())


# ----- delete_all() meets the delete policy ----------------------------------------------------


async def two_thousand(base, db, policy: str):
    Target, Referrer = pair(base)
    await settle(Target.save_many([Target(name=f"t{i}") for i in range(2000)], db=db))
    await settle(Referrer(title="outside").save(db=db))
    await put(db, Referrer.__tablename__, 1, "$.target", ref(Target.__tablename__, 1500))
    return Target, Referrer


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_of_two_thousand_rows_with_one_outside_referrer_is_refused(base):
    db = await open_db(base)
    try:
        Target, Referrer = await two_thousand(base, db, "restrict")

        async def act():
            with pytest.raises(
                ReferentialIntegrityError,
                match=rf"^Cannot delete 2000 rows of {Target.__tablename__}; referenced by 1 row\(s\)$",
            ):
                await settle(Target.using(db).delete_all())

        statements = await sent(act)

        assert await rows(db, f"SELECT count(*) FROM {Target.__tablename__}") == [(2000,)]
        assert await holds(db, Referrer.__tablename__) == [(1, 1500)]
        assert [s.split()[0] for s in statements if f" {Target.__tablename__} " in s] == ["DELETE"]
        assert [s for s in statements if f" {Target.__tablename__} " not in s] == [
            f"SELECT _id, data FROM {Referrer.__tablename__} WHERE (((data GLOB '*{Target.__tablename__}*' OR "
            "data GLOB '*target*' OR data GLOB '*\\u*')) OR data >= x'') AND "
            "(json_extract(data, '$.target._id') IN (SELECT value FROM json_each(?1)));"
        ]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,referrer", [("set_null", [(1, None)]), ("cascade", [])], ids=["set_null", "cascade"]
)
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_of_two_thousand_rows_with_set_null_and_cascade(base, policy, referrer):
    db = await open_db(base)
    try:
        Target, Referrer = await two_thousand(base, db, policy)

        assert await settle(Target.using(db).delete_all(on_delete=policy)) == 2000

        assert await rows(db, f"SELECT count(*) FROM {Target.__tablename__}") == [(0,)]
        assert await holds(db, Referrer.__tablename__) == referrer
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_cascade_of_a_set_through_three_classes_with_a_cycle(base):
    """``A`` <- ``B`` <- ``C`` <- ``A``: deleting one ``A`` row deletes the ``B`` row that
    refers to it, the ``C`` row that refers to that, and the other ``A`` row that refers to
    the ``C`` row; each once."""
    db = await open_db(base)
    try:
        module = forward_module()
        c_name = f"C{next(_tables)}"
        A = declare(base, "A", {"name": (str, ""), "c": (Optional[c_name], None)}, module=module)
        B = declare(base, "B", {"a": (Optional[A], None)})
        C = declare(base, c_name, {"b": (Optional[B], None)})
        resolve(module, A, B, C)
        for doc in ({"name": "set"}, {"name": "outside", "c": ref(C.__tablename__, 1)}):
            await settle(db.insert_document(A.__tablename__, doc))
        await settle(db.insert_document(B.__tablename__, {"a": ref(A.__tablename__, 1)}))
        await settle(db.insert_document(C.__tablename__, {"b": ref(B.__tablename__, 1)}))

        deleted = await settle(A.using(db).filter(F("name") == "set").delete_all(on_delete="cascade"))

        assert deleted == 1
        for cls in (A, B, C):
            assert await rows(db, f"SELECT count(*) FROM {cls.__tablename__}") == [(0,)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("referable", [False, True], ids=["nothing can refer", "a class can refer"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_of_nothing_sends_only_its_own_statement(base, referable):
    db = await open_db(base)
    try:
        Target, _ = pair(base) if referable else (declare(base, "Alone", {"name": (str, "")}), None)
        await settle(Target(name="t").save(db=db))
        q = Target.using(db).filter(F("name") == "none")

        statements = await sent(lambda: settle(q.delete_all()))

        version = ["_version"] if "Safe" in base.__name__ else []
        returning = " RETURNING " + ", ".join(["_id", "data", *["IFNULL(_version, 0)"] * len(version)])
        t = Target.__tablename__
        row = ", ".join(f"`{name}`" for name in ["_id", "data", *version])
        guard = f"CASE WHEN 1 THEN 1 ELSE (SELECT ({row}) IN (SELECT * FROM {t}) FROM {t}) END"  # never runs
        assert statements == [
            f"DELETE FROM {t} WHERE JSON_EXTRACT(data, '$.name') = ? AND {guard}" + (returning if referable else "")
        ]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["delete()", "delete_all()"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_a_delete_of_a_class_nothing_can_refer_to_reads_no_other_table(base, way):
    db = await open_db(base)
    try:
        pair(base)  # a class that can refer, to another class
        Alone = declare(base, "Alone", {"name": (str, "")})
        alone = await settle(Alone(name="a").save(db=db))

        if way == "delete()":
            statements = await sent(lambda: settle(alone.delete(db=db)))
        else:
            statements = await sent(lambda: settle(Alone.using(db).delete_all()))

        assert [s for s in statements if Alone.__tablename__ not in s] == []
        assert [s.split()[0] for s in statements if s.split()[0] != "CREATE"] == ["DELETE"]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["query()", "using(db)", "using(db, table)"])
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_refuses_through_every_door(base, door):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        Alone = declare(base, "Alone", {"name": (str, "")})
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(target=target).save(db=db))
        if door == "query()":
            Target.set_db(db)
            q = Target.query()
        elif door == "using(db)":
            q = Target.using(db)
        else:
            q = Alone.using(db, Target.__tablename__)

        with pytest.raises(ReferentialIntegrityError, match=rf"^Cannot delete {Target.__tablename__}:1;"):
            await settle(q.delete_all())

        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_with_an_unknown_policy_is_refused_before_anything_runs(base):
    db = await open_db(base)
    try:
        Target, _ = pair(base)
        await settle(Target(name="t").save(db=db))
        q = Target.using(db)

        async def act():
            with pytest.raises(ValueError, match="^on_delete must be 'restrict', 'set_null', or 'cascade'$"):
                await settle(q.delete_all(on_delete="CASCADE"))

        assert await sent(act) == []
        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_in_a_transaction_the_caller_rolls_back_leaves_both_tables(base):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(target=target).save(db=db))

        async def act():
            assert await settle(Target.using(db).delete_all(on_delete="set_null")) == 1
            raise RuntimeError("the caller changes its mind")

        with pytest.raises(RuntimeError, match="changes its mind"):
            await in_transaction(db, act)

        assert await holds(db, Target.__tablename__, "$.name") == [(1, "t")]
        assert await holds(db, Referrer.__tablename__) == [(1, 1)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_with_an_order_and_a_limit_deletes_and_looks_at_every_match(base):
    """A ``DELETE`` takes the filter only, as it did: the limit and the order are not used,
    and the look covers every row deleted."""
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        for name in ("a", "b", "c"):
            await settle(Target(name=name).save(db=db))
        await settle(Referrer().save(db=db))
        await put(db, Referrer.__tablename__, 1, "$.target", ref(Target.__tablename__, 3))
        q = Target.using(db).order_by("name").limit(1)

        with pytest.raises(ReferentialIntegrityError, match=r"^Cannot delete 3 rows of "):
            await settle(q.delete_all())
        assert await settle(q.delete_all(on_delete="set_null")) == 3

        assert await holds(db, Target.__tablename__, "$.name") == []
        assert await holds(db, Referrer.__tablename__) == [(1, None)]
    finally:
        await settle(db.close())


SAFE_BASES = [b for b in BASES if "Safe" in b.__name__]


@pytest.mark.asyncio
@pytest.mark.parametrize("base", SAFE_BASES, ids=lambda b: b.__name__)
async def test_a_versioned_referrer_nulled_by_delete_all_refuses_a_stale_save(base):
    db = await open_db(base)
    try:
        Target, Referrer = pair(base)
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r", target=target).save(db=db))
        held = await settle(Referrer.from_id(1, db=db))

        assert await settle(Target.using(db).delete_all(on_delete="set_null")) == 1

        held.title = "a holder who loaded it before the delete"
        with pytest.raises(StaleVersionError):
            await settle(held.save(db=db))
        assert await holds(db, Referrer.__tablename__) == [(1, None)]
    finally:
        await settle(db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,targets,referrers",
    [
        ("restrict", [("create",)], [("create",)]),
        ("set_null", [("create",), ("delete",)], [("create",), ("update",)]),
        ("cascade", [("create",), ("delete",)], [("create",), ("delete",)]),
    ],
)
@pytest.mark.parametrize("base", BASES, ids=lambda b: b.__name__)
async def test_delete_all_of_an_audited_target_and_an_audited_referrer(base, policy, targets, referrers):
    db = await open_db(base)
    try:
        audit = AsyncAuditLogMixin if base in ASYNC_BASES else AuditLogMixin
        Target, Referrer = pair(base, (audit,))
        target = await settle(Target(name="t").save(db=db))
        await settle(Referrer(title="r", target=target).save(db=db))

        try:
            await settle(Target.using(db).delete_all(on_delete=policy))
        except ReferentialIntegrityError:
            assert policy == "restrict"

        for cls, actions in ((Target, targets), (Referrer, referrers)):
            assert await rows(db, f"SELECT action FROM {cls.__tablename__}_audit ORDER BY _id") == actions
    finally:
        await settle(db.close())
