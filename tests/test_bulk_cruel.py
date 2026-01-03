"""Cruel and unusual tests for bulk operations.

These tests explore edge cases, stress scenarios, and unusual inputs
that might expose bugs in find_documents, from_ids, and count.
"""

import concurrent.futures

from sqler import SQLerDB
from sqler.models import SQLerModel


class CruelUser(SQLerModel):
    name: str
    score: int = 0


class CruelAddress(SQLerModel):
    city: str
    country: str


class CruelPerson(SQLerModel):
    name: str
    address: CruelAddress | None = None
    friends: list["CruelPerson"] | None = None


# --- Edge Cases for find_documents ---


def test_find_documents_duplicate_ids():
    """Test find_documents with duplicate IDs in input."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    u1 = CruelUser(name="Alice", score=100).save()
    u2 = CruelUser(name="Bob", score=200).save()

    # Request same ID multiple times
    ids = [u1._id, u1._id, u2._id, u1._id]
    docs = db.find_documents("cruelusers", ids)

    # Should return in order, with duplicates
    assert len(docs) == 4
    assert docs[0]["name"] == "Alice"
    assert docs[1]["name"] == "Alice"
    assert docs[2]["name"] == "Bob"
    assert docs[3]["name"] == "Alice"


def test_find_documents_all_missing():
    """Test find_documents when ALL ids are missing."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    CruelUser(name="Exists", score=1).save()

    # All these IDs don't exist
    docs = db.find_documents("cruelusers", [9999, 8888, 7777])
    assert docs == []


def test_find_documents_large_batch():
    """Test find_documents with 1000+ IDs."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    # Insert 500 users
    ids = []
    for i in range(500):
        u = CruelUser(name=f"User{i}", score=i).save()
        ids.append(u._id)

    # Request all + some non-existent
    request_ids = ids + [99999 + i for i in range(500)]
    docs = db.find_documents("cruelusers", request_ids)

    # Should get exactly 500 back
    assert len(docs) == 500
    # First 500 should be in order
    for i, doc in enumerate(docs):
        assert doc["name"] == f"User{i}"


def test_find_documents_zero_id():
    """Test find_documents with zero as ID (edge case)."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    CruelUser(name="Normal", score=1).save()

    # Zero is not a valid SQLite AUTOINCREMENT id
    docs = db.find_documents("cruelusers", [0])
    assert docs == []


def test_find_documents_negative_ids():
    """Test find_documents with negative IDs."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    CruelUser(name="Normal", score=1).save()

    # Negative IDs don't exist
    docs = db.find_documents("cruelusers", [-1, -100, -999])
    assert docs == []


def test_find_documents_mixed_valid_invalid():
    """Test find_documents with mix of valid, invalid, negative, zero IDs."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    u1 = CruelUser(name="Alice", score=100).save()
    u2 = CruelUser(name="Bob", score=200).save()

    # Mix of everything
    ids = [u1._id, -1, 0, 9999, u2._id, -100]
    docs = db.find_documents("cruelusers", ids)

    # Only valid ones returned, in input order
    assert len(docs) == 2
    assert docs[0]["name"] == "Alice"
    assert docs[1]["name"] == "Bob"


def test_find_documents_non_sequential_ids():
    """Test find_documents with non-sequential IDs after deletions."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    # Create and delete some to make gaps
    u1 = CruelUser(name="Keep1", score=1).save()
    u2 = CruelUser(name="Delete", score=2).save()
    u3 = CruelUser(name="Keep2", score=3).save()

    u2.delete()

    # IDs are now non-sequential (1, 3 - gap at 2)
    docs = db.find_documents("cruelusers", [u1._id, u2._id, u3._id])
    assert len(docs) == 2
    assert docs[0]["name"] == "Keep1"
    assert docs[1]["name"] == "Keep2"


# --- Edge Cases for from_ids ---


def test_from_ids_duplicate_ids():
    """Test from_ids with duplicate IDs."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    u = CruelUser(name="Solo", score=42).save()

    # Same ID 5 times
    hydrated = CruelUser.from_ids([u._id] * 5)
    assert len(hydrated) == 5
    assert all(h.name == "Solo" for h in hydrated)
    assert all(h._id == u._id for h in hydrated)


