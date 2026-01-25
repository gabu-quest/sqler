from sqler import SQLerDB
from sqler.models import SQLerModel


class Address(SQLerModel):
    city: str
    country: str


class User(SQLerModel):
    name: str
    address: Address | None = None


def test_relationship_save_load_refresh():
    db = SQLerDB.in_memory(shared=False)
    Address.set_db(db)
    User.set_db(db)

    addr = Address(city="Kyoto", country="JP").save()
    u = User(name="Alice", address=addr)
    u.save()

    # user and address get ids
    assert u._id is not None
    assert u.address is not None and u.address._id is not None

    # loading the user hydrates the address as a model instance
    loaded = User.from_id(u._id)
    assert isinstance(loaded, User)
    assert isinstance(loaded.address, Address)
    assert loaded.address.city == "Kyoto"

    # change the address directly and refresh the user
    loaded.address.city = "Osaka"
    loaded.address.save()
    loaded.refresh()
    assert loaded.address.city == "Osaka"

    db.close()
