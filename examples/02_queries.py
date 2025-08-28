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

    print(
        "contains electronics:",
        [p.name for p in Product.query().filter(F("tags").contains("electronics")).all()],
    )
    print(
        "isin computers:",
        [p.name for p in Product.query().filter(F("tags").isin(["computers"])).all()],
    )
    print(
        "any qty>3:", [p.name for p in Product.query().filter(F(["items"]).any()["qty"] > 3).all()]
    )

    db.close()


if __name__ == "__main__":
    main()