def test_from_ids_all_missing():
    """Test from_ids when all IDs are missing."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    CruelUser(name="Exists", score=1).save()

    hydrated = CruelUser.from_ids([9999, 8888, 7777])
    assert hydrated == []


def test_from_ids_with_deep_nesting():
    """Test from_ids with deeply nested relations."""
    db = SQLerDB.in_memory(shared=False)
    CruelAddress.set_db(db)
    CruelPerson.set_db(db)

    addr = CruelAddress(city="Tokyo", country="Japan").save()

    # Create chain: p1 -> p2 -> p3
    p3 = CruelPerson(name="Level3", address=addr, friends=None).save()
    p2 = CruelPerson(name="Level2", address=addr, friends=[p3]).save()
    p1 = CruelPerson(name="Level1", address=addr, friends=[p2]).save()

    # Hydrate all at once
    hydrated = CruelPerson.from_ids([p1._id, p2._id, p3._id])

    assert len(hydrated) == 3
    assert hydrated[0].name == "Level1"
    assert hydrated[0].friends[0].name == "Level2"
    assert hydrated[0].friends[0].friends[0].name == "Level3"
    assert hydrated[0].address.city == "Tokyo"


def test_from_ids_with_set_null_delete():
    """Test from_ids after a nested reference is deleted with set_null."""
    db = SQLerDB.in_memory(shared=False)
    CruelAddress.set_db(db)
    CruelPerson.set_db(db)

    addr = CruelAddress(city="Tokyo", country="Japan").save()
    person = CruelPerson(name="Test", address=addr).save()

    # Delete the address using set_null (nullifies refs first)
    addr.delete_with_policy(on_delete="set_null")

    # from_ids should work, address should be None now
    hydrated = CruelPerson.from_ids([person._id])
    assert len(hydrated) == 1
    assert hydrated[0].name == "Test"
    assert hydrated[0].address is None


def test_from_ids_large_batch():
    """Test from_ids with 1000 records."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    ids = []
    for i in range(1000):
        u = CruelUser(name=f"User{i}", score=i).save()
        ids.append(u._id)

    hydrated = CruelUser.from_ids(ids)
    assert len(hydrated) == 1000
    for i, u in enumerate(hydrated):
        assert u.name == f"User{i}"
        assert u.score == i


def test_from_ids_reverse_order():
    """Test from_ids returns in exact input order (reversed)."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    users = [CruelUser(name=f"User{i}", score=i).save() for i in range(10)]
    ids = [u._id for u in users]

    # Reverse order
    hydrated = CruelUser.from_ids(list(reversed(ids)))
    names = [u.name for u in hydrated]
    assert names == [f"User{i}" for i in range(9, -1, -1)]


def test_from_ids_random_order():
    """Test from_ids with shuffled order."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    users = [CruelUser(name=f"User{i}", score=i).save() for i in range(10)]
    ids = [u._id for u in users]

    # Specific shuffle pattern
    shuffled = [ids[5], ids[2], ids[9], ids[0], ids[7]]
    hydrated = CruelUser.from_ids(shuffled)

    expected = ["User5", "User2", "User9", "User0", "User7"]
    assert [u.name for u in hydrated] == expected


# --- Edge Cases for count ---


def test_count_after_deletions():
    """Test count reflects deletions properly."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    users = [CruelUser(name=f"User{i}", score=i).save() for i in range(10)]
    assert CruelUser.count() == 10

    # Delete half
    for u in users[:5]:
        u.delete()

    assert CruelUser.count() == 5


def test_count_empty_fresh_table():
    """Test count on brand new empty table."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)
    # Table created but empty
    assert CruelUser.count() == 0


