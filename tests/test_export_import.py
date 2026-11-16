"""Export to and import from CSV, JSON and JSONL files and strings."""

import csv
import json
import os
import tempfile

import pytest

from sqler import (
    ExportResult,
    ImportResult,
    SQLerDB,
    SQLerModel,
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
from sqler.export import export_jsonl_string


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

        assert csv_str == (
            "_id,name,email,age\r\n"
            "1,Alice,alice@example.com,30\r\n"
            "2,Bob,bob@example.com,25\r\n"
            "3,Charlie,charlie@example.com,35\r\n"
        )

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


class TestExportImportRoundtrip:
    """Export and import combined."""

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


def _export_to_file(fn):
    def run(model, tmp_path, kwargs):
        out = tmp_path / "out.txt"
        res = fn(model, str(out), **kwargs)
        return (res.count, res.format, out.read_text(encoding="utf-8"))

    return run


def _export_to_string(fn):
    return lambda model, tmp_path, kwargs: fn(model, **kwargs)


EXPORTS = {
    "export_csv": _export_to_file(export_csv),
    "export_csv_string": _export_to_string(export_csv_string),
    "export_json": _export_to_file(export_json),
    "export_json_string": _export_to_string(export_json_string),
    "export_jsonl": _export_to_file(export_jsonl),
    "export_jsonl_string": _export_to_string(export_jsonl_string),
    "stream_jsonl": lambda model, tmp_path, kwargs: list(stream_jsonl(model, **kwargs)),
}

EXPORT_KWARGS = {
    "defaults": {},
    "no_id": {"include_id": False},
    "fields_no_id": {"include_id": False, "fields": ["name"]},
    "fields_with_id": {"fields": ["name"]},
}


def _export_tables(db):
    cur = db.adapter.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


@pytest.mark.parametrize("kwargs_name", list(EXPORT_KWARGS))
@pytest.mark.parametrize("name", list(EXPORTS))
def test_export_of_a_dropped_table_gives_what_an_empty_table_gives(name, kwargs_name, tmp_path):
    kwargs = EXPORT_KWARGS[kwargs_name]
    if name.startswith("export_csv") and not kwargs.get("fields"):
        kwargs = {**kwargs, "fields": ["name"]}
    db = SQLerDB.in_memory()
    try:

        class Kept(SQLerModel):
            name: str = ""

        class Gone(SQLerModel):
            name: str = ""

        Kept.set_db(db, "kept_empty")
        Gone.set_db(db, "gone_table")
        Gone(name="a").save()
        db.adapter.execute("DROP TABLE gone_table")
        db.adapter.commit()

        reference = EXPORTS[name](Kept, tmp_path, kwargs)
        result = EXPORTS[name](Gone, tmp_path, kwargs)
        assert result == reference
        assert "gone_table" not in _export_tables(db)
    finally:
        db.close()
