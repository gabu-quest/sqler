"""Tests for new features: export/import, caching, tracking, FTS."""

import csv
import json
import os
import tempfile

import pytest
from sqler import (
    ChangeTracker,
    DiffMixin,
    ExportResult,
    FTSIndex,
    ImportResult,
    QueryCache,
    SearchableMixin,
    SearchResult,
    SQLerDB,
    SQLerModel,
    TrackedModel,
    cached_query,
    export_csv,
    export_csv_string,
    export_json,
    export_json_string,
    export_jsonl,
    import_csv,
    import_json,
    import_jsonl,
    stream_jsonl,
)

# =============================================================================
# Export/Import Tests
# =============================================================================


class TestExportCSV:
    """Tests for CSV export functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class User(SQLerModel):
            name: str
            email: str
            age: int

        User.set_db(self.db)
        self.User = User

        # Create test data
        User(name="Alice", email="alice@example.com", age=30).save()
        User(name="Bob", email="bob@example.com", age=25).save()
        User(name="Charlie", email="charlie@example.com", age=35).save()

    def test_export_csv_file(self):
        """Export to CSV file works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "users.csv")
            result = export_csv(self.User, path)

            assert isinstance(result, ExportResult)
            assert result.format == "csv"
            assert result.count == 3
            assert result.size_bytes > 0
            assert os.path.exists(path)

            # Verify content
            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 3
                assert rows[0]["name"] == "Alice"

    def test_export_csv_string(self):
        """Export to CSV string works."""
        csv_str = export_csv_string(self.User)

        assert "name,email,age" in csv_str or "_id,name,email,age" in csv_str
        assert "Alice" in csv_str
        assert "Bob" in csv_str

    def test_export_csv_escapes_formula_injection(self):
        """Values starting with =, +, -, @ must be escaped to neutralize spreadsheet formula injection.

        Without this escape, a stored value like ``=HYPERLINK("http://attacker", "click")``
        would be interpreted as a formula when the CSV is opened in Excel/Sheets.
        """
        # Insert a payload that would otherwise be interpreted as a formula
        evil_user = self.User(
            name='=HYPERLINK("http://attacker.com","click")',
            email="@SUM(A1:A10)",
            age=42,
        )
        evil_user.save()
        plus_user = self.User(name="+1234567", email="-CMD|'/c calc'!A0", age=21)
        plus_user.save()

        csv_str = export_csv_string(self.User)

        # The original payloads must NOT appear unprefixed (they'd run as formulas)
        assert '=HYPERLINK' not in csv_str.replace("'=HYPERLINK", "")
        assert "@SUM" not in csv_str.replace("'@SUM", "")
        # Each payload must appear with a leading single-quote (the spreadsheet escape)
        assert "'=HYPERLINK" in csv_str
        assert "'@SUM" in csv_str
        assert "'+1234567" in csv_str
        assert "'-CMD" in csv_str

    def test_export_csv_specific_fields(self):
        """Export specific fields only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "users.csv")
            result = export_csv(self.User, path, fields=["name", "age"], include_id=False)

            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert "email" not in rows[0]
                assert "name" in rows[0]
                assert "age" in rows[0]


class TestExportJSON:
    """Tests for JSON export functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Product(SQLerModel):
            name: str
            price: float
            tags: list[str]

        Product.set_db(self.db)
        self.Product = Product

        Product(name="Widget", price=9.99, tags=["small", "blue"]).save()
        Product(name="Gadget", price=19.99, tags=["large", "red"]).save()

    def test_export_json_file(self):
        """Export to JSON file works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "products.json")
            result = export_json(self.Product, path, indent=2)

            assert result.format == "json"
            assert result.count == 2

            with open(path) as f:
                data = json.load(f)
                assert len(data) == 2
                assert data[0]["name"] == "Widget"

    def test_export_json_string(self):
        """Export to JSON string works."""
        json_str = export_json_string(self.Product)
        data = json.loads(json_str)

        assert len(data) == 2
        assert data[0]["tags"] == ["small", "blue"]


class TestExportJSONL:
    """Tests for JSONL export functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Event(SQLerModel):
            type: str
            data: dict

        Event.set_db(self.db)
        self.Event = Event

        Event(type="click", data={"x": 100, "y": 200}).save()
        Event(type="scroll", data={"delta": 50}).save()

    def test_export_jsonl_file(self):
        """Export to JSONL file works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "events.jsonl")
            result = export_jsonl(self.Event, path)

            assert result.format == "jsonl"
            assert result.count == 2

            with open(path) as f:
                lines = f.readlines()
                assert len(lines) == 2
                assert json.loads(lines[0])["type"] == "click"

    def test_stream_jsonl(self):
        """Stream JSONL records works."""
        records = list(stream_jsonl(self.Event))
        assert len(records) == 2
        assert json.loads(records[0])["type"] == "click"


class TestImportCSV:
    """Tests for CSV import functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Person(SQLerModel):
            name: str
            age: int

        Person.set_db(self.db)
        self.Person = Person

    def test_import_csv(self):
        """Import from CSV file works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "people.csv")

            # Create CSV file
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["name", "age"])
                writer.writeheader()
                writer.writerow({"name": "Alice", "age": "30"})
                writer.writerow({"name": "Bob", "age": "25"})

            result = import_csv(self.Person, path)

            assert isinstance(result, ImportResult)
            assert result.count == 2
            assert result.succeeded == 2
            assert result.failed == 0

            # Verify data
            people = self.Person.query().all()
            assert len(people) == 2

    def test_import_csv_with_transform(self):
        """Import with transform function works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "people.csv")

            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["name", "age"])
                writer.writeheader()
                writer.writerow({"name": "alice", "age": "30"})

            def transform(row):
                row["name"] = row["name"].title()
                return row

            result = import_csv(self.Person, path, transform=transform)
            person = self.Person.query().first()
            assert person.name == "Alice"


