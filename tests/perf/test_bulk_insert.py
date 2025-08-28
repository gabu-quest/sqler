import pytest

pytest.importorskip("pytest_benchmark")

import random
import string

from sqler import SQLerDB
from sqler.query import SQLerField as F
from sqler.query import SQLerQuery


def _rand_str(n=16):
    return "".join(random.choice(string.ascii_letters) for _ in range(n))


def _doc(i, depth=3, width=4):
    d = {"i": i, "name": _rand_str(12), "tags": [random.randint(0, 9) for _ in range(8)]}
    cur = d
    for lvl in range(depth):
        cur["level"] = lvl
        cur["child"] = {"w": {f"k{j}": j * i for j in range(width)}}
        cur = cur["child"]
    return d


@pytest.fixture(scope="function")
def perf_db():
    db = SQLerDB.in_memory(shared=False)
    db._ensure_table("perf")
    try:
        yield db
    finally:
        db.close()


@pytest.mark.perf
def test_bulk_insert_50k(perf_db, benchmark):
    docs = [_doc(i) for i in range(50_000)]

    def _run():
        perf_db.bulk_upsert("perf", docs)

    benchmark(_run)


@pytest.mark.perf
def test_heavy_filter_sort_limit(perf_db, benchmark):
    perf_db.bulk_upsert("perf", [_doc(i) for i in range(20_000)])
    perf_db.create_index("perf", "i")
    f_i = F("i")
    q = (
        SQLerQuery("perf", perf_db.adapter)
        .filter((f_i >= 10_000) & (f_i < 11_000))
        .order_by("i")
        .limit(200)
    )

    def _run():
        return q.all()

    result = benchmark(_run)
    assert len(result) == 200
