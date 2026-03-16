"""Tests for streaming query results (iter_dicts, queryset.iter, stream_jsonl)."""

from sqler import SQLerDB, SQLerModel, stream_jsonl
from sqler.query import SQLerField as F


class Item(SQLerModel):
    name: str
    category: str
    price: float


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    Item.set_db(db)
    return db


def seed_items():
    Item.save_many([
        Item(name="A", category="x", price=10.0),
        Item(name="B", category="x", price=20.0),
        Item(name="C", category="y", price=30.0),
        Item(name="D", category="y", price=40.0),
        Item(name="E", category="z", price=50.0),
    ])


class TestIterDicts:
    def test_iter_dicts_yields_all_rows(self):
        db = setup_db()
        try:
            seed_items()
            q = db.query("items")
            results = list(q.iter_dicts())
            assert len(results) == 5
            names = {r["name"] for r in results}
            assert names == {"A", "B", "C", "D", "E"}
            # Each result should have _id
            for r in results:
                assert "_id" in r
                assert isinstance(r["_id"], int)
        finally:
            db.close()

    def test_iter_dicts_with_filter(self):
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").filter(F("category") == "x")
            results = list(q.iter_dicts())
            assert len(results) == 2
            names = {r["name"] for r in results}
            assert names == {"A", "B"}
        finally:
            db.close()

    def test_iter_dicts_with_limit(self):
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").order_by("name").limit(3)
            results = list(q.iter_dicts())
            assert len(results) == 3
            assert [r["name"] for r in results] == ["A", "B", "C"]
        finally:
            db.close()

    def test_iter_dicts_empty_table(self):
        db = setup_db()
        try:
            q = db.query("items")
            results = list(q.iter_dicts())
            assert results == []
        finally:
            db.close()

    def test_iter_dicts_is_generator(self):
        """Verify iter_dicts returns a generator, not a list."""
        db = setup_db()
        try:
            seed_items()
            q = db.query("items")
            gen = q.iter_dicts()
            import types
            assert isinstance(gen, types.GeneratorType)
            # Can consume one at a time
            first = next(gen)
            assert "name" in first
        finally:
            db.close()


class TestQuerySetIter:
    def test_queryset_iter(self):
        db = setup_db()
        try:
            seed_items()
            results = list(Item.query().iter())
            assert len(results) == 5
            for inst in results:
                assert isinstance(inst, Item)
                assert inst._id is not None
        finally:
            db.close()

    def test_queryset_iter_with_filter(self):
        db = setup_db()
        try:
            seed_items()
            results = list(Item.query().filter(F("price") > 25).iter())
            assert len(results) == 3
            names = {inst.name for inst in results}
            assert names == {"C", "D", "E"}
        finally:
            db.close()

    def test_queryset_iter_is_generator(self):
        db = setup_db()
        try:
            seed_items()
            gen = Item.query().iter()
            import types
            assert isinstance(gen, types.GeneratorType)
        finally:
            db.close()


class TestStreamJsonl:
    def test_stream_jsonl_yields_all_rows(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item))
            assert len(lines) == 5
        finally:
            db.close()

    def test_stream_jsonl_is_generator(self):
        """stream_jsonl should yield rows, not build a list."""
        db = setup_db()
        try:
            seed_items()
            gen = stream_jsonl(Item)
            import types
            assert isinstance(gen, types.GeneratorType)
        finally:
            db.close()

    def test_stream_jsonl_valid_json(self):
        import json

        db = setup_db()
        try:
            seed_items()
            for line in stream_jsonl(Item):
                obj = json.loads(line)
                assert "name" in obj
                assert "_id" in obj
        finally:
            db.close()
