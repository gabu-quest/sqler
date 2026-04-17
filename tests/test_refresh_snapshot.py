"""Regression test for H-4: SQLerModel.refresh() must update _snapshot.

Before this fix, calling refresh() on a Pydantic SQLerModel updated the fields
in place but left _snapshot at its stale pre-refresh value. This caused any
dirty-tracking consumer (e.g. TrackedModel, PartialUpdateMixin) to see stale
state — potentially writing back fields that had already converged with the DB.

The Lite and Msgspec backends already do this correctly; this test locks in
parity for the Pydantic backend.
"""


import pytest
from sqler import SQLerModel
from sqler.db.sqler_db import SQLerDB


class User(SQLerModel):
    __tablename__ = "users"
    name: str = ""
    age: int = 0


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    yield d
    d.close()


def test_refresh_updates_snapshot_to_match_db(db):
    """After refresh, _snapshot must reflect the DB state, not the pre-refresh state."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        User.set_db(db)

    u = User(name="alice", age=30)
    u.save()
    assert u._id is not None
    row_id = u._id

    # Load a fresh instance via the queryset path, which populates _snapshot
    hydrated = User.query().first()
    assert hydrated is not None
    assert hydrated._id == row_id
    assert hydrated._snapshot is not None
    assert hydrated._snapshot.get("name") == "alice"
    assert hydrated._snapshot.get("age") == 30

    # Simulate a concurrent update landing in the DB
    db.upsert_document("users", row_id, {"name": "alice-updated", "age": 31})

    # Local mutations that would otherwise make the instance "dirty"
    hydrated.name = "alice-local"
    hydrated.age = 99

    # refresh() pulls the new DB state in
    hydrated.refresh()
    assert hydrated.name == "alice-updated"
    assert hydrated.age == 31

    # THIS is the fix: _snapshot must reflect the refreshed DB state, not the stale one.
    assert hydrated._snapshot is not None
    assert hydrated._snapshot.get("name") == "alice-updated"
    assert hydrated._snapshot.get("age") == 31


def test_refresh_snapshot_excludes_internal_keys(db):
    """_snapshot should exclude _id and _version (mirrors queryset._attach_metadata)."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        User.set_db(db)

    u = User(name="bob", age=25)
    u.save()
    hydrated = User.query().first()
    assert hydrated is not None
    hydrated.refresh()

    assert hydrated._snapshot is not None
    assert "_id" not in hydrated._snapshot
    assert "_version" not in hydrated._snapshot
