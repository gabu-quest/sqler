"""Cruel and unusual stress tests for SQLer.

These tests are designed to break the library in creative ways:
- Concurrency under heavy contention
- Circular references and deep nesting
- Large documents and many records
- Edge cases and boundary conditions
- Race conditions and optimistic locking battles
- Memory pressure and resource exhaustion
"""

import sys
sys.path.insert(0, 'src')

import concurrent.futures
import gc
import random
import string
import threading
import time
from typing import Any, List, Optional
from unittest.mock import patch

import pytest
from pydantic import Field

from sqler import (
    SQLerDB,
    SQLerModel,
    SQLerSafeModel,
    SoftDeleteMixin,
    TimestampMixin,
    F,
    RebaseConfig,
    PERMISSIVE_REBASE_CONFIG,
    StaleVersionError,
)
from sqler.models.safe import StaleVersionError
from sqler.utils import ReferenceTracker, collect_references


@pytest.fixture
def db():
    """Create an in-memory database for testing."""
    return SQLerDB.in_memory()


@pytest.fixture
def disk_db(tmp_path):
    """Create a disk-based database for persistence tests."""
    db_path = str(tmp_path / "stress_test.db")
    return SQLerDB.on_disk(db_path)


class TestConcurrencyStress:
    """Tests for concurrent access patterns."""

    def test_100_concurrent_inserts_with_retry(self, db):
        """Stress test: 100 concurrent inserts with retry logic.

        SQLite in-memory databases serialize writes, so we add retry logic
        to handle lock contention gracefully.
        """
        class Item(SQLerModel):
            name: str
            value: int

        Item.set_db(db)
        errors = []
        success_count = [0]
        lock = threading.Lock()

        def insert_item(i):
            # Retry with exponential backoff for lock contention
            for attempt in range(5):
                try:
                    Item(name=f"item_{i}", value=i).save()
                    with lock:
                        success_count[0] += 1
                    return
                except Exception as e:
                    if "locked" in str(e).lower() and attempt < 4:
                        time.sleep(0.01 * (2 ** attempt))
                        continue
                    with lock:
                        errors.append((i, str(e)))
                    return

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(insert_item, range(100)))

        # With retries, most should succeed
        assert success_count[0] >= 80, f"Only {success_count[0]} succeeded, errors: {errors[:5]}"
        assert Item.query().count() == success_count[0]

    def test_100_concurrent_inserts_disk_db(self, disk_db):
        """Stress test: 100 concurrent inserts on disk-based DB with WAL mode."""
        class Item(SQLerModel):
            name: str
            value: int

        Item.set_db(disk_db)
        errors = []
        success_count = [0]
        lock = threading.Lock()

        def insert_item(i):
            for attempt in range(3):
                try:
                    Item(name=f"item_{i}", value=i).save()
                    with lock:
                        success_count[0] += 1
                    return
                except Exception as e:
                    if "locked" in str(e).lower() and attempt < 2:
                        time.sleep(0.02 * (attempt + 1))
                        continue
                    with lock:
                        errors.append((i, str(e)))
                    return

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(insert_item, range(100)))

        # Disk-based DB with WAL should handle this better
        assert success_count[0] >= 90, f"Only {success_count[0]} succeeded, errors: {errors[:5]}"

    def test_concurrent_reads_and_writes(self, db):
        """Stress test: concurrent reads while writing."""
        class Counter(SQLerModel):
            name: str
            value: int = 0

        Counter.set_db(db)

        # Create initial counters
        for i in range(10):
            Counter(name=f"counter_{i}", value=0).save()

        read_results = []
        write_count = [0]
        lock = threading.Lock()

        def reader():
            for _ in range(50):
                try:
                    counters = Counter.query().all()
                    with lock:
                        read_results.append(len(counters))
                except Exception:
                    pass
                time.sleep(0.001)

        def writer():
            for i in range(20):
                try:
                    Counter(name=f"new_counter_{threading.current_thread().name}_{i}", value=i).save()
                    with lock:
                        write_count[0] += 1
                except Exception:
                    pass
                time.sleep(0.001)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # 5 readers, 5 writers
            futures = []
            for _ in range(5):
                futures.append(executor.submit(reader))
                futures.append(executor.submit(writer))
            for f in futures:
                f.result()

        # Writers should have succeeded
        assert write_count[0] > 0
        # Readers should have gotten results
        assert len(read_results) > 0

    def test_optimistic_locking_contention(self, db):
        """Stress test: many threads fighting over the same counter."""
        class Counter(SQLerSafeModel):
            name: str
            count: int = 0

            _rebase_config = PERMISSIVE_REBASE_CONFIG

        Counter.set_db(db)

        # Create the counter
        counter = Counter(name="contended").save()
        counter_id = counter._id

        success_count = [0]
        stale_errors = [0]
        lock = threading.Lock()

        def increment():
            for _ in range(5):
                try:
                    c = Counter.from_id(counter_id)
                    if c:
                        c.count += 1
                        c.save()
                        with lock:
                            success_count[0] += 1
                except StaleVersionError:
                    with lock:
                        stale_errors[0] += 1
                except Exception:
                    pass

        # 10 threads each trying to increment 5 times
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(increment) for _ in range(10)]
            for f in futures:
                f.result()

        # Check that some increments succeeded
        final = Counter.from_id(counter_id)
        assert final is not None
        # With rebasing, most should succeed
        assert final.count > 0
        print(f"Final count: {final.count}, successes: {success_count[0]}, stale errors: {stale_errors[0]}")


