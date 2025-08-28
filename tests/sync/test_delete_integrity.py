import pytest

from sqler import SQLerDB
from sqler.models import ReferentialIntegrityError, SQLerModel


class Address(SQLerModel):
    city: str


class User(SQLerModel):
    name: str
    address: Address | None = None
    friends: list[Address | None] | None = None


def test_restrict_blocks_when_referenced():
    db = SQLerDB.in_memory(shared=False)
    Address.set_db(db)
    User.set_db(db)
    a = Address(city="Kyoto").save()
    User(name="Alice", address=a).save()
    with pytest.raises(ReferentialIntegrityError):
        a.delete_with_policy(on_delete="restrict")


def test_set_null_clears_single_ref_and_list_refs():
    db = SQLerDB.in_memory(shared=False)
    Address.set_db(db)
    User.set_db(db)
    a1 = Address(city="Kyoto").save()
    a2 = Address(city="Osaka").save()
    u = User(name="Bob", address=a1, friends=[a1, a2]).save()

    a1.delete_with_policy(on_delete="set_null")
    u2 = User.from_id(u._id)
    assert u2.address is None
    assert u2.friends[0] is None
    assert u2.friends[1].city == "Osaka"


def test_cascade_deletes_referrers_and_avoids_cycles():
    db = SQLerDB.in_memory(shared=False)
    Address.set_db(db)
    User.set_db(db)
    a = Address(city="Tokyo").save()
    u = User(name="Alice", address=a).save()
    # cascade should remove user first, then address
    a.delete_with_policy(on_delete="cascade")
    assert Address.from_id(a._id) is None
    assert User.from_id(u._id) is None
