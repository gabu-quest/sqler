from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class User(SQLerModel):
    name: str
    age: int


def main():
    db = SQLerDB.in_memory()

    User(name="Alice", age=30).save(db=db)
    User(name="Bob", age=20).save(db=db)

    adults = User.using(db).filter(F("age") >= 18).order_by("age").all()
    print([u.name for u in adults])

    db.close()


if __name__ == "__main__":
    main()
