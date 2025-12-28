"""Tests for sync bulk operations: find_documents, from_ids, count."""

from sqler import SQLerDB
from sqler.models import SQLerModel


class BulkUser(SQLerModel):
    name: str
    age: int


class BulkAddress(SQLerModel):
    city: str
    country: str


class BulkUserWithAddr(SQLerModel):
    name: str
    address: BulkAddress | None = None


# --- Sync Tests ---


def test_find_documents_batch():
    """Test db.find_documents() fetches multiple docs in one query."""
    db = SQLerDB.in_memory(shared=False)
    BulkUser.set_db(db)

    # Insert 5 users
    users = [BulkUser(name=f"User{i}", age=20 + i).save() for i in range(5)]
    ids = [u._id for u in users]

    # Fetch all in one batch
    docs = db.find_documents("bulkusers", ids)
    assert len(docs) == 5
    assert [d["name"] for d in docs] == ["User0", "User1", "User2", "User3", "User4"]


def test_find_documents_preserves_order():
    """Test that find_documents returns docs in input order."""
    db = SQLerDB.in_memory(shared=False)
    BulkUser.set_db(db)

    users = [BulkUser(name=f"User{i}", age=20 + i).save() for i in range(5)]
    ids = [u._id for u in users]

    # Request in reverse order
    reversed_ids = list(reversed(ids))
    docs = db.find_documents("bulkusers", reversed_ids)
    assert [d["name"] for d in docs] == ["User4", "User3", "User2", "User1", "User0"]


def test_find_documents_skips_missing():
    """Test that find_documents skips missing ids."""
    db = SQLerDB.in_memory(shared=False)
    BulkUser.set_db(db)

    users = [BulkUser(name=f"User{i}", age=20 + i).save() for i in range(3)]
    ids = [users[0]._id, 9999, users[2]._id]  # 9999 doesn't exist

    docs = db.find_documents("bulkusers", ids)
    assert len(docs) == 2
    assert [d["name"] for d in docs] == ["User0", "User2"]


def test_find_documents_empty_list():
    """Test find_documents with empty list returns empty."""
    db = SQLerDB.in_memory(shared=False)
    docs = db.find_documents("nonexistent", [])
    assert docs == []


def test_from_ids_batch():
    """Test Model.from_ids() hydrates multiple instances."""
    db = SQLerDB.in_memory(shared=False)
    BulkUser.set_db(db)

    users = [BulkUser(name=f"User{i}", age=20 + i).save() for i in range(5)]
    ids = [u._id for u in users]

    # Fetch all in one batch
    hydrated = BulkUser.from_ids(ids)
    assert len(hydrated) == 5
    assert all(isinstance(u, BulkUser) for u in hydrated)
    assert [u.name for u in hydrated] == ["User0", "User1", "User2", "User3", "User4"]


def test_from_ids_with_relations():
    """Test from_ids resolves nested relations."""
    db = SQLerDB.in_memory(shared=False)
    BulkAddress.set_db(db)
    BulkUserWithAddr.set_db(db)

    addr1 = BulkAddress(city="Tokyo", country="Japan").save()
    addr2 = BulkAddress(city="Paris", country="France").save()

    u1 = BulkUserWithAddr(name="Alice", address=addr1).save()
    u2 = BulkUserWithAddr(name="Bob", address=addr2).save()
    u3 = BulkUserWithAddr(name="Charlie", address=None).save()

    hydrated = BulkUserWithAddr.from_ids([u1._id, u2._id, u3._id])
    assert len(hydrated) == 3
    assert hydrated[0].address.city == "Tokyo"
    assert hydrated[1].address.city == "Paris"
    assert hydrated[2].address is None


def test_from_ids_empty_list():
    """Test from_ids with empty list returns empty."""
    db = SQLerDB.in_memory(shared=False)
    BulkUser.set_db(db)
    assert BulkUser.from_ids([]) == []


def test_from_ids_preserves_order():
    """Test from_ids preserves input order."""
    db = SQLerDB.in_memory(shared=False)
    BulkUser.set_db(db)

    users = [BulkUser(name=f"User{i}", age=20 + i).save() for i in range(5)]
    ids = [users[4]._id, users[0]._id, users[2]._id]

    hydrated = BulkUser.from_ids(ids)
    assert [u.name for u in hydrated] == ["User4", "User0", "User2"]


def test_model_count_shortcut():
    """Test Model.count() shortcut."""
    db = SQLerDB.in_memory(shared=False)
    BulkUser.set_db(db)

    assert BulkUser.count() == 0

    for i in range(10):
        BulkUser(name=f"User{i}", age=20 + i).save()

    assert BulkUser.count() == 10
