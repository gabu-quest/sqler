from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class User(SQLerModel):
    name: str
    age: int


def main():
    db = SQLerDB.in_memory()
    User.set_db(db)

    User(name="Alice", age=30).save()
    User(name="Bob", age=20).save()

    adults = User.query().filter(F("age") >= 18).order_by("age").all()
    print([u.name for u in adults])

    db.close()


if __name__ == "__main__":
    main()
