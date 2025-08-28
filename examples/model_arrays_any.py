from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class Product(SQLerModel):
    name: str
    price: int
    tags: list[str] | None = None
    items: list[dict] | None = None


def main():
    db = SQLerDB.in_memory()
    Product.set_db(db)

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

    # contains
    p = Product.query().filter(F("tags").contains("electronics")).order_by("price").all()
    print("contains electronics:", [x.name for x in p])

    # isin
    p2 = Product.query().filter(F("tags").isin(["computers"])).all()
    print("isin computers:", [x.name for x in p2])

    # any over array of objects: items[].qty > 3
    p3 = Product.query().filter(F(["items"]).any()["qty"] > 3).order_by("price").all()
    print("any qty>3:", [x.name for x in p3])

    db.close()


if __name__ == "__main__":
    main()
