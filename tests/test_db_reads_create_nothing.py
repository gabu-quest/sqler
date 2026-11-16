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
from sqler.exceptions import NoAdapterError
from sqler.logging import query_logger
from sqler.models import _table_names
from sqler.query import SQLerExpression, SQLerQuery


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


# --- the query is the one place where a missing table reads as empty --------------------
#
# The reference is an existing table with no rows, in the same database. Every method of the
# query that runs a statement on the query's own table gives on a missing table what it gives
# on that reference, creates nothing, and sends one statement (counted in the adapter's log).


def _page(p):
    return (p.items, p.total, p.total_pages, p.page, p.per_page)


QUERY_CALLS = {
    "all": (lambda q: q.all(), []),
    "first": (lambda q: q.first(), None),
    "all_dicts": (lambda q: q.all_dicts(), []),
    "first_dict": (lambda q: q.first_dict(), None),
    "iter_dicts": (lambda q: list(q.iter_dicts()), []),
    "count": (lambda q: q.count(), 0),
    "sum": (lambda q: q.sum("n"), None),
    "avg": (lambda q: q.avg("n"), None),
    "min": (lambda q: q.min("n"), None),
    "max": (lambda q: q.max("n"), None),
    "grouped_count": (lambda q: q.group_by("n").count(), []),
    "grouped_sum": (lambda q: q.group_by("n").sum("n"), []),
    "grouped_avg": (lambda q: q.group_by("n").avg("n"), []),
    "grouped_min": (lambda q: q.group_by("n").min("n"), []),
    "grouped_max": (lambda q: q.group_by("n").max("n"), []),
    "exists": (lambda q: q.exists(), False),
    "distinct_values": (lambda q: q.distinct_values("n"), []),
    "paginate": (lambda q: _page(q.paginate(1, 5)), ([], 0, 0, 1, 5)),
    "update": (lambda q: q.update(n=1), 0),
    "update_one": (lambda q: q.update_one(n=1), None),
    "update_n": (lambda q: q.update_n(2, k=1), []),
    "delete": (lambda q: q.delete(), 0),
}

QUERY_ROUTES = {
    "db_query": lambda d, t: d.query(t),
    "by_hand": lambda d, t: SQLerQuery(table=t, adapter=d.adapter),
}
# what a read of a missing table sends: a query from the database looks at the table first, as a
# model's query set does; one built by hand on the adapter sends its own statement only
SENT = {"db_query": 2, "by_hand": 1}


def _bound(d, table):
    cls = type(f"Bound_{table}", (Thing,), {"__tablename__": table, "__module__": __name__})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(d)  # creates the table
    return cls


def _drop(d, table):
    d.adapter.execute(f"DROP TABLE {table}")
    d.adapter.commit()


def _model_query(d, table):
    cls = _bound(d, table)
    if table == "absent":
        _drop(d, table)
    return cls.query()


QS_CALLS = {
    "all": (lambda q: q.all(), []),
    "first": (lambda q: q.first(), None),
    "as_dicts": (lambda q: q.as_dicts(), []),
    "iter": (lambda q: list(q.iter()), []),
    "count": (lambda q: q.count(), 0),
    "sum": (lambda q: q.sum("n"), None),
    "avg": (lambda q: q.avg("n"), None),
    "min": (lambda q: q.min("n"), None),
    "max": (lambda q: q.max("n"), None),
    "grouped_count": (lambda q: q.group_by("n").count(), []),
    "grouped_sum": (lambda q: q.group_by("n").sum("n"), []),
    "grouped_avg": (lambda q: q.group_by("n").avg("n"), []),
    "grouped_min": (lambda q: q.group_by("n").min("n"), []),
    "grouped_max": (lambda q: q.group_by("n").max("n"), []),
    "exists": (lambda q: q.exists(), False),
    "distinct_values": (lambda q: q.distinct_values("n"), []),
    "paginate": (lambda q: _page(q.paginate(1, 5)), ([], 0, 0, 1, 5)),
    "update": (lambda q: q.update(n=1), 0),
    "update_one": (lambda q: q.update_one(n=1), None),
    "update_n": (lambda q: q.update_n(2, k=1), []),
    "delete_all": (lambda q: q.delete_all(), 0),
}

QS_ROUTES = {
    "using": lambda d, t: Thing.using(d, t),
    "model_query": _model_query,
}


