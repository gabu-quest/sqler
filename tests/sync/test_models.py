from sqler import SQLerDB
from sqler.models import SQLerModel
from sqler.query import SQLerField as F


class User(SQLerModel):
    name: str
    age: int


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    User.set_db(db)
    return db


def test_model_crud_lifecycle():
    db = setup_db()
    try:
        u = User(name="Alice", age=30)
        assert u._id is None
        u.save()
        assert isinstance(u._id, int)

        # fetch via from_id
        fetched = User.from_id(u._id)
        assert isinstance(fetched, User)
        assert fetched.name == "Alice"
        assert fetched.age == 30

        # update + refresh
        u.age = 31
        u.save()
        u.age = 0
        u.refresh()
        assert u.age == 31

        # delete
        user_id = u._id
        u.delete()
        assert u._id is None
        assert User.from_id(user_id) is None
    finally:
        db.close()


def test_model_query_chaining():
    db = setup_db()
    try:
        # seed
        User(name="A", age=20).save()
        User(name="B", age=30).save()
        User(name="C", age=40).save()

        qs = User.query().filter(F("age") >= 30).order_by("age").limit(2)
        results = qs.all()
        assert [u.name for u in results] == ["B", "C"]
        assert isinstance(results[0], User)

        first = User.query().filter(F("age") >= 30).order_by("age").first()
        assert isinstance(first, User)
        assert first.name == "B"

        count = User.query().filter(F("age") >= 30).count()
        assert count == 2

        # sql inspection
        s = qs.sql()
        assert s.startswith("SELECT data FROM users") or s.startswith("SELECT _id, data FROM users")
    finally:
        db.close()


def test_model_add_index():
    db = setup_db()
    try:
        # just ensure no exception and index is created
        User.add_index("age")
        # smoke: creating it again should be no-op due to IF NOT EXISTS
        User.add_index("age")
    finally:
        db.close()
