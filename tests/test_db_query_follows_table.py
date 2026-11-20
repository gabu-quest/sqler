"""``db.query(table)`` follows its table as a model's query set does.

Another database object on the same file promotes a field into a column; a query built by
``db.query()`` (before or after that, chained or not) still reads every document with the field
and its value, sync and async. A table that does not exist is not created, and a read inside a
transaction sees the transaction's writes.
"""

import asyncio
import json
import sqlite3

import pytest

from sqler import AsyncSQLerDB, F, SQLerDB

TABLE = "tickets"
DOCS = [
    {"name": "a", "status": "new"},
    {"name": "b", "status": "open"},
    {"name": "c", "status": "new"},
]


def _seed(path):
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE TABLE {TABLE} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)"
    )
    for doc in DOCS:
        conn.execute(f"INSERT INTO {TABLE} (data) VALUES (json(?))", [json.dumps(doc)])
    conn.commit()
    conn.close()


class Side:
    """One way in: a reader object and another object on the same file that promotes."""

    def __init__(self, path, is_async):
        self.path, self.is_async = path, is_async
        self.loop = asyncio.new_event_loop() if is_async else None
        make = AsyncSQLerDB.on_disk if is_async else SQLerDB.on_disk
        self.reader, self.other = make(path), make(path)
        if is_async:
            self.go(self.reader.connect())
            self.go(self.other.connect())

    def go(self, value):
        return self.loop.run_until_complete(value) if self.is_async else value

    def query(self, table=TABLE):
        return self.go(self.reader.query(table))

    def run(self, q, method, *args):
        result = getattr(q, method)(*args)
        if method == "iter_dicts" and self.is_async:
            return self.go(self._collect(result))
        if method == "iter_dicts":
            return list(result)
        return self.go(result) if self.is_async else result

    @staticmethod
    async def _collect(it):
        return [row async for row in it]

    def promote(self, table=TABLE):
        self.go(self.other.ensure_table(table, promoted={"status": "TEXT"}))

    def close(self):
        self.go(self.reader.close())
        self.go(self.other.close())
        if self.loop:
            self.loop.close()


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def side(request, tmp_path):
    path = str(tmp_path / "q.db")
    _seed(path)
    s = Side(path, request.param)
    yield s
    s.close()


def _statuses(rows):
    return sorted((r["name"], r["status"]) for r in rows)


EXPECTED = sorted((d["name"], d["status"]) for d in DOCS)


def test_read_after_another_object_promoted(side):
    assert len(side.run(side.query(), "all_dicts")) == 3
    side.promote()
    assert _statuses(side.run(side.query(), "all_dicts")) == EXPECTED


def test_every_read_after_promotion(side):
    q = side.query()
    side.run(q, "all_dicts")
    side.promote()
    assert _statuses(side.run(q, "all_dicts")) == EXPECTED
    assert _statuses(side.run(q, "iter_dicts")) == EXPECTED
    first = side.run(q.order_by("name"), "first_dict")
    assert (first["name"], first["status"]) == ("a", "new")
    assert side.run(q, "count") == 3
    assert side.run(q.filter(F("status") == "new"), "count") == 2
    assert _statuses(side.run(q.filter(F("status") == "new"), "all_dicts")) == [
        ("a", "new"),
        ("c", "new"),
    ]


def test_promoted_between_build_and_first_send(side):
    q = side.query()
    side.promote()
    assert _statuses(side.run(q, "all_dicts")) == EXPECTED


def test_chain_built_before_the_change(side):
    q = side.query().filter(F("name") != "zzz").order_by("name")
    side.run(q, "all_dicts")
    side.promote()
    rows = side.run(q, "all_dicts")
    assert [(r["name"], r["status"]) for r in rows] == [("a", "new"), ("b", "open"), ("c", "new")]


def test_table_dropped_and_made_again_with_another_layout(side):
    q = side.query()
    side.run(q, "all_dicts")
    side.promote()
    side.run(q, "all_dicts")
    conn = sqlite3.connect(side.path)
    conn.execute(f"DROP TABLE {TABLE}")
    conn.execute(
        f"CREATE TABLE {TABLE} (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL, name TEXT)"
    )
    conn.execute(
        f"INSERT INTO {TABLE} (data, name) VALUES (json(?), ?)", [json.dumps({"status": "z"}), "q"]
    )
    conn.commit()
    conn.close()
    rows = side.run(q, "all_dicts")
    assert [(r["name"], r["status"]) for r in rows] == [("q", "z")]


def test_missing_table_is_not_created(side):
    q = side.query("nothing_here")
    assert side.run(q, "all_dicts") == []
    assert side.run(q, "count") == 0
    conn = sqlite3.connect(side.path)
    names = sorted(name for (name,) in conn.execute("SELECT name FROM sqlite_master"))
    conn.close()
    assert names == ["sqlite_sequence", TABLE]


def test_read_inside_a_transaction_sees_its_writes(side):
    if side.is_async:

        async def body():
            async with side.reader.transaction():
                await side.reader.insert_document(TABLE, {"name": "d", "status": "x"})
                return await (await side.reader.query(TABLE)).count()

        assert side.go(body()) == 4
    else:
        with side.reader.transaction():
            side.reader.insert_document(TABLE, {"name": "d", "status": "x"})
            assert side.reader.query(TABLE).count() == 4