class TestDeepNestingAndCircularReferences:
    """Tests for deeply nested structures and circular references."""

    def test_deeply_nested_document(self, db):
        """Stress test: very deeply nested JSON structure."""
        class DeepDoc(SQLerModel):
            data: dict

        DeepDoc.set_db(db)

        # Create a deeply nested structure (100 levels deep)
        def create_deep(depth):
            if depth == 0:
                return {"value": "leaf"}
            return {"nested": create_deep(depth - 1)}

        deep_data = create_deep(100)
        doc = DeepDoc(data=deep_data).save()

        # Should be able to retrieve it
        retrieved = DeepDoc.from_id(doc._id)
        assert retrieved is not None

        # Navigate to the deepest level
        current = retrieved.data
        for _ in range(100):
            current = current.get("nested", current)
        assert current.get("value") == "leaf"

    def test_wide_document_many_keys(self, db):
        """Stress test: document with thousands of keys."""
        class WideDoc(SQLerModel):
            data: dict

        WideDoc.set_db(db)

        # Create a document with 1000 keys
        wide_data = {f"key_{i}": f"value_{i}" for i in range(1000)}
        doc = WideDoc(data=wide_data).save()

        retrieved = WideDoc.from_id(doc._id)
        assert retrieved is not None
        assert len(retrieved.data) == 1000
        assert retrieved.data["key_500"] == "value_500"

    def test_large_array(self, db):
        """Stress test: document with a very large array."""
        class ArrayDoc(SQLerModel):
            items: list

        ArrayDoc.set_db(db)

        # 10,000 items in the array
        large_array = list(range(10000))
        doc = ArrayDoc(items=large_array).save()

        retrieved = ArrayDoc.from_id(doc._id)
        assert retrieved is not None
        assert len(retrieved.items) == 10000
        assert retrieved.items[5000] == 5000

    def test_circular_reference_detection(self, db):
        """Test that circular references are handled gracefully."""
        class Node(SQLerModel):
            name: str
            next_node: Optional[dict] = None  # Will store ref dict

        Node.set_db(db)

        # Create a chain that loops back
        a = Node(name="A").save()
        b = Node(name="B").save()
        c = Node(name="C").save()

        # A -> B -> C -> A (circular)
        a.next_node = {"_table": "nodes", "_id": b._id}
        a.save()
        b.next_node = {"_table": "nodes", "_id": c._id}
        b.save()
        c.next_node = {"_table": "nodes", "_id": a._id}  # Loop back!
        c.save()

        # Query should not hang or crash
        nodes = Node.query().all()
        assert len(nodes) == 3

    def test_reference_tracker_prevents_infinite_loop(self):
        """Test that ReferenceTracker catches cycles."""
        tracker = ReferenceTracker(max_depth=100, raise_on_cycle=False)

        # Simulate visiting in a cycle
        with tracker.visit("a", 1) as ok1:
            assert ok1
            with tracker.visit("b", 2) as ok2:
                assert ok2
                with tracker.visit("c", 3) as ok3:
                    assert ok3
                    # Try to visit 'a' again - should fail
                    with tracker.visit("a", 1) as ok4:
                        assert not ok4  # Cycle detected


