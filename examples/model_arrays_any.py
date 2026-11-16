from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class Product(SQLerModel):
    name: str
    price: int
    tags: list[str] | None = None
    items: list[dict] | None = None


def main():
    db = SQLerDB.in_memory()

    Product(
        name="Laptop",
        price=1000,
        tags=["electronics", "computers"],
        items=[{"sku": "A1", "qty": 2}],
    ).save(db=db)
    Product(
        name="Mouse", price=50, tags=["electronics", "accessories"], items=[{"sku": "B2", "qty": 5}]
    ).save(db=db)
    Product(
        name="Keyboard",
        price=100,
        tags=["electronics", "accessories"],
        items=[{"sku": "C3", "qty": 1}],
    ).save(db=db)

    # contains
    p = Product.using(db).filter(F("tags").contains("electronics")).order_by("price").all()
    print("contains electronics:", [x.name for x in p])

    # isin
    p2 = Product.using(db).filter(F("tags").isin(["computers"])).all()
    print("isin computers:", [x.name for x in p2])

    # any over array of objects: items[].qty > 3
    p3 = Product.using(db).filter(F(["items"]).any()["qty"] > 3).order_by("price").all()
    print("any qty>3:", [x.name for x in p3])

    db.close()


if __name__ == "__main__":
    main()
