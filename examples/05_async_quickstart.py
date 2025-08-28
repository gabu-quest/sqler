import asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.query import SQLerField as F


class AUser(AsyncSQLerModel):
    name: str
    age: int


async def main():
    db = AsyncSQLerDB.in_memory()
    await db.connect()
    AUser.set_db(db)

    await AUser(name="Alice", age=30).save()
    adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
    print([u.name for u in adults])

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