def _answers(d, path, route, call):
    d.ensure_table("present")
    reference = call(route(d, "present"))
    q = route(d, "absent")
    with query_logger.capture() as sent:
        result = call(q)
    return reference, result, len(sent), "absent" in _tables(path)


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
@pytest.mark.parametrize("name", list(QUERY_CALLS))
def test_the_query_of_a_missing_table_answers_as_an_empty_one(db, path, route, name):
    call, empty = QUERY_CALLS[name]
    assert _answers(db, path, QUERY_ROUTES[route], call) == (empty, empty, SENT[route], False)


@pytest.mark.parametrize("route", list(QS_ROUTES))
@pytest.mark.parametrize("name", list(QS_CALLS))
def test_the_queryset_of_a_missing_table_answers_as_an_empty_one(db, path, route, name):
    call, empty = QS_CALLS[name]
    # two statements: the look at the table its database remembers nothing of, then its own
    assert _answers(db, path, QS_ROUTES[route], call) == (empty, empty, 2, False)


def test_db_query_reads_through_its_database_on_the_adapters_own_connection(db):
    q = db.query("absent")
    assert (q._adapter is db.adapter, q._db is db) == (True, True)


# --- hostile inputs ----------------------------------------------------------------------


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
def test_iter_dicts_of_a_missing_table_runs_at_the_first_next_and_yields_nothing(db, path, route):
    with query_logger.capture() as sent:
        rows = QUERY_ROUTES[route](db, "absent").iter_dicts()
        before = len(sent)
        first = next(rows, "end")
    assert (before, first, len(sent), "absent" in _tables(path)) == (0, "end", SENT[route], False)


def test_iter_dicts_abandoned_halfway_on_an_existing_table(db, path):
    for n in (1, 2, 3):
        db.insert_document("docs", {"n": n})
    rows = db.query("docs").iter_dicts()
    first = next(rows)
    rows.close()
    db.insert_document("docs", {"n": 4})
    assert (first, db.query("docs").count(), _rows(path, "docs")) == ({"n": 1, "_id": 1}, 4, 4)


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
def test_a_missing_table_read_in_a_transaction_keeps_the_write_and_commits(db, path, route):
    db.insert_document("kept", {"n": 1})
    with db.transaction():
        db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
        read = (QUERY_ROUTES[route](db, "absent").all(), QUERY_ROUTES[route](db, "absent").count())
        inside = db.query("kept").count()
    assert (read, inside, _rows(path, "kept"), "absent" in _tables(path)) == (([], 0), 2, 2, False)


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
def test_writes_of_a_missing_table_in_a_transaction_leave_it_usable(db, path, route):
    db.insert_document("kept", {"n": 1})
    with db.transaction():
        q = QUERY_ROUTES[route](db, "absent")
        answers = (q.update(n=1), q.update_one(n=1), q.update_n(2, k=1), q.delete())
        db.adapter.execute("INSERT INTO kept (data) VALUES ('{\"n\": 2}')")
    assert (answers, _rows(path, "kept"), "absent" in _tables(path)) == ((0, None, [], 0), 2, False)


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
@pytest.mark.parametrize("name", ["all", "count", "update", "delete"])
def test_a_statement_naming_another_missing_table_still_raises(db, route, name):
    db.insert_document("here", {"n": 1})
    joined = SQLerExpression("EXISTS (SELECT 1 FROM other_missing)")
    q = QUERY_ROUTES[route](db, "here").filter(joined)
    with pytest.raises(sqlite3.OperationalError, match="^no such table: other_missing$"):
        QUERY_CALLS[name][0](q)


@pytest.mark.parametrize(("own", "named"), [("a", "ab"), ("ab", "a")])
def test_a_missing_table_whose_name_shares_a_prefix_is_not_the_own_table(db, own, named):
    db.insert_document(own, {"n": 1})
    q = db.query(own).filter(SQLerExpression(f"EXISTS (SELECT 1 FROM {named})"))
    with pytest.raises(sqlite3.OperationalError, match=f"^no such table: {named}$"):
        q.count()


@pytest.mark.parametrize("name", list(QUERY_CALLS))
def test_a_query_with_no_adapter_still_raises(name):
    with pytest.raises(NoAdapterError, match="No adapter set for query"):
        QUERY_CALLS[name][0](SQLerQuery(table="absent"))


@pytest.mark.parametrize("route", list(QUERY_ROUTES))
def test_a_table_dropped_after_the_query_was_built_reads_as_empty(db, path, route):
    db.insert_document("gone", {"n": 1})
    q = QUERY_ROUTES[route](db, "gone")
    other = sqlite3.connect(path)
    other.execute("DROP TABLE gone")
    other.commit()
    other.close()
    assert (q.all_dicts(), q.count(), q.group_by("n").count()) == ([], 0, [])