class TestImportJSON:
    """Tests for JSON import functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Item(SQLerModel):
            name: str
            quantity: int

        Item.set_db(self.db)
        self.Item = Item

    def test_import_json(self):
        """Import from JSON file works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "items.json")

            with open(path, "w") as f:
                json.dump(
                    [
                        {"name": "Apples", "quantity": 10},
                        {"name": "Oranges", "quantity": 5},
                    ],
                    f,
                )

            result = import_json(self.Item, path)

            assert result.count == 2
            assert result.succeeded == 2

            items = self.Item.query().all()
            assert len(items) == 2


class TestImportJSONL:
    """Tests for JSONL import functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Log(SQLerModel):
            level: str
            message: str

        Log.set_db(self.db)
        self.Log = Log

    def test_import_jsonl(self):
        """Import from JSONL file works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "logs.jsonl")

            with open(path, "w") as f:
                f.write(json.dumps({"level": "INFO", "message": "Started"}) + "\n")
                f.write(json.dumps({"level": "ERROR", "message": "Failed"}) + "\n")

            result = import_jsonl(self.Log, path)

            assert result.count == 2
            assert result.succeeded == 2

            logs = self.Log.query().all()
            assert len(logs) == 2


# =============================================================================
# Cache Tests
# =============================================================================


class TestQueryCache:
    """Tests for QueryCache functionality."""

    def test_cache_set_and_get(self):
        """Basic set and get works."""
        cache = QueryCache(max_size=100)

        cache.set("key1", {"data": "value"})
        result = cache.get("key1")

        assert result == {"data": "value"}

    def test_cache_ttl_expiration(self):
        """TTL expiration works."""
        cache = QueryCache(max_size=100, default_ttl_seconds=0.1)

        cache.set("key1", "value")
        assert cache.has("key1")

        import time

        time.sleep(0.15)

        assert not cache.has("key1")
        assert cache.get("key1") is None

    def test_cache_lru_eviction(self):
        """LRU eviction works when max size reached."""
        cache = QueryCache(max_size=3)

        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)

        # Access 'a' to make it recently used
        cache.get("a")

        # Add new item, should evict 'b' (least recently used)
        cache.set("d", 4)

        assert cache.has("a")
        assert not cache.has("b")
        assert cache.has("c")
        assert cache.has("d")

    def test_cache_invalidate_key(self):
        """Invalidate specific key works."""
        cache = QueryCache(max_size=100)

        cache.set("key1", "value1")
        cache.set("key2", "value2")

        cache.invalidate("key1")

        assert not cache.has("key1")
        assert cache.has("key2")

    def test_cache_invalidate_pattern(self):
        """Pattern invalidation works."""
        cache = QueryCache(max_size=100)

        cache.set("users:1", "user1")
        cache.set("users:2", "user2")
        cache.set("products:1", "product1")

        count = cache.invalidate_pattern("users:*")

        assert count == 2
        assert not cache.has("users:1")
        assert not cache.has("users:2")
        assert cache.has("products:1")

    def test_cache_invalidate_table(self):
        """Table invalidation works."""
        cache = QueryCache(max_size=100)

        cache.set("key1", "value1", table="users")
        cache.set("key2", "value2", table="users")
        cache.set("key3", "value3", table="products")

        count = cache.invalidate_table("users")

        assert count == 2
        assert not cache.has("key1")
        assert cache.has("key3")

    def test_cache_stats(self):
        """Cache statistics are tracked."""
        cache = QueryCache(max_size=100)

        cache.set("key1", "value1")
        cache.get("key1")  # Hit
        cache.get("key2")  # Miss

        stats = cache.stats

        assert stats.hits == 1
        assert stats.misses == 1
        assert stats.hit_rate == 0.5

    def test_cache_clear(self):
        """Clear all entries works."""
        cache = QueryCache(max_size=100)

        cache.set("a", 1)
        cache.set("b", 2)

        count = cache.clear()

        assert count == 2
        assert len(cache) == 0


