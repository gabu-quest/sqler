"""A read creates nothing and waits for no write lock; a read of a missing table keeps the caller's work.

``find_document``, ``find_documents``, ``find_document_with_version`` and ``query`` of
``SQLerDB`` run no statement that creates a table. On a table that does not exist they give
``None``, an empty list or 0, and the table is still missing afterwards. A connection that read
the schema before the table existed no longer needs the write lock for a read. A read of a
missing table is not a failure of the thread's earlier statements: what it left uncommitted
stays to be committed; a write that fails still rolls it back. The model layer's own copy of
the document reads is gone.
"""

import os
import sqlite3
import time
import warnings

import pytest
from sqler import SQLerDB, SQLerModel
from sqler.models import _table_names


class Thing(SQLerModel):
    n: int = 0


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "reads.db")


@pytest.fixture
def db(path):
    d = SQLerDB.on_disk(path)
    try:
        yield d
    finally:
        d.close()


def _tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def test_reads_of_a_missing_table_give_empty_and_create_nothing(db, path):
    assert db.find_document("ghost", 1) is None
    assert db.find_documents("ghost", [1, 2]) == []
    assert db.find_document_with_version("ghost", 1) is None
    q = db.query("ghost")
    assert q.count() == 0
    assert q.all() == []
    assert q.first() is None
    assert q.exists() is False
    assert db.query("ghost").sum("n") is None
    assert "ghost" not in _tables(path)


def test_a_write_after_a_read_of_a_missing_table_creates_it(db, path):
    assert db.find_document("ghost", 1) is None
    rid = db.insert_document("ghost", {"n": 1})
    assert db.find_document("ghost", rid) == {"n": 1, "_id": rid}
    assert db.query("ghost").count() == 1


def test_ensure_table_called_directly_still_creates(db, path):
    db._ensure_table("made")
    assert "made" in _tables(path)


def test_a_table_dropped_by_another_connection_reads_as_missing(db, path):
    rid = db.insert_document("gone", {"n": 1})
    assert db.find_document("gone", rid) == {"n": 1, "_id": rid}
    other = sqlite3.connect(path)
    other.execute("DROP TABLE gone")
    other.commit()
    other.close()
    assert db.find_document("gone", rid) is None
    assert db.find_documents("gone", [rid]) == []
    assert db.query("gone").count() == 0


def test_a_read_does_not_wait_for_the_write_lock_on_a_stale_schema(db, path):
    # this connection read the schema before the table existed
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("CREATE TABLE docs (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)")
    other.execute("INSERT INTO docs (data) VALUES ('{\"n\": 1}')")
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO docs (data) VALUES ('{\"n\": 2}')")
    try:
        start = time.perf_counter()
        doc = db.find_document("docs", 1)
        docs = db.find_documents("docs", [1])
        waited = time.perf_counter() - start
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert doc == {"n": 1, "_id": 1}
    assert docs == [{"n": 1, "_id": 1}]
    assert waited < 1.0, f"the reads waited {waited:.2f}s"


def _rows(path, table):
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


READS = {
    "using_all": lambda d: Thing.using(d, "absent").all(),
    "using_count": lambda d: Thing.using(d, "absent").count(),
    "find_document": lambda d: d.find_document("absent", 1),
    "find_documents": lambda d: d.find_documents("absent", [1]),
    "query_count": lambda d: d.query("absent").count(),
}


@pytest.mark.parametrize("read", list(READS))
def test_a_read_of_a_missing_table_keeps_uncommitted_work(db, path, read):
    db.insert_document("kept", {"n": 1})
    db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")  # uncommitted
    READS[read](db)
    db.adapter.commit()
    assert _rows(path, "kept") == 2


def test_a_read_of_a_missing_table_inside_a_raw_begin_keeps_the_work(db, path):
    db.insert_document("kept", {"n": 1})
    db.adapter.execute("BEGIN")
    db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
    assert db.find_document("absent", 1) is None
    db.adapter.commit()
    assert _rows(path, "kept") == 2


def test_a_write_that_fails_still_rolls_back_uncommitted_work(db, path):
    db.insert_document("kept", {"n": 1})
    db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.adapter.execute("INSERT INTO absent (data) VALUES ('{}')")
    db.adapter.commit()
    assert _rows(path, "kept") == 1


@pytest.mark.parametrize("name", ["_one_row_sql", "_rows_sql", "_doc_from_row"])
def test_the_model_layer_has_no_copy_of_the_document_sql(name):
    assert not hasattr(_table_names, name)


def test_the_model_layer_reads_through_the_database_methods(db, path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert Thing.using(db).all() == []
    assert _table_names._read_document(db, "void_t", 1) is None
    assert _table_names._read_documents(db, "void_t", [1]) == []
    assert "void_t" not in _tables(path)
    assert os.path.exists(path)
