"""Tests for error conditions and edge cases in SQLer.

This module tests error handling, malformed data, concurrent access,
and other edge cases to ensure robustness.
"""

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel, StaleVersionError
from sqler.models import ReferentialIntegrityError
from sqler.query import SQLerField as F

# --- Table Name Validation Tests ---


def test_invalid_table_name_rejected():
    """Test that invalid table names are rejected."""
    from sqler.exceptions import InvalidTableNameError
    from sqler.utils import validate_table_name

    # Valid names should pass
    assert validate_table_name("users") == "users"
    assert validate_table_name("User") == "User"
    assert validate_table_name("_private") == "_private"
    assert validate_table_name("table_123") == "table_123"

    # Invalid names should raise InvalidTableNameError
    with pytest.raises(InvalidTableNameError):
        validate_table_name("user-table")

    with pytest.raises(InvalidTableNameError):
        validate_table_name("user table")

    with pytest.raises(InvalidTableNameError):
        validate_table_name("123users")

    with pytest.raises(InvalidTableNameError):
        validate_table_name("user;DROP TABLE users--")


def test_sql_injection_prevented():
    """Test that SQL injection attempts are blocked."""
    from sqler.exceptions import InvalidTableNameError

    class BadTable(SQLerModel):
        data: str

    db = SQLerDB.in_memory()

    # Attempt to set a malicious table name
    with pytest.raises(InvalidTableNameError):
        BadTable.set_db(db, table="users; DROP TABLE users--")


# --- Malformed JSON Tests ---


def test_malformed_json_handling():
    """Test that malformed JSON in database raises JSONDecodeError.

    Note: The current implementation does NOT gracefully handle malformed JSON.
    This test documents the current behavior where an exception is raised.
    """
    import json

    class Item(SQLerModel):
        name: str

    db = SQLerDB.in_memory()
    Item.set_db(db)

    # Insert valid item
    item = Item(name="Valid").save()

    # Manually corrupt the JSON data
    db.adapter.execute(
        "UPDATE items SET data = 'invalid json{{{' WHERE _id = ?;",
        [item._id],
    )
    db.adapter.commit()

    # Current implementation raises JSONDecodeError on malformed data
    with pytest.raises(json.JSONDecodeError):
        Item.from_id(item._id)


def test_referential_integrity_with_malformed_json():
    """Test that referential integrity checks skip malformed JSON."""

    class Author(SQLerModel):
        name: str

    class Book(SQLerModel):
        title: str
        author: dict | None = None

    db = SQLerDB.in_memory()
    Author.set_db(db)
    Book.set_db(db)

    author = Author(name="Writer").save()
    book = Book(title="Story", author={"_table": "authors", "_id": author._id}).save()

    # Corrupt the book's JSON
    db.adapter.execute(
        "UPDATE books SET data = 'bad json' WHERE _id = ?;",
        [book._id],
    )
    db.adapter.commit()

    # Should not crash when finding referrers (malformed rows are skipped)
    referrers = author._find_referrers(db, "authors", author._id)
    # The corrupted book should be skipped
    assert all(table != "books" or row_id != book._id for table, row_id, _ in referrers)


# --- Concurrent Update Tests ---


def test_safe_model_concurrent_update_conflict():
    """Test that concurrent updates to safe models detect conflicts."""

    class Account(SQLerSafeModel):
        balance: int

    db = SQLerDB.in_memory()
    Account.set_db(db)

    # Create account
    acc = Account(balance=100).save()
    assert acc._version == 0

    # Simulate concurrent update by manually incrementing version
    db.adapter.execute(
        "UPDATE accounts SET _version = _version + 1 WHERE _id = ?;",
        [acc._id],
    )
    db.adapter.commit()

    # Attempt to save should fail with stale version
    acc.balance = 200
    with pytest.raises(StaleVersionError):
        acc.save()


def test_safe_model_refresh_after_conflict():
    """Test that refresh() works after a version conflict."""

    class Counter(SQLerSafeModel):
        count: int

    db = SQLerDB.in_memory()
    Counter.set_db(db)

    counter = Counter(count=0).save()
    original_version = counter._version

    # Simulate another process updating the counter
    db.adapter.execute(
        "UPDATE counters SET data = json_set(data, '$.count', 10), _version = _version + 1 WHERE _id = ?;",
        [counter._id],
    )
    db.adapter.commit()

    # Refresh to get latest
    counter.refresh()
    assert counter.count == 10
    assert counter._version == original_version + 1


# --- Edge Cases in Referential Integrity ---


def test_circular_reference_cascade_delete():
    """Test cascade delete with circular references."""

    class Node(SQLerModel):
        value: int
        next: dict | None = None

    db = SQLerDB.in_memory()
    Node.set_db(db)

    # Create circular reference: A -> B -> A
    node_a = Node(value=1).save()
    node_b = Node(value=2, next={"_table": "nodes", "_id": node_a._id}).save()

    # Update A to point to B, creating cycle
    node_a.next = {"_table": "nodes", "_id": node_b._id}
    node_a.save()

    # Cascade delete should handle the cycle without infinite loop
    node_a.delete_with_policy(on_delete="cascade")

    # Both should be deleted
    assert Node.from_id(node_a._id) is None
    assert Node.from_id(node_b._id) is None