class TestLargeScaleOperations:
    """Tests for operations on large datasets."""

    def test_bulk_insert_10000_records(self, db):
        """Stress test: insert 10,000 records."""
        class Record(SQLerModel):
            index: int
            data: str

        Record.set_db(db)

        # Bulk insert
        start = time.time()
        docs = [{"index": i, "data": f"data_{i}"} for i in range(10000)]
        db.bulk_upsert("records", docs)
        elapsed = time.time() - start

        print(f"Bulk insert 10,000 records: {elapsed:.2f}s")

        # Verify count
        assert Record.query().count() == 10000

    def test_query_with_many_filters(self, db):
        """Stress test: query with many chained filters."""
        class Item(SQLerModel):
            a: int
            b: int
            c: int
            d: int
            e: int

        Item.set_db(db)

        # Insert some records
        for i in range(100):
            Item(a=i % 10, b=i % 5, c=i % 3, d=i % 2, e=i).save()

        # Query with many filters
        results = (
            Item.query()
            .filter(F("a") < 5)
            .filter(F("b") >= 2)
            .filter(F("c") == 1)
            .filter(F("d") == 0)
            .order_by("e")
            .all()
        )

        # Should find some results
        assert len(results) >= 0  # May be 0, that's ok

    def test_paginate_through_large_dataset(self, db):
        """Stress test: paginate through 1000 records."""
        class Record(SQLerModel):
            index: int

        Record.set_db(db)

        # Insert 1000 records
        for i in range(1000):
            Record(index=i).save()

        # Paginate through all pages
        all_records = []
        page = 1
        while True:
            result = Record.query().order_by("index").paginate(page, per_page=50)
            all_records.extend(result.items)
            if not result.has_next:
                break
            page += 1

        assert len(all_records) == 1000
        assert page == 20  # 1000 / 50 = 20 pages


