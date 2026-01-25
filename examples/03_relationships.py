from sqler import SQLerDB, SQLerModel


class Address(SQLerModel):
    city: str
    country: str


class User(SQLerModel):
    name: str
    address: Address | None = None


def main():
    db = SQLerDB.in_memory()
    Address.set_db(db)
    User.set_db(db)

    home = Address(city="Kyoto", country="JP").save()
    u = User(name="Alice", address=home).save()

    # filter by related
    from sqler.models import SQLerModelField as MF

    users = User.query().filter(MF(User, ["address", "city"]) == "Kyoto").all()
    print([x.name for x in users])

    # sugar
    users2 = User.query().filter(User.ref("address").field("city") == "Kyoto").all()
    print([x.name for x in users2])

    # hydration controls
    raw = User.query().resolve(False).all()
    print(type(raw[0].address))  # ref dict after validation

    # update nested and refresh
    home.city = "Osaka"
    home.save()
    u.refresh()
    print(u.address.city)

    db.close()


if __name__ == "__main__":
    main()
