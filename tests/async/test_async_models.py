import pytest

from sqler import AsyncSQLerModel
from sqler.query import SQLerField as F


class AUser(AsyncSQLerModel):
    name: str
    age: int


@pytest.mark.asyncio
async def test_async_model_crud_and_query(async_db):
    AUser.set_db(async_db)
    try:
        u = AUser(name="Alice", age=30)
        await u.save()
        assert u._id is not None

        u2 = await AUser.from_id(u._id)
        assert u2 and u2.name == "Alice"

        adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
        assert [a.name for a in adults] == ["Alice"]

        u.age = 31
        await u.save()
        u.age = 0
        await u.refresh()
        assert u.age == 31
    finally:
        # Important if the class keeps a reference
        AUser.set_db(None)