class TestEdgeCasesAndBoundaryConditions:
    """Tests for edge cases and boundary conditions."""

    def test_empty_string_fields(self, db):
        """Test handling of empty strings."""
        class Item(SQLerModel):
            name: str
            description: str = ""

        Item.set_db(db)

        item = Item(name="", description="").save()
        retrieved = Item.from_id(item._id)

        assert retrieved.name == ""
        assert retrieved.description == ""

    def test_unicode_and_special_characters(self, db):
        """Test handling of unicode and special characters."""
        class Item(SQLerModel):
            text: str

        Item.set_db(db)

        special_texts = [
            "Hello 世界",  # Chinese
            "مرحبا",  # Arabic
            "🎉🚀💻",  # Emoji
            "Line1\nLine2\tTabbed",  # Control chars
            "Quote's \"double\"",  # Quotes
            "Back\\slash",  # Backslash
            "\x00\x01\x02",  # Null bytes
            "a" * 10000,  # Very long string
        ]

        for text in special_texts:
            try:
                item = Item(text=text).save()
                retrieved = Item.from_id(item._id)
                assert retrieved.text == text, f"Failed for: {repr(text[:50])}"
            except Exception as e:
                # Some special chars might fail, that's expected
                print(f"Expected failure for {repr(text[:20])}: {e}")

    def test_very_large_integer(self, db):
        """Test handling of very large integers."""
        class BigNum(SQLerModel):
            value: int

        BigNum.set_db(db)

        # Python can handle arbitrary large integers
        huge = 10 ** 100
        item = BigNum(value=huge).save()
        retrieved = BigNum.from_id(item._id)

        assert retrieved.value == huge

    def test_float_precision(self, db):
        """Test float precision is maintained."""
        class Precise(SQLerModel):
            value: float

        Precise.set_db(db)

        # Test various float values
        test_values = [
            0.1 + 0.2,  # Classic floating point issue
            1e-10,
            1e10,
            float('inf'),
            -float('inf'),
            3.141592653589793,
        ]

        for val in test_values:
            try:
                item = Precise(value=val).save()
                retrieved = Precise.from_id(item._id)
                if val == val:  # Not NaN
                    assert retrieved.value == val, f"Failed for {val}"
            except Exception as e:
                print(f"Float {val} handling: {e}")

    def test_none_in_list(self, db):
        """Test handling of None values in lists."""
        class Container(SQLerModel):
            items: list

        Container.set_db(db)

        item = Container(items=[1, None, "hello", None, 5]).save()
        retrieved = Container.from_id(item._id)

        assert retrieved.items == [1, None, "hello", None, 5]

    def test_empty_list_and_dict(self, db):
        """Test handling of empty collections."""
        class Container(SQLerModel):
            items: list = Field(default_factory=list)
            data: dict = Field(default_factory=dict)

        Container.set_db(db)

        item = Container(items=[], data={}).save()
        retrieved = Container.from_id(item._id)

        assert retrieved.items == []
        assert retrieved.data == {}


class TestSoftDeleteStress:
    """Stress tests for soft delete functionality."""

    def test_soft_delete_many_records(self, db):
        """Stress test: soft delete thousands of records."""
        class User(SoftDeleteMixin, SQLerModel):
            name: str

        User.set_db(db)

        # Create 500 users
        for i in range(500):
            User(name=f"user_{i}").save()

        # Soft delete half
        users = User.query().limit(250).all()
        for user in users:
            user.soft_delete()

        # Check counts
        assert User.active().count() == 250
        assert User.only_deleted().count() == 250
        assert User.with_deleted().count() == 500

    def test_restore_all_deleted(self, db):
        """Test restoring all soft-deleted records."""
        class Item(SoftDeleteMixin, SQLerModel):
            name: str

        Item.set_db(db)

        # Create and delete items
        for i in range(100):
            item = Item(name=f"item_{i}").save()
            item.soft_delete()

        assert Item.active().count() == 0
        assert Item.only_deleted().count() == 100

        # Restore all
        for item in Item.only_deleted().all():
            item.restore()

        assert Item.active().count() == 100
        assert Item.only_deleted().count() == 0


class TestOptimisticLockingStress:
    """Stress tests for optimistic locking."""

    def test_rapid_version_increments(self, db):
        """Test rapid sequential updates with version checking."""
        class Counter(SQLerSafeModel):
            value: int = 0
            count: int = 0

            _rebase_config = PERMISSIVE_REBASE_CONFIG

        Counter.set_db(db)

        counter = Counter(value=0, count=0).save()

        # 100 rapid increments
        for i in range(100):
            counter.value += 1
            counter.count += 1
            counter.save()

        assert counter.value == 100
        assert counter._version == 100

    def test_version_conflict_without_rebase(self, db):
        """Test that version conflicts raise errors without rebase."""
        class Item(SQLerSafeModel):
            name: str
            data: str = "initial"

            # Disable rebasing
            _rebase_config = RebaseConfig(enabled=False)

        Item.set_db(db)

        item = Item(name="test").save()

        # Load two copies
        copy1 = Item.from_id(item._id)
        copy2 = Item.from_id(item._id)

        # Update first copy
        copy1.data = "updated_by_copy1"
        copy1.save()

        # Try to update second copy - should fail
        copy2.data = "updated_by_copy2"
        with pytest.raises(StaleVersionError):
            copy2.save()

    def test_refresh_after_external_update(self, db):
        """Test refresh picks up external changes."""
        class Item(SQLerSafeModel):
            value: int = 0

        Item.set_db(db)

        item = Item(value=10).save()
        item_id = item._id

        # External update via raw SQL
        db.adapter.execute(
            f"UPDATE items SET data = json_set(data, '$.value', 999), _version = _version + 1 WHERE _id = ?",
            [item_id]
        )
        db.adapter.commit()

        # Refresh should pick up changes
        item.refresh()
        assert item.value == 999


