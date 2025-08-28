from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class User(SQLerModel):
    name: str
    age: int
    address: dict | None = None


def main():
    db = SQLerDB.in_memory()
    User.set_db(db)
    for i in range(100):
        User(name=f"U{i}", age=i, address={"city": "X" if i % 2 else "Y"}).save()

    # ensure index on a ref-like path
    User.ensure_index("address.city")

    qs = User.query().filter(F("address.city") == "X")
    print("SQL:", qs.debug())
    plan = qs.explain_query_plan(db.adapter)
    print("PLAN:", plan)

    db.close()


if __name__ == "__main__":
    main()
