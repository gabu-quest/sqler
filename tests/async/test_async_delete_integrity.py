"""Tests for async referential integrity support."""

import pytest
from sqler.db.async_db import AsyncSQLerDB
from sqler.models import (
    AsyncSQLerModel,
    async_find_referrers,
    async_set_null_referrers,
    async_cascade_delete,
    async_validate_references,
    BrokenRef,
)


class IntegrityAddress(AsyncSQLerModel):
    city: str


class IntegrityUser(AsyncSQLerModel):
    name: str
    address: IntegrityAddress | None = None
    friends: list[IntegrityAddress | None] | None = None


@pytest.mark.asyncio
async def test_delete_with_policy_restrict(async_db: AsyncSQLerDB):
    """Test restrict mode simply deletes without checking references."""
    IntegrityAddress.set_db(async_db, "integrity_address")
    IntegrityUser.set_db(async_db, "integrity_user")

    a = IntegrityAddress(city="Kyoto")
    await a.save()
    u = IntegrityUser(name="Alice", address=a)
    await u.save()

    # Restrict doesn't check references, it just deletes
    await a.delete_with_policy(on_delete="restrict")
    assert await IntegrityAddress.from_id(a._id) is None
    # User still exists with dangling reference
    u2 = await IntegrityUser.from_id(u._id)
    assert u2 is not None


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