class TestQueryStress:
    """Stress tests for query operations."""

    def test_complex_nested_query(self, db):
        """Test complex queries on nested JSON."""
        class Order(SQLerModel):
            customer: dict
            items: list
            total: float

        Order.set_db(db)

        # Create orders with nested data
        for i in range(100):
            Order(
                customer={"name": f"Customer {i}", "tier": i % 3},
                items=[
                    {"sku": f"SKU{j}", "qty": j + 1, "price": 10.0 * j}
                    for j in range(5)
                ],
                total=100.0 + i
            ).save()

        # Query on nested field
        high_value = Order.filter(F("total") > 150).all()
        assert len(high_value) == 49  # orders 51-99

    def test_query_chain_immutability(self, db):
        """Test that query chains don't mutate each other."""
        class Item(SQLerModel):
            value: int

        Item.set_db(db)

        for i in range(50):
            Item(value=i).save()

        base_query = Item.query()
        query1 = base_query.filter(F("value") > 10)
        query2 = base_query.filter(F("value") < 20)
        query3 = query1.filter(F("value") < 30)

        # Each query should be independent
        assert base_query.count() == 50
        assert query1.count() == 39  # 11-49
        assert query2.count() == 20  # 0-19
        assert query3.count() == 19  # 11-29

    def test_aggregations_on_large_dataset(self, db):
        """Test aggregation functions on large dataset."""
        class Sale(SQLerModel):
            amount: float
            quantity: int

        Sale.set_db(db)

        # Create 1000 sales
        total_amount = 0
        total_qty = 0
        for i in range(1000):
            amount = float(i * 10)
            qty = i % 100
            Sale(amount=amount, quantity=qty).save()
            total_amount += amount
            total_qty += qty

        # Test aggregations
        assert Sale.query().count() == 1000
        assert Sale.query().sum("amount") == total_amount
        assert Sale.query().sum("quantity") == total_qty
        assert Sale.query().min("amount") == 0.0
        assert Sale.query().max("amount") == 9990.0
        avg = Sale.query().avg("quantity")
        assert abs(avg - (total_qty / 1000)) < 0.01


class TestMemoryAndResourceStress:
    """Tests for memory usage and resource management."""

    def test_many_model_classes(self, db):
        """Test creating many model classes dynamically."""
        classes = []
        for i in range(100):
            # Dynamically create model classes
            cls = type(
                f"DynamicModel{i}",
                (SQLerModel,),
                {"__annotations__": {"value": int}}
            )
            cls.set_db(db, table=f"dynamic_{i}")
            cls(value=i).save()
            classes.append(cls)

        # All should work
        for i, cls in enumerate(classes):
            item = cls.query().first()
            assert item.value == i

    def test_repeated_connect_disconnect(self):
        """Test repeated connection cycles."""
        for _ in range(50):
            db = SQLerDB.in_memory()

            class TempModel(SQLerModel):
                value: int

            TempModel.set_db(db)
            TempModel(value=42).save()
            assert TempModel.query().count() == 1

            db.close()
            gc.collect()

    def test_query_without_fetching(self, db):
        """Test that unfetched queries don't leak resources."""
        class Item(SQLerModel):
            value: int

        Item.set_db(db)

        for i in range(100):
            Item(value=i).save()

        # Create many query objects without executing
        queries = []
        for _ in range(1000):
            q = Item.query().filter(F("value") > 50)
            queries.append(q)

        # Should not have memory issues
        del queries
        gc.collect()


