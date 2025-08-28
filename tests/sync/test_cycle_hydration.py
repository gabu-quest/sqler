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
    # second hop should not recurse infinitely; back-ref may remain a ref or a shallow dict
    # ensure it is not a fully hydrated deep chain by checking attribute type or None
    # acceptable: either None or a dict-like (ref), but not same instance recursion
    if isinstance(aa.b.a, A):
        # ensure it stops here; the nested back-reference should not have its own .b hydrated again
        assert aa.b.a.b is None or not isinstance(aa.b.a.b, B)