def test_self_referential_structure():
    """Test self-referential structures (like trees)."""

    class TreeNode(SQLerModel):
        value: str
        parent: dict | None = None
        children: list[dict] | None = None

    db = SQLerDB.in_memory()
    TreeNode.set_db(db)

    # Create tree: root -> child1, child2
    root = TreeNode(value="root", children=[]).save()
    child1 = TreeNode(value="child1", parent={"_table": "treenodes", "_id": root._id}).save()
    child2 = TreeNode(value="child2", parent={"_table": "treenodes", "_id": root._id}).save()

    # Delete root with cascade should delete children
    root.delete_with_policy(on_delete="cascade")

    assert TreeNode.from_id(root._id) is None
    assert TreeNode.from_id(child1._id) is None
    assert TreeNode.from_id(child2._id) is None


def test_multiple_references_to_same_target():
    """Test object with multiple fields referencing same target."""

    class Person(SQLerModel):
        name: str

    class Contract(SQLerModel):
        party_a: dict | None = None
        party_b: dict | None = None
        witness: dict | None = None

    db = SQLerDB.in_memory()
    Person.set_db(db)
    Contract.set_db(db)

    person = Person(name="Alice").save()
    ref = {"_table": "persons", "_id": person._id}

    # Same person in all three roles
    contract = Contract(party_a=ref, party_b=ref, witness=ref).save()

    # Restrict delete should fail
    with pytest.raises(ReferentialIntegrityError):
        person.delete_with_policy(on_delete="restrict")

    # Set null should nullify all three references
    person.delete_with_policy(on_delete="set_null")
    updated = Contract.from_id(contract._id)
    assert updated.party_a is None
    assert updated.party_b is None
    assert updated.witness is None


def test_nested_reference_in_list():
    """Test references nested deeply in lists."""

    class Tag(SQLerModel):
        name: str

    class Article(SQLerModel):
        title: str
        metadata: list[dict] | None = None

    db = SQLerDB.in_memory()
    Tag.set_db(db)
    Article.set_db(db)

    tag = Tag(name="python").save()
    article = Article(
        title="Tutorial",
        metadata=[
            {"type": "tag", "value": {"_table": "tags", "_id": tag._id}},
            {"type": "author", "value": "Alice"},
        ],
    ).save()

    # Should find the nested reference
    referrers = tag._find_referrers(db, "tags", tag._id)
    assert len(referrers) == 1
    assert referrers[0][0] == "articles"
    assert referrers[0][1] == article._id


# --- Query Builder Edge Cases ---


def test_empty_list_for_isin():
    """Test that isin with empty list returns no results."""

    class Item(SQLerModel):
        category: str

    db = SQLerDB.in_memory()
    Item.set_db(db)
    Item(category="books").save()
    Item(category="electronics").save()

    # Empty list should match nothing
    result = Item.query().filter(F("category").isin([])).all()
    assert len(result) == 0


def test_contains_with_non_array_field():
    """Test contains operator on non-array field.

    Note: SQLite's json_each treats scalar values as single-element arrays,
    so contains("value") on a string field matches if the string equals "value".
    This may not be the expected behavior but is how SQLite works.
    """

    class Product(SQLerModel):
        name: str
        category: str

    db = SQLerDB.in_memory()
    Product.set_db(db)
    Product(name="Widget", category="tools").save()

    # contains on string field behaves like equality check in SQLite
    result = Product.query().filter(F("category").contains("tools")).all()
    # SQLite json_each on scalar treats it as single-element, so this matches
    assert len(result) == 1

    # But searching for partial value won't match
    result = Product.query().filter(F("category").contains("tool")).all()
    assert len(result) == 0


def test_query_with_null_values():
    """Test querying fields with null values."""

    class Record(SQLerModel):
        value: int | None = None

    db = SQLerDB.in_memory()
    Record.set_db(db)

    Record(value=10).save()
    Record(value=None).save()
    Record(value=20).save()

    # Query for non-null values
    results = Record.query().filter(F("value") > 5).all()
    assert len(results) == 2


def test_like_pattern_matching():
    """Test LIKE pattern matching in queries."""

    class User(SQLerModel):
        email: str

    db = SQLerDB.in_memory()
    User.set_db(db)

    User(email="alice@example.com").save()
    User(email="bob@example.com").save()
    User(email="alice@test.com").save()

    # Find all @example.com emails
    results = User.query().filter(F("email").like("%@example.com")).all()
    assert len(results) == 2

    # Find all starting with "alice"
    results = User.query().filter(F("email").like("alice%")).all()
    assert len(results) == 2


# --- Registry Edge Cases ---


def test_registry_caching():
    """Test that registry caching works correctly."""
    from sqler import registry

    class TestModel(SQLerModel):
        value: int

    db = SQLerDB.in_memory()
    TestModel.set_db(db)

    # First call should populate cache
    tables1 = registry.get_allowed_tables("testmodels")
    assert "testmodels" in tables1

    # Second call should use cache
    tables2 = registry.get_allowed_tables("testmodels")
    assert tables1 == tables2


