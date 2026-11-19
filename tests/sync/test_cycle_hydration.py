from sqler import SQLerDB
from sqler.models import SQLerModel


class A(SQLerModel):
    name: str
    b: "B | dict | None" = None  # type: ignore[name-defined]


class B(SQLerModel):
    name: str
    a: A | dict | None = None


def test_hydration_handles_cycle_once():
    db = SQLerDB.in_memory(shared=False)
    A.set_db(db)
    B.set_db(db)

    a = A(name="a").save()
    b = B(name="b", a=a).save()
    a.b = b
    a.save()

    rows = A.query().all()
    aa = rows[0]
    assert isinstance(aa.b, B)
    assert aa.b.name == "b"
    # the cycle closes on its objects: the back-reference is the row loaded first
    assert aa.b.a is aa
