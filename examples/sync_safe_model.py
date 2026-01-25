from sqler import SQLerDB, SQLerSafeModel, StaleVersionError


class Account(SQLerSafeModel):
    owner: str
    balance: int


def main():
    db = SQLerDB.in_memory()
    Account.set_db(db)

    acc = Account(owner="Ada", balance=100)
    acc.save()
    print("version:", acc._version)

    acc.balance = 120
    acc.save()
    print("version:", acc._version)

    try:
        # simulate external writer bump
        db.adapter.execute(
            "UPDATE accounts SET _version = _version + 1 WHERE _id = ?;",
            [acc._id],
        )
        db.adapter.commit()
        acc.balance = 130
        acc.save()
    except StaleVersionError:
        acc.refresh()
        print("stale detected, refreshed to version:", acc._version)

    db.close()


if __name__ == "__main__":
    main()
