import pytest

pytest.importorskip("pytest_benchmark")

import random

from sqler import SQLerDB
from sqler.query import SQLerField as F
from sqler.query import SQLerQuery


@pytest.fixture(scope="function")
def array_db():
    db = SQLerDB.in_memory(shared=False)
    db._ensure_table("arrs")
    for i in range(15_000):
        db.insert_document(
            "arrs",
            {
                "name": f"row{i}",
                "tags": [random.randint(0, 100) for _ in range(12)],
                "events": [
                    {"type": "a", "val": i % 7},
                    {"type": "b", "val": (i * 3) % 11},
                ],
            },
        )
    try:
        yield db
    finally:
        db.close()


@pytest.mark.perf
def test_contains(array_db, benchmark):
    tags = F("tags")
    q = SQLerQuery("arrs", array_db.adapter).filter(tags.contains(42))

    def _run():
        return len(q.all())

    n = benchmark(_run)
    assert n > 0


@pytest.mark.perf
def test_isin(array_db, benchmark):
    tags = F("tags")
    q2 = SQLerQuery("arrs", array_db.adapter).filter(tags.isin([17, 42]))

    def _run2():
        return len(q2.all())

    n2 = benchmark(_run2)
    assert n2 > 0


@pytest.mark.perf
def test_nested_any(array_db, benchmark):
    expr = F(["events"]).any()["val"] > 8
    q = SQLerQuery("arrs", array_db.adapter).filter(expr)

    def _run():
        return len(q.all())

    n = benchmark(_run)
    assert n > 0