# --- Deletion Edge Cases ---


def test_delete_nonexistent_row():
    """Test deleting a row that doesn't exist."""

    class Item(SQLerModel):
        name: str

    db = SQLerDB.in_memory()
    Item.set_db(db)

    item = Item(name="Test")
    # Not saved, so _id is None
    with pytest.raises(ValueError, match="Cannot delete unsaved model"):
        item.delete()


def test_delete_already_deleted_row():
    """Test deleting a row that was already deleted."""

    class Item(SQLerModel):
        name: str

    db = SQLerDB.in_memory()
    Item.set_db(db)

    item = Item(name="Test").save()
    item_id = item._id

    # Delete once
    item.delete()
    assert item._id is None

    # from_id should return None
    assert Item.from_id(item_id) is None


# --- Bulk Operations Tests ---


def test_bulk_upsert_with_mixed_operations():
    """Test bulk_upsert with both inserts and updates."""

    class Record(SQLerModel):
        value: int

    db = SQLerDB.in_memory()
    Record.set_db(db)

    # Create some existing records
    r1 = Record(value=100).save()
    r2 = Record(value=200).save()

    # Bulk upsert with mix of new and existing
    rows = [
        {"_id": r1._id, "value": 150},  # Update
        {"value": 300},  # Insert
        {"_id": r2._id, "value": 250},  # Update
        {"value": 400},  # Insert
    ]

    ids = db.bulk_upsert("records", rows)
    assert len(ids) == 4
    assert r1._id in ids
    assert r2._id in ids

    # Verify updates
    assert Record.from_id(r1._id).value == 150
    assert Record.from_id(r2._id).value == 250


# --- Relationship Hydration Edge Cases ---


def test_hydration_with_missing_target():
    """Test relationship hydration when target doesn't exist.

    When the referenced row is deleted, hydration returns None (not the raw dict).
    This is the expected behavior - the reference is "broken" and hydrated as None.
    """

    class Author(SQLerModel):
        name: str

    class Book(SQLerModel):
        title: str
        author: dict | None = None

    db = SQLerDB.in_memory()
    Author.set_db(db)
    Book.set_db(db)

    # Create author and book
    author = Author(name="Writer").save()
    book = Book(title="Story", author={"_table": "authors", "_id": author._id}).save()

    # Delete author directly (bypass referential integrity)
    db.delete_document("authors", author._id)

    # Hydrating book returns None for the missing author reference
    loaded = Book.from_id(book._id)
    # Broken references are hydrated as None, not the raw dict
    assert loaded.author is None


def test_hydration_with_wrong_table():
    """Test hydration with reference to non-existent table."""

    class Item(SQLerModel):
        name: str
        ref: dict | None = None

    db = SQLerDB.in_memory()
    Item.set_db(db)

    # Create item with reference to non-existent table
    item = Item(name="Test", ref={"_table": "nonexistent", "_id": 123}).save()

    # Should return raw dict without hydration
    loaded = Item.from_id(item._id)
    assert isinstance(loaded.ref, dict)
    assert loaded.ref["_table"] == "nonexistent"


def test_pluralization_edge_cases():
    """Test that table name pluralization handles common patterns."""
    from sqler.models.model import _default_table_name, _pluralize

    # Regular plurals
    assert _pluralize("user") == "users"
    assert _pluralize("post") == "posts"

    # Already ends in 's'
    assert _pluralize("status") == "status"
    assert _pluralize("address") == "address"

    # Consonant + y → ies
    assert _pluralize("category") == "categories"
    assert _pluralize("company") == "companies"
    assert _pluralize("city") == "cities"

    # Vowel + y → ys (regular)
    assert _pluralize("key") == "keys"
    assert _pluralize("day") == "days"

    # Words ending in s, x, z, ch, sh → es
    assert _pluralize("box") == "boxes"
    assert _pluralize("match") == "matches"
    assert _pluralize("wish") == "wishes"
    # Note: "quiz" → "quizes" (not "quizzes") - use __tablename__ for irregular cases

    # SQL reserved words get _tbl suffix
    assert _default_table_name("As") == "as_tbl"
    assert _default_table_name("By") == "by_tbl"
    assert _default_table_name("Index") == "index_tbl"
    assert _default_table_name("Table") == "table_tbl"
    # Pluralized forms are generally safe as table names
    assert _default_table_name("Order") == "orders"  # "orders" is fine
    assert _default_table_name("Select") == "selects"  # "selects" is fine


def test_model_uses_correct_pluralization():
    """Test that models get correctly pluralized table names."""

    class Category(SQLerModel):
        name: str

    class Company(SQLerModel):
        name: str

    class Box(SQLerModel):
        size: int

    db = SQLerDB.in_memory()
    Category.set_db(db)
    Company.set_db(db)
    Box.set_db(db)

    assert Category._table == "categories"
    assert Company._table == "companies"
    assert Box._table == "boxes"
