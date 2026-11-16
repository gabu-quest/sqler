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


def test_delete_with_policy_invalid_mode_raises():
    """Invalid on_delete mode raises ValueError with helpful message."""
    db = SQLerDB.in_memory(shared=False)
    Address.set_db(db)
    a = Address(city="Tokyo").save()

    with pytest.raises(ValueError, match="on_delete must be"):
        a.delete_with_policy(on_delete="destroy")

    with pytest.raises(ValueError, match="on_delete must be"):
        a.delete_with_policy(on_delete="")

    with pytest.raises(ValueError, match="on_delete must be"):
        a.delete_with_policy(on_delete="CASCADE")  # case-sensitive


def test_find_ref_paths_reads_any_position_and_only_a_reference_counts():
    """``find_ref_paths`` takes no class: it finds a reference at any position. A reference is
    exactly a str ``_table`` and an int ``_id``; any other dict (a text or bool ``_id``, a key
    more) is data, walked into and never counted."""
    from sqler.models.integrity import find_ref_paths

    def ref(rid):
        return {"_table": "users", "_id": rid}

    doc = {
        "author": ref(1),
        "comments": [{"user": ref(1)}, ref(2)],
        "as_text": ref("1"),
        "as_bool": ref(True),
        "no_number": ref("x"),
        "other_table": {"_table": "posts", "_id": 1},
        "outer": {**ref(1), "inner": ref(1)},
        "with_more_keys": {**ref(1), "label": "data"},
    }

    assert find_ref_paths(doc, {"users"}, 1) == ["author", "comments[0].user", "outer.inner"]
    assert find_ref_paths(ref(1), {"users"}, 1) == ["$"]
