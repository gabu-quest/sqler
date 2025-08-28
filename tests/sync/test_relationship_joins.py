from sqler import SQLerDB
from sqler.models import SQLerModel
from sqler.models import SQLerModelField as MF
from sqler.query import SQLerField as F


class Address(SQLerModel):
    city: str
    country: str


class User(SQLerModel):
    name: str
    address: Address | None = None


def test_relationship_join_exists_query():
    db = SQLerDB.in_memory(shared=False)
    Address.set_db(db)
    User.set_db(db)

    a1 = Address(city="Kyoto", country="JP").save()
    a2 = Address(city="Osaka", country="JP").save()
    User(name="Alice", address=a1).save()
    User(name="Bob", address=a2).save()
    User(name="Carol", address=a1).save()

    # users where address.city == Kyoto
    qs = User.query().filter(MF(User, ["address", "city"]) == "Kyoto").order_by("name")
    res = qs.all()
    assert [u.name for u in res] == ["Alice", "Carol"]

    # combine with other predicates
    res2 = (
        User.query()
        .filter(User.ref("address").field("city") == "Osaka")
        .exclude(F("name").like("C%"))
        .all()
    )
    assert [u.name for u in res2] == ["Bob"]

    # join + any() over list of refs: model sugar
    class Order(SQLerModel):
        total: int

    Order.set_db(db)
    # extend User to include orders list via direct payload manipulation for test brevity
    o1 = Order(total=50).save()
    o2 = Order(total=150).save()
    # attach [o1,o2] to Alice
    alice = User.query().filter(F("name") == "Alice").first()
    alice_dict = db.find_document("users", alice._id)
    alice_dict["orders"] = [
        {"_table": "orders", "_id": o1._id},
        {"_table": "orders", "_id": o2._id},
    ]
    db.upsert_document("users", alice._id, {k: v for k, v in alice_dict.items() if k != "_id"})

    rich = User.query().filter(User.ref("orders").any().field("total") > 100).order_by("name").all()
    assert [u.name for u in rich] == ["Alice"]
