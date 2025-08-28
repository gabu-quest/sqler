from sqler import SQLerDB
from sqler.models import SQLerModel
from sqler.query import SQLerField as F


class Product(SQLerModel):
    name: str
    price: int
    tags: list[str] | None = None
    items: list[dict] | None = None


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    Product.set_db(db)
    return db


def seed(db):
    Product(
        name="Laptop",
        price=1000,
        tags=["electronics", "computers"],
        items=[{"sku": "A1", "qty": 2}],
    ).save()
    Product(
        name="Mouse", price=50, tags=["electronics", "accessories"], items=[{"sku": "B2", "qty": 5}]
    ).save()
    Product(
        name="Keyboard",
        price=100,
        tags=["electronics", "accessories"],
        items=[{"sku": "C3", "qty": 1}],
    ).save()


def test_arrays_and_any_filters():
    db = setup_db()
    try:
        seed(db)

        # contains
        p = Product.query().filter(F("tags").contains("electronics")).order_by("price").all()
        assert [x.name for x in p] == ["Mouse", "Keyboard", "Laptop"]

        # isin
        p2 = Product.query().filter(F("tags").isin(["computers"])).all()
        assert [x.name for x in p2] == ["Laptop"]

        # any over array of objects: items[].qty > 3
        p3 = Product.query().filter(F(["items"]).any()["qty"] > 3).order_by("price").all()
        assert [x.name for x in p3] == ["Mouse"]

        # complex boolean: (price>=50 & price<=100) & !like('M%')
        cond = (F("price") >= 50) & (F("price") <= 100) & ~F("name").like("M%")
        p4 = Product.query().filter(cond).all()
        assert [x.name for x in p4] == ["Keyboard"]

        # exclude + limit/desc
        q = (
            Product.query()
            .exclude(F("tags").contains("accessories"))
            .order_by("price", desc=True)
            .limit(1)
        )
        first = q.first()
        assert first.name == "Laptop"
    finally:
        db.close()