class TestCachedQueryDecorator:
    """Tests for cached_query decorator."""

    def test_cached_query_decorator(self):
        """Decorator caches function results."""
        call_count = 0

        @cached_query(ttl_seconds=60)
        def expensive_function():
            nonlocal call_count
            call_count += 1
            return {"result": "data"}

        # First call executes function
        result1 = expensive_function()
        assert call_count == 1

        # Second call returns cached result
        result2 = expensive_function()
        assert call_count == 1  # Not incremented
        assert result1 == result2


# =============================================================================
# Change Tracking Tests
# =============================================================================


class TestChangeTracker:
    """Tests for ChangeTracker class."""

    def test_tracker_snapshot(self):
        """Snapshot captures initial state."""
        tracker = ChangeTracker()
        tracker.snapshot({"name": "Alice", "age": 30})

        assert tracker._original == {"name": "Alice", "age": 30}

    def test_tracker_detect_changes(self):
        """Detects changed fields."""
        tracker = ChangeTracker()
        tracker.snapshot({"name": "Alice", "age": 30})

        changed = tracker.get_changed_fields({"name": "Bob", "age": 30})

        assert changed == {"name"}

    def test_tracker_get_changes(self):
        """Gets old and new values for changes."""
        tracker = ChangeTracker()
        tracker.snapshot({"name": "Alice", "age": 30})

        changes = tracker.get_changes({"name": "Bob", "age": 31})

        assert changes == {
            "name": ("Alice", "Bob"),
            "age": (30, 31),
        }


class TestTrackedModel:
    """Tests for TrackedModel mixin."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class User(TrackedModel, SQLerModel):
            name: str
            email: str
            age: int

        User.set_db(self.db)
        self.User = User

    def test_model_is_dirty_after_change(self):
        """Model is dirty after field change."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()
        user.mark_clean()

        user.name = "Bob"

        assert user.is_dirty
        assert "name" in user.changed_fields

    def test_model_not_dirty_after_save(self):
        """Model is clean after save."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()

        assert not user.is_dirty

    def test_get_changes(self):
        """get_changes returns old and new values."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()
        user.mark_clean()

        user.name = "Bob"
        user.age = 31

        changes = user.get_changes()

        assert changes["name"] == ("Alice", "Bob")
        assert changes["age"] == (30, 31)

    def test_revert_changes(self):
        """revert_changes restores original values."""
        user = self.User(name="Alice", email="alice@test.com", age=30)
        user.save()
        user.mark_clean()

        user.name = "Bob"
        user.revert_changes()

        assert user.name == "Alice"
        assert not user.is_dirty


