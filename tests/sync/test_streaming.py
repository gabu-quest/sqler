"""Tests for streaming query results (iter_dicts, queryset.iter, stream_jsonl)."""

import json
import types

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
            for r in results:
                assert isinstance(r["_id"], int)
                assert r["category"] in {"x", "y", "z"}
                assert isinstance(r["price"], (int, float))
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

    def test_iter_dicts_is_generator_with_correct_data(self):
        """Verify iter_dicts returns a generator that yields correct dicts."""
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").order_by("name")
            gen = q.iter_dicts()
            assert isinstance(gen, types.GeneratorType)
            first = next(gen)
            assert first["name"] == "A"
            assert first["price"] == 10.0
            assert isinstance(first["_id"], int)
            second = next(gen)
            assert second["name"] == "B"
        finally:
            db.close()

    def test_iter_dicts_matches_all_dicts(self):
        """iter_dicts and all_dicts should return identical data."""
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").order_by("name")
            streamed = list(q.iter_dicts())
            eager = q.all_dicts()
            assert len(streamed) == len(eager) == 5
            for s, e in zip(streamed, eager):
                assert s == e
        finally:
            db.close()


class TestQuerySetIter:
    def test_queryset_iter(self):
        db = setup_db()
        try:
            seed_items()
            results = list(Item.query().order_by("name").iter())
            assert len(results) == 5
            assert [inst.name for inst in results] == ["A", "B", "C", "D", "E"]
            for inst in results:
                assert isinstance(inst, Item)
                assert isinstance(inst._id, int)
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

    def test_queryset_iter_is_generator_with_data(self):
        db = setup_db()
        try:
            seed_items()
            gen = Item.query().order_by("name").iter()
            assert isinstance(gen, types.GeneratorType)
            first = next(gen)
            assert first.name == "A"
            assert first.price == 10.0
        finally:
            db.close()


class TestStreamJsonl:
    def test_stream_jsonl_yields_all_rows(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item))
            assert len(lines) == 5
            parsed = [json.loads(line) for line in lines]
            names = {obj["name"] for obj in parsed}
            assert names == {"A", "B", "C", "D", "E"}
        finally:
            db.close()

    def test_stream_jsonl_is_generator_with_data(self):
        db = setup_db()
        try:
            seed_items()
            gen = stream_jsonl(Item)
            assert isinstance(gen, types.GeneratorType)
            first = json.loads(next(gen))
            assert first["_id"] is not None
            assert first["name"] in {"A", "B", "C", "D", "E"}
        finally:
            db.close()

    def test_stream_jsonl_valid_json_with_values(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item))
            assert len(lines) == 5
            for line in lines:
                obj = json.loads(line)
                assert obj["name"] in {"A", "B", "C", "D", "E"}
                assert isinstance(obj["_id"], int)
                assert obj["category"] in {"x", "y", "z"}
                assert isinstance(obj["price"], (int, float))
        finally:
            db.close()

    def test_stream_jsonl_exclude_id(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item, include_id=False))
            assert len(lines) == 5
            for line in lines:
                obj = json.loads(line)
                assert "_id" not in obj
                assert obj["name"] in {"A", "B", "C", "D", "E"}
        finally:
            db.close()

    def test_stream_jsonl_field_projection(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item, fields=["name", "price"]))
            assert len(lines) == 5
            for line in lines:
                obj = json.loads(line)
                assert "name" in obj
                assert "price" in obj
                assert "category" not in obj
        finally:
            db.close()

    def test_stream_jsonl_empty_table(self):
        db = setup_db()
        try:
            lines = list(stream_jsonl(Item))
            assert lines == []
        finally:
            db.close()