# --- Concurrency Tests ---


def test_from_ids_concurrent_reads():
    """Test multiple threads calling from_ids simultaneously."""
    db = SQLerDB.in_memory(shared=True)
    CruelUser.set_db(db)

    # Create 100 users
    ids = []
    for i in range(100):
        u = CruelUser(name=f"User{i}", score=i).save()
        ids.append(u._id)

    results = []
    errors = []

    def reader():
        try:
            hydrated = CruelUser.from_ids(ids)
            results.append(len(hydrated))
        except Exception as e:
            errors.append(e)

    # 10 concurrent readers
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(reader) for _ in range(10)]
        for f in futures:
            f.result()

    assert len(errors) == 0, f"Errors: {errors}"
    assert all(r == 100 for r in results)


def test_from_ids_during_sequential_operations():
    """Test from_ids between other operations (not truly concurrent due to SQLite locks)."""
    db = SQLerDB.in_memory(shared=True)
    CruelUser.set_db(db)

    # Create initial users
    initial_ids = []
    for i in range(20):
        u = CruelUser(name=f"Initial{i}", score=i).save()
        initial_ids.append(u._id)

    # Insert more users
    for i in range(20):
        CruelUser(name=f"New{i}", score=i).save()

    # from_ids should still work correctly for initial set
    hydrated = CruelUser.from_ids(initial_ids)
    assert len(hydrated) == 20
    assert all(h.name.startswith("Initial") for h in hydrated)

    # Total count should be 40
    assert CruelUser.count() == 40


def test_count_after_bulk_modifications():
    """Test count after a series of modifications."""
    db = SQLerDB.in_memory(shared=True)
    CruelUser.set_db(db)

    # Start with 50 users
    users = [CruelUser(name=f"User{i}", score=i).save() for i in range(50)]
    assert CruelUser.count() == 50

    # Delete 25
    for u in users[:25]:
        u.delete()
    assert CruelUser.count() == 25

    # Add 25 new
    for i in range(25):
        CruelUser(name=f"New{i}", score=i).save()

    # Final count should be 50 (25 original + 25 new)
    assert CruelUser.count() == 50


# --- Stress Tests ---


def test_from_ids_very_large_documents():
    """Test from_ids with documents containing large data."""
    db = SQLerDB.in_memory(shared=False)

    class LargeDoc(SQLerModel):
        name: str
        data: str

    LargeDoc.set_db(db)

    # Create docs with 10KB of data each
    ids = []
    for i in range(50):
        large_data = "x" * 10000
        doc = LargeDoc(name=f"Doc{i}", data=large_data).save()
        ids.append(doc._id)

    # Fetch all at once
    hydrated = LargeDoc.from_ids(ids)
    assert len(hydrated) == 50
    assert all(len(h.data) == 10000 for h in hydrated)


def test_from_ids_single_id():
    """Test from_ids with single ID (edge of batch)."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    u = CruelUser(name="Solo", score=42).save()

    hydrated = CruelUser.from_ids([u._id])
    assert len(hydrated) == 1
    assert hydrated[0].name == "Solo"


def test_find_documents_single_id():
    """Test find_documents with single ID."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    u = CruelUser(name="Solo", score=42).save()

    docs = db.find_documents("cruelusers", [u._id])
    assert len(docs) == 1
    assert docs[0]["name"] == "Solo"


def test_from_ids_preserves_instances_independence():
    """Test that from_ids returns independent instances."""
    db = SQLerDB.in_memory(shared=False)
    CruelUser.set_db(db)

    u = CruelUser(name="Original", score=0).save()

    # Get two hydrations of same ID
    hydrated = CruelUser.from_ids([u._id, u._id])
    assert len(hydrated) == 2

    # Modify one
    hydrated[0].name = "Modified"

    # Other should be unaffected (separate instances)
    assert hydrated[1].name == "Original"
