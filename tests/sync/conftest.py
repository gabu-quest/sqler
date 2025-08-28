import json

import pytest

from sqler import SQLerDB, SQLiteAdapter
from sqler.adapter.abstract import AdapterABC


class DummyAdapter(AdapterABC):
    def __init__(self):
        self.executed = []
        self.return_value = []  # set the raw dicts or lists or values
        self.count = 0

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def execute(self, query: str, params=None):
        self.executed.append((query, params))

        class Cursor:
            def fetchall(cur_self):
                # Support both SELECT data (single column) and SELECT _id, data (two columns)
                lower_q = query.strip().lower()
                if lower_q.startswith("select _id, data"):
                    to_return = []
                    for i in self.return_value:
                        _id = i.get("_id") if isinstance(i, dict) else None
                        if isinstance(i, dict) and "_id" in i:
                            payload = {k: v for k, v in i.items() if k != "_id"}
                        else:
                            payload = i
                        to_return.append((_id, json.dumps(payload)))
                    return to_return
                else:
                    # default single-column JSON string
                    return [(json.dumps(i),) for i in self.return_value]

            def fetchone(cur_self):
                return (self.count,)

        return Cursor()

    def executemany(self, query, param_list):
        pass

    def executescript(self, script):
        pass

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def dummy_adapter():
    return DummyAdapter()


@pytest.fixture(scope="function")
def oligo_adapter():
    adapter = SQLiteAdapter.in_memory(shared=False)
    adapter.connect()

    adapter.execute("""
        CREATE TABLE oligos (
            length INTEGER,
            sequence STRING
        );
    """)

    yield adapter

    adapter.close()


@pytest.fixture(scope="function")
def oligo_db():
    db = SQLerDB.in_memory(shared=False)
    table = "oligos"
    db._ensure_table(table=table)

    yield db

    db.close()
