import pytest

from sqler import AsyncSQLerDB
from sqler.models import AsyncSQLerModel


class AAddress(AsyncSQLerModel):
    city: str
    country: str


class AUser(AsyncSQLerModel):
    name: str
    address: AAddress | None = None


@pytest.mark.asyncio
async def test_async_relationships_save_load_refresh(async_db: AsyncSQLerDB):
    AAddress.set_db(async_db)
    AUser.set_db(async_db)

    addr = await AAddress(city="Kyoto", country="JP").save()
    u = AUser(name="Alice", address=addr)
    await u.save()
    assert u._id is not None and u.address and u.address._id is not None

    loaded = await AUser.from_id(u._id)
    assert loaded is not None
    assert isinstance(loaded.address, AAddress)
    assert loaded.address.city == "Kyoto"

    # update nested and refresh
    loaded.address.city = "Osaka"
    await loaded.address.save()
    await loaded.refresh()
    assert loaded.address.city == "Osaka"
