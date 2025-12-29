"""Tests for connection pooling."""

import os
import tempfile
import threading
import time

import pytest

from sqler import (
    ConnectionPool,
    ConnectionPoolExhaustedError,
    PooledSQLerDB,
    PooledSQLiteAdapter,
    PoolStats,
    SQLerModel,
)


class TestConnectionPool:
    """Tests for ConnectionPool class."""

    def test_pool_creation(self):
        """Pool creates with correct settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            assert pool.max_connections == 5
            assert pool.path == db_path

            stats = pool.stats()
            assert stats.max_connections == 5
            assert stats.total_connections >= 1  # Pre-warmed

            pool.close()

    def test_connection_context_manager(self):
        """Connection context manager works correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            with pool.connection() as conn:
                cursor = conn.execute("SELECT 1")
                assert cursor.fetchone()[0] == 1

            pool.close()

    def test_writer_context_manager(self):
        """Writer context manager works correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            with pool.writer() as conn:
                conn.execute("CREATE TABLE test (_id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO test (value) VALUES (?)", ["hello"])
                conn.commit()

            with pool.connection() as conn:
                cursor = conn.execute("SELECT value FROM test")
                assert cursor.fetchone()[0] == "hello"

            pool.close()

    def test_connections_are_reused(self):
        """Connections are returned to pool and reused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=2, min_connections=1)

            with pool.connection() as conn1:
                id1 = id(conn1)

            with pool.connection() as conn2:
                id2 = id(conn2)

            # Same connection should be reused
            assert id1 == id2

            pool.close()

    def test_pool_stats(self):
        """Pool statistics are tracked correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            stats_before = pool.stats()
            initial_checkouts = stats_before.total_checkouts

            with pool.connection() as _:
                pass

            stats_after = pool.stats()
            assert stats_after.total_checkouts == initial_checkouts + 1

            pool.close()

    def test_pool_stats_to_dict(self):
        """PoolStats can be serialized to dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            stats = pool.stats()
            data = stats.to_dict()

            assert "total_connections" in data
            assert "max_connections" in data
            assert "created_at" in data

            pool.close()

    def test_pool_exhaustion_timeout(self):
        """Pool raises error when exhausted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=1, timeout_ms=100)

            with pool.connection() as _:
                # Pool is exhausted (only 1 connection, in use)
                with pytest.raises(ConnectionPoolExhaustedError):
                    with pool.connection() as _:
                        pass

            pool.close()

    def test_concurrent_reads(self):
        """Multiple concurrent reads work correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            # Create test table
            with pool.writer() as conn:
                conn.execute("CREATE TABLE test (_id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO test (value) VALUES (?)", ["hello"])
                conn.commit()

            results = []
            errors = []

            def read_data():
                try:
                    with pool.connection() as conn:
                        cursor = conn.execute("SELECT value FROM test")
                        results.append(cursor.fetchone()[0])
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=read_data) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0
            assert len(results) == 5
            assert all(r == "hello" for r in results)

            pool.close()

    def test_pool_context_manager(self):
        """Pool can be used as context manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")

            with ConnectionPool(db_path, max_connections=5) as pool:
                with pool.connection() as conn:
                    cursor = conn.execute("SELECT 1")
                    assert cursor.fetchone()[0] == 1

            # Pool should be closed now


class TestPooledSQLiteAdapter:
    """Tests for PooledSQLiteAdapter."""

    def test_adapter_creation(self):
        """Adapter creates correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            adapter = PooledSQLiteAdapter.on_disk(db_path, max_connections=5)

            adapter.connect()
            cursor = adapter.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1

            adapter.close()

    def test_adapter_transactions(self):
        """Adapter transactions work correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            adapter = PooledSQLiteAdapter.on_disk(db_path, max_connections=5)
            adapter.connect()

            adapter.execute("CREATE TABLE test (_id INTEGER PRIMARY KEY, value TEXT)")
            adapter.auto_commit()

            # Begin transaction
            adapter.begin_transaction()
            assert adapter.in_transaction

            adapter.execute("INSERT INTO test (value) VALUES (?)", ["test1"])
            adapter.execute("INSERT INTO test (value) VALUES (?)", ["test2"])

            adapter.end_transaction(commit=True)
            assert not adapter.in_transaction

            # Verify data
            cursor = adapter.execute("SELECT COUNT(*) FROM test")
            assert cursor.fetchone()[0] == 2

            adapter.close()

    def test_adapter_transaction_rollback(self):
        """Adapter rollback works correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            adapter = PooledSQLiteAdapter.on_disk(db_path, max_connections=5)
            adapter.connect()

            adapter.execute("CREATE TABLE test (_id INTEGER PRIMARY KEY, value TEXT)")
            adapter.auto_commit()

            adapter.begin_transaction()
            adapter.execute("INSERT INTO test (value) VALUES (?)", ["test1"])
            adapter.end_transaction(commit=False)  # Rollback

            cursor = adapter.execute("SELECT COUNT(*) FROM test")
            assert cursor.fetchone()[0] == 0

            adapter.close()

    def test_adapter_pool_stats(self):
        """Adapter exposes pool stats."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            adapter = PooledSQLiteAdapter.on_disk(db_path, max_connections=5)
            adapter.connect()

            stats = adapter.pool_stats()
            assert isinstance(stats, PoolStats)
            assert stats.max_connections == 5

            adapter.close()


class TestPooledSQLerDB:
    """Tests for PooledSQLerDB."""

    def test_pooled_db_creation(self):
        """PooledSQLerDB creates correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = PooledSQLerDB.on_disk(db_path, max_connections=5)

            stats = db.pool_stats()
            assert stats.max_connections == 5

            db.close()

    def test_pooled_db_with_model(self):
        """PooledSQLerDB works with SQLerModel."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = PooledSQLerDB.on_disk(db_path, max_connections=5)

            class User(SQLerModel):
                name: str
                email: str

            User.set_db(db)

            # Create users
            user1 = User(name="Alice", email="alice@example.com").save()
            user2 = User(name="Bob", email="bob@example.com").save()

            assert user1._id is not None
            assert user2._id is not None

            # Query users
            users = User.query().all()
            assert len(users) == 2

            db.close()

    def test_pooled_db_crud_operations(self):
        """PooledSQLerDB CRUD operations work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = PooledSQLerDB.on_disk(db_path, max_connections=5)

            # Insert
            doc_id = db.insert_document("test", {"name": "Alice"})
            assert doc_id is not None

            # Find
            doc = db.find_document("test", doc_id)
            assert doc is not None
            assert doc["name"] == "Alice"

            # Update
            db.upsert_document("test", doc_id, {"name": "Alice Updated"})
            doc = db.find_document("test", doc_id)
            assert doc["name"] == "Alice Updated"

            # Delete
            db.delete_document("test", doc_id)
            doc = db.find_document("test", doc_id)
            assert doc is None

            db.close()

    def test_pooled_db_indexing(self):
        """PooledSQLerDB index creation works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = PooledSQLerDB.on_disk(db_path, max_connections=5)

            db.insert_document("users", {"name": "Alice", "age": 30})
            db.create_index("users", "name")
            db.create_index("users", "age", unique=False)

            # Verify data was inserted
            doc = db.find_document("users", 1)
            assert doc is not None
            assert doc["name"] == "Alice"

            db.close()

    def test_pooled_db_transaction_context(self):
        """PooledSQLerDB transaction context manager works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = PooledSQLerDB.on_disk(db_path, max_connections=5)

            with db:
                db.insert_document("test", {"value": 1})
                db.insert_document("test", {"value": 2})

            # Verify both documents exist
            doc1 = db.find_document("test", 1)
            doc2 = db.find_document("test", 2)
            assert doc1 is not None
            assert doc2 is not None

            db.close()

    def test_pooled_db_concurrent_model_operations(self):
        """PooledSQLerDB handles concurrent model operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = PooledSQLerDB.on_disk(db_path, max_connections=5)

            class Item(SQLerModel):
                value: int

            Item.set_db(db)

            # Pre-create items sequentially to avoid race conditions on table creation
            for i in range(10):
                Item(value=i).save()

            # Concurrent reads
            results = []
            errors = []

            def read_items():
                try:
                    items = Item.query().all()
                    results.append(len(items))
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=read_items) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0
            assert all(r == 10 for r in results)

            db.close()


class TestPoolWALMode:
    """Tests specific to WAL mode behavior."""

    def test_wal_mode_enabled(self):
        """WAL mode is enabled by default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            with pool.connection() as conn:
                cursor = conn.execute("PRAGMA journal_mode")
                mode = cursor.fetchone()[0]
                assert mode.lower() == "wal"

            pool.close()

    def test_reader_doesnt_block_writer(self):
        """Readers don't block writers in WAL mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            pool = ConnectionPool(db_path, max_connections=5)

            # Create table
            with pool.writer() as conn:
                conn.execute("CREATE TABLE test (_id INTEGER PRIMARY KEY, value TEXT)")
                conn.commit()

            read_started = threading.Event()
            write_done = threading.Event()

            def long_read():
                with pool.connection() as conn:
                    conn.execute("SELECT * FROM test")
                    read_started.set()
                    time.sleep(0.1)  # Simulate long read

            def write():
                read_started.wait()  # Wait for read to start
                with pool.writer() as conn:
                    conn.execute("INSERT INTO test (value) VALUES (?)", ["test"])
                    conn.commit()
                write_done.set()

            t1 = threading.Thread(target=long_read)
            t2 = threading.Thread(target=write)

            t1.start()
            t2.start()

            # Write should complete even while read is in progress
            write_done.wait(timeout=1.0)
            assert write_done.is_set()

            t1.join()
            t2.join()

            pool.close()