class TestDiffMixin:
    """Tests for DiffMixin."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Item(DiffMixin, SQLerModel):
            name: str
            quantity: int

        Item.set_db(self.db)
        self.Item = Item

    def test_diff_detects_differences(self):
        """diff() detects field differences."""
        item1 = self.Item(name="Apple", quantity=10)
        item2 = self.Item(name="Apple", quantity=15)

        diff = item1.diff(item2)

        assert diff == {"quantity": (10, 15)}

    def test_is_equal(self):
        """is_equal() compares field values."""
        item1 = self.Item(name="Apple", quantity=10)
        item2 = self.Item(name="Apple", quantity=10)
        item3 = self.Item(name="Orange", quantity=10)

        assert item1.is_equal(item2)
        assert not item1.is_equal(item3)

    def test_clone(self):
        """clone() creates a copy with overrides."""
        item = self.Item(name="Apple", quantity=10)
        item.save()

        cloned = item.clone(quantity=20)

        assert cloned.name == "Apple"
        assert cloned.quantity == 20
        assert cloned._id is None  # Clone has no _id


# =============================================================================
# FTS Tests
# =============================================================================


class TestFTSIndex:
    """Tests for FTSIndex functionality."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Article(SQLerModel):
            title: str
            content: str
            author: str

        Article.set_db(self.db)
        self.Article = Article

        # Create test articles (this creates the table)
        self.Article(
            title="Python Tutorial", content="Learn Python programming from scratch", author="Alice"
        ).save()
        self.Article(
            title="JavaScript Guide",
            content="Modern JavaScript development techniques",
            author="Bob",
        ).save()
        self.Article(
            title="Advanced Python",
            content="Deep dive into Python internals and performance",
            author="Alice",
        ).save()

    def test_fts_index_creation(self):
        """FTS index can be created."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)

        # Rebuild after table exists
        count = fts.rebuild()
        assert count == 3

    def test_fts_search_basic(self):
        """Basic search returns matching documents."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        results = fts.search("Python")

        assert len(results) == 2
        titles = [r.title for r in results]
        assert "Python Tutorial" in titles
        assert "Advanced Python" in titles

    def test_fts_search_phrase(self):
        """Phrase search works."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        results = fts.search('"Python programming"')

        assert len(results) == 1
        assert results[0].title == "Python Tutorial"

    def test_fts_search_ranked(self):
        """Ranked search returns scores."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        results = fts.search_ranked("Python")

        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert all(r.score != 0 for r in results)

    def test_fts_count(self):
        """Count matching documents."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        count = fts.count("Python")
        assert count == 2

        count = fts.count("JavaScript")
        assert count == 1

    def test_fts_index_single_document(self):
        """Index a single document after creation."""
        fts = FTSIndex(self.Article, fields=["title", "content"])
        fts.create(self.db)
        fts.rebuild()

        # Add new article
        new_article = self.Article(
            title="Rust Programming", content="Systems programming with Rust", author="Charlie"
        )
        new_article.save()
        fts.index(new_article)

        results = fts.search("Rust")
        assert len(results) == 1
        assert results[0].title == "Rust Programming"


class TestSearchableMixin:
    """Tests for SearchableMixin."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

        class Post(SearchableMixin, SQLerModel):
            title: str
            body: str

            class FTS:
                fields = ["title", "body"]

        Post.set_db(self.db)
        self.Post = Post

    def test_searchable_mixin_create_index(self):
        """create_search_index() works."""
        self.Post.create_search_index(self.db)

        # Add posts
        self.Post(title="Hello World", body="This is my first post").save()
        self.Post(title="Python Tips", body="Learn Python effectively").save()

        self.Post.rebuild_search_index()

        results = self.Post.search("Python")
        assert len(results) == 1
        assert results[0].title == "Python Tips"

    def test_searchable_mixin_count(self):
        """search_count() works."""
        self.Post.create_search_index(self.db)
        self.Post(title="Hello", body="World").save()
        self.Post(title="Hello", body="There").save()
        self.Post.rebuild_search_index()

        count = self.Post.search_count("Hello")
        assert count == 2


# =============================================================================
# Integration Tests
# =============================================================================


class TestFeatureIntegration:
    """Integration tests combining multiple features."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = SQLerDB.in_memory()

    def test_export_and_import_roundtrip(self):
        """Export and import roundtrip preserves data."""

        class Product(SQLerModel):
            name: str
            price: float

        Product.set_db(self.db)

        # Create data
        Product(name="Widget", price=9.99).save()
        Product(name="Gadget", price=19.99).save()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Export
            path = os.path.join(tmpdir, "products.json")
            export_json(Product, path)

            # Clear database
            for p in Product.query().all():
                p.delete()
            assert len(Product.query().all()) == 0

            # Import
            result = import_json(Product, path)
            assert result.succeeded == 2

            # Verify
            products = Product.query().all()
            assert len(products) == 2
            names = {p.name for p in products}
            assert names == {"Widget", "Gadget"}

    def test_tracked_model_with_cache(self):
        """TrackedModel works with caching."""

        class Item(TrackedModel, SQLerModel):
            name: str
            count: int

        Item.set_db(self.db)

        cache = QueryCache(max_size=100)

        # Create and cache
        item = Item(name="Test", count=10)
        item.save()
        cache.set("item:1", item)

        # Modify
        item.name = "Modified"
        assert item.is_dirty

        # Cache still has old version (by design)
        cached = cache.get("item:1")
        assert cached.name == "Modified"  # Same object reference
