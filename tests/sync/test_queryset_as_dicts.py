from sqler import SQLerDB
from sqler.models import SQLerModel
from sqler.query import SQLerField as F


class Item(SQLerModel):
    name: str
    price: float


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    Item.set_db(db)
    return db


def test_as_dicts_returns_list_of_dicts():
    db = setup_db()
    try:
        Item(name="A", price=10.0).save()
        Item(name="B", price=20.0).save()

        results = Item.query().as_dicts()
        assert len(results) == 2
        assert all(isinstance(d, dict) for d in results)
        # must NOT be model instances
        assert not any(isinstance(d, Item) for d in results)
        names = {d["name"] for d in results}
        assert names == {"A", "B"}
    finally:
        db.close()


def test_as_dicts_includes_id():
    db = setup_db()
    try:
        item = Item(name="X", price=5.0)
        item.save()

        results = Item.query().as_dicts()
        assert len(results) == 1
        assert results[0]["_id"] == item._id
        assert results[0]["name"] == "X"
        assert results[0]["price"] == 5.0
    finally:
        db.close()


def test_as_dicts_with_filter():
    db = setup_db()
    try:
        Item(name="cheap", price=5.0).save()
        Item(name="mid", price=50.0).save()
        Item(name="expensive", price=100.0).save()

        results = Item.query().filter(F("price") > 10).as_dicts()
        assert len(results) == 2
        names = {d["name"] for d in results}
        assert names == {"mid", "expensive"}
    finally:
        db.close()


def test_as_dicts_with_select():
    db = setup_db()
    try:
        Item(name="widget", price=9.99).save()

        results = Item.query().select("name").as_dicts()
        assert len(results) == 1
        assert results[0]["name"] == "widget"
        assert results[0]["_id"] >= 1
        # select("name") must exclude price
        assert "price" not in results[0]
    finally:
        db.close()


def test_as_dicts_with_order_and_limit():
    db = setup_db()
    try:
        Item(name="C", price=30.0).save()
        Item(name="A", price=10.0).save()
        Item(name="B", price=20.0).save()

        results = Item.query().order_by("price").limit(2).as_dicts()
        assert len(results) == 2
        assert results[0]["name"] == "A"
        assert results[1]["name"] == "B"
    finally:
        db.close()


def test_as_dicts_empty_result():
    db = setup_db()
    try:
        results = Item.query().as_dicts()
        assert results == []
    finally:
        db.close()