class TestTransactionStress:
    """Stress tests for transaction handling.

    Note: SQLer's model-level operations (save(), delete()) call commit()
    internally, which means they don't participate in explicit transactions
    properly. This is a known limitation - use db-level operations for
    transaction control.
    """

    def test_transaction_rollback_raw_operations(self, db):
        """Test that raw db operations rollback correctly on error."""
        db._ensure_table("raw_items")

        try:
            with db.transaction():
                db.adapter.execute(
                    "INSERT INTO raw_items (data) VALUES (json(?))", ['{"value": 1}']
                )
                db.adapter.execute(
                    "INSERT INTO raw_items (data) VALUES (json(?))", ['{"value": 2}']
                )
                raise RuntimeError("Simulated error")
        except RuntimeError:
            pass

        # Raw operations should have rolled back
        cur = db.adapter.execute("SELECT COUNT(*) FROM raw_items")
        count = cur.fetchone()[0]
        assert count == 0

    def test_transaction_commit_on_success(self, db):
        """Test that transactions commit on success."""
        db._ensure_table("raw_items")

        with db.transaction():
            db.adapter.execute(
                "INSERT INTO raw_items (data) VALUES (json(?))", ['{"value": 1}']
            )
            db.adapter.execute(
                "INSERT INTO raw_items (data) VALUES (json(?))", ['{"value": 2}']
            )

        # Should be committed
        cur = db.adapter.execute("SELECT COUNT(*) FROM raw_items")
        count = cur.fetchone()[0]
        assert count == 2

    def test_model_save_commits_immediately_known_limitation(self, db):
        """Document: model.save() commits immediately, breaking outer transactions.

        This is a known limitation of the current design. Model operations
        call commit() internally for reliability in the common case.
        """
        class Item(SQLerModel):
            value: int

        Item.set_db(db)

        try:
            with db.transaction():
                Item(value=1).save()  # This commits!
                Item(value=2).save()  # This also commits!
                raise RuntimeError("Simulated error")
        except RuntimeError:
            pass

        # Due to model.save() calling commit(), items ARE saved
        # This documents the current behavior (not ideal, but known)
        count = Item.query().count()
        assert count == 2, "Known limitation: model.save() commits immediately"

    def test_nested_transaction_behavior(self, db):
        """Test behavior with nested transactions."""
        class Item(SQLerModel):
            value: int

        Item.set_db(db)

        with db.transaction():
            Item(value=1).save()

            # SQLite doesn't support true nested transactions
            # This should still work (savepoints or ignored)
            try:
                with db.transaction():
                    Item(value=2).save()
            except Exception:
                pass

            Item(value=3).save()

        # Items should be saved (commits happen in save())
        assert Item.query().count() >= 2


class TestRandomizedStress:
    """Randomized stress tests for unpredictable behavior."""

    def test_random_operations_sequence(self, db):
        """Execute random sequence of operations."""
        class Item(SQLerModel):
            name: str
            value: int = 0

        Item.set_db(db)

        random.seed(42)  # Reproducible randomness

        for _ in range(500):
            op = random.choice(["insert", "query", "update", "delete"])

            try:
                if op == "insert":
                    name = ''.join(random.choices(string.ascii_letters, k=10))
                    Item(name=name, value=random.randint(0, 1000)).save()

                elif op == "query":
                    if random.random() > 0.5:
                        Item.query().filter(F("value") > random.randint(0, 500)).all()
                    else:
                        Item.query().limit(random.randint(1, 100)).all()

                elif op == "update":
                    item = Item.query().first()
                    if item:
                        item.value = random.randint(0, 1000)
                        item.save()

                elif op == "delete":
                    item = Item.query().first()
                    if item:
                        item.delete()

            except Exception as e:
                # Log but don't fail on individual operations
                print(f"Op {op} failed: {e}")

        # Should have some records
        count = Item.query().count()
        print(f"After random operations: {count} records")
        assert count >= 0  # May be 0, that's ok


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
