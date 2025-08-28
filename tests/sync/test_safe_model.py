from sqler import SQLerDB
from sqler.models import SQLerSafeModel, StaleVersionError
from sqler.query import SQLerField as F


class Customer(SQLerSafeModel):
    name: str
    tier: int


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    Customer.set_db(db)
    return db


def test_safe_model_insert_sets_version_zero():
    db = setup_db()
    try:
        c = Customer(name="Alice", tier=1)
        c.save()
        assert c._id is not None
        assert c._version == 0

        # bump via update
        c.tier = 2
        c.save()
        assert c._version == 1
    finally:
        db.close()


def test_safe_model_stale_update_raises():
    db = setup_db()
    try:
        c = Customer(name="Bob", tier=1)
        c.save()

        # simulate concurrent update: bump version behind the model's back
        db.adapter.execute("UPDATE customers SET _version = _version + 1 WHERE _id = ?;", [c._id])
        db.adapter.commit()

        c.tier = 3
        try:
            c.save()
            assert False, "Expected StaleVersionError"
        except StaleVersionError:
            pass
    finally:
        db.close()


def test_safe_model_refresh_reads_version():
    db = setup_db()
    try:
        c = Customer(name="Zoe", tier=1)
        c.save()
        assert c._version == 0

        # bump version via raw SQL
        db.adapter.execute(
            "UPDATE customers SET _version = _version + 1, data = json_set(data, '$.tier', 5) WHERE _id = ?;",
            [c._id],
        )
        db.adapter.commit()

        c.refresh()
        assert c._version == 1
        assert c.tier == 5
    finally:
        db.close()


def test_safe_model_complex_filters():
    db = setup_db()
    try:
        Customer(name="A", tier=1).save()
        Customer(name="B", tier=2).save()
        Customer(name="C", tier=3).save()

        # complex: (tier>=2) & name like 'B%'
        qs = Customer.query().filter((F("tier") >= 2) & F("name").like("B%"))
        res = qs.all()
        assert [c.name for c in res] == ["B"]

        # order + limit
        first = Customer.query().order_by("tier", desc=True).limit(1).first()
        assert first.name == "C"

        # version present only after refresh/from_id
        assert getattr(first, "_version", None) == 0  # default until refresh
        first.refresh()
        assert first._version >= 0
    finally:
        db.close()
