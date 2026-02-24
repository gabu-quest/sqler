import pytest
from sqler.models import StaleVersionError
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.utils import RebaseConfig
from sqler.query import SQLerField as F


class ACustomer(AsyncSQLerSafeModel):
    name: str
    tier: int


@pytest.mark.asyncio
async def test_async_safe_version_bumps_and_stale(async_db):
    ACustomer.set_db(async_db)
    try:
        c = ACustomer(name="Bob", tier=1)
        await c.save()
        assert c._version == 0

        c.tier = 2
        await c.save()
        assert c._version == 1

        # simulate an external writer bumping version
        await async_db.adapter.execute(
            "UPDATE acustomers SET _version = _version + 1 WHERE _id = ?;", [c._id]
        )
        await async_db.adapter.commit()

        c.tier = 3
        with pytest.raises(StaleVersionError):
            await c.save()
    finally:
        ACustomer.set_db(None)


@pytest.mark.asyncio
async def test_async_safe_query_and_refresh(async_db):
    ACustomer.set_db(async_db)
    try:
        await ACustomer(name="A", tier=1).save()
        await ACustomer(name="B", tier=2).save()

        res = await ACustomer.query().filter(F("tier") >= 2).all()
        assert [r.name for r in res] == ["B"]

        first = await ACustomer.query().order_by("tier", desc=True).first()
        assert first is not None
        await first.refresh()
        assert first._version >= 0
    finally:
        ACustomer.set_db(None)


@pytest.mark.asyncio
async def test_async_safe_rebase_retry_exhaustion(async_db):
    """Rebase retries exhaust after max_retries, raising StaleVersionError."""

    class ACounter(AsyncSQLerSafeModel):
        count: int = 0
        _rebase_config = RebaseConfig(
            enabled=True,
            allowed_fields={"count"},
            max_delta=1,
            max_retries=2,
        )

    ACounter.set_db(async_db)
    try:
        counter = await ACounter(count=0).save()
        stale = await ACounter.from_id(counter._id)

        # Bump version behind stale's back so rebase kicks in
        counter.count += 1
        await counter.save()

        stale.count += 1

        # Monkey-patch adapter.commit to bump version on every retry,
        # ensuring the stale copy can never catch up.
        original_commit = async_db.adapter.commit
        call_count = 0

        async def sabotaging_commit():
            nonlocal call_count
            await original_commit()
            call_count += 1
            if call_count <= 3:
                await async_db.adapter.execute(
                    "UPDATE acounters SET _version = _version + 1 WHERE _id = ?;",
                    [stale._id],
                )
                await original_commit()

        async_db.adapter.commit = sabotaging_commit

        with pytest.raises(StaleVersionError, match="retries exhausted"):
            await stale.save()
    finally:
        async_db.adapter.commit = original_commit
        ACounter.set_db(None)
