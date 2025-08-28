from sqler import SQLerDB
from sqler.models import SQLerModel


class Address(SQLerModel):
    city: str


class User(SQLerModel):
    name: str
    address: Address | None = None


def test_batch_hydration_reduces_fetches(monkeypatch):
    db = SQLerDB.in_memory(shared=False)
    Address.set_db(db)
    User.set_db(db)

    a1 = Address(city="Kyoto").save()
    a2 = Address(city="Osaka").save()
    a3 = Address(city="Tokyo").save()

    # 200 users referencing 3 addresses
    for i in range(200):
        addr = [a1, a2, a3][i % 3]
        User(name=f"U{i}", address=addr).save()

    # instrument adapter to count address selects
    original_execute = db.adapter.execute
    counter = {"address_selects": 0}

    def wrapped_execute(sql, params=None):
        if (
            sql.strip().lower().startswith("select _id, data from address")
            and " in (" in sql.lower()
        ):
            counter["address_selects"] += 1
        return original_execute(sql, params)

    monkeypatch.setattr(db.adapter, "execute", wrapped_execute)

    users = User.query().order_by("name").all()
    # expect at most 1 batched select per table
    assert counter["address_selects"] <= 1
    # ensure hydrated
    assert isinstance(users[0].address, Address)
