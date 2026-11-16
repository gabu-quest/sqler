import asyncio

from sqler import AsyncSQLerDB, AsyncSQLerSafeModel, StaleVersionError


class AAccount(AsyncSQLerSafeModel):
    owner: str
    balance: int


async def amain():
    db = AsyncSQLerDB.in_memory()
    await db.connect()

    acc = AAccount(owner="Ada", balance=100)
    await acc.save(db=db)
    print("version:", acc._version)

    acc.balance = 120
    await acc.save(db=db)
    print("version:", acc._version)

    try:
        # simulate external writer bump
        await db.adapter.execute(
            "UPDATE aaccounts SET _version = _version + 1 WHERE _id = ?;",
            [acc._id],
        )
        await db.adapter.commit()
        await acc.save(db=db)
    except StaleVersionError:
        await acc.refresh(db=db)
        print("stale detected, refreshed to version:", acc._version)

    await db.close()


if __name__ == "__main__":
    asyncio.run(amain())
