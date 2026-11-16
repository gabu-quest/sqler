import pytest

from sqler import AsyncSQLerDB
from sqler.models import AsyncSQLerModel


class AAddress(AsyncSQLerModel):
    city: str


class AUser(AsyncSQLerModel):
    name: str
    address: AAddress | None = None


@pytest.mark.asyncio
async def test_async_batch_hydration(async_db: AsyncSQLerDB, monkeypatch):
    AAddress.set_db(async_db)
    AUser.set_db(async_db)

    a1 = await AAddress(city="Kyoto").save()
    a2 = await AAddress(city="Osaka").save()
    a3 = await AAddress(city="Tokyo").save()

    for i in range(200):
        addr = [a1, a2, a3][i % 3]
        await AUser(name=f"U{i}", address=addr).save()

    original_execute = async_db.adapter.execute
    counter = {"address_in": 0}

    async def wrapped_execute(sql, params=None):
        low = sql.strip().lower()
        if low.startswith("select _id, data from aaddress") and " in (" in low:
            counter["address_in"] += 1
        return await original_execute(sql, params)

    monkeypatch.setattr(async_db.adapter, "execute", wrapped_execute)

    users = await AUser.query().order_by("name").all()
    assert counter["address_in"] <= 1
    assert users and users[0].address is not None
