"""What a database object learns about tables inside a transaction belongs to that transaction.

``ensure_table`` remembers a table's promoted columns and its ``_version`` column. Learned inside
a transaction the object opened, it is kept in that level: a commit hands it to the level above
(the shared memory at the outermost level), a rollback drops it, and neither runs a statement.
In a transaction the object did not open, nothing is remembered. Statements are counted with the
driver's trace callback.
"""

import threading
import warnings
from dataclasses import dataclass
from typing import Any

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel, SQLiteAdapter
from sqler.logging import query_logger
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel


class MemPromo(SQLerModel):
    __tablename__ = "mem_promos"
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


@dataclass
class MemRefChild(SQLerLiteSafeModel):
    __tablename__ = "mem_ref_children"
    __promoted__ = {"tier": "TEXT NOT NULL DEFAULT 'basic'"}
    name: str = ""
    tier: str = "basic"


@dataclass
class MemRefParent(SQLerLiteModel):
    __tablename__ = "mem_ref_parents"
    name: str = ""
    child: Any = None


@pytest.fixture
def db(tmp_path):
    db = SQLerDB.on_disk(str(tmp_path / "mem.db"))
    yield db
    db.close()


class Traced:
    """Statements this thread's connection sends while the block runs."""

    def __init__(self, db):
        self.conn = db.adapter._conn()
        self.sent: list[str] = []

    def __enter__(self):
        self.conn.set_trace_callback(self.sent.append)
        return self.sent

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False


def _kind(sent: list[str], word: str) -> list[str]:
    return [s for s in sent if s.lstrip().upper().startswith(word)]


def _statuses(db) -> list[str]:
    return sorted(p.status for p in MemPromo.using(db).all())


def test_tables_first_made_in_a_rolled_back_transaction_save_again_and_the_rollback_sends_only_rollback(db):
    with db.transaction() as tx:
        MemPromo(name="gone").save(db=db)
        db.ensure_table("mem_vers", versioned=True)
        with Traced(db) as sent:
            tx.rollback()
    assert sent == ["ROLLBACK"]
    MemPromo(name="kept", status="x").save(db=db)
    rid, version = db.upsert_with_version("mem_vers", None, {"n": 1}, None)
    assert (_statuses(db), version) == (["x"], 0)
    assert db.find_document_with_version("mem_vers", rid) == {"_id": rid, "n": 1, "_version": 0}


def test_inner_level_rolled_back_inside_an_outer_level_that_commits(db):
    with db.transaction():
        with pytest.raises(RuntimeError, match="undo"):
            with db.transaction():
                MemPromo(name="gone").save(db=db)
                db.ensure_table("mem_vers", versioned=True)
                raise RuntimeError("undo")
        MemPromo(name="kept", status="x").save(db=db)
        rid, version = db.upsert_with_version("mem_vers", None, {"n": 1}, None)
    assert (_statuses(db), version) == (["x"], 0)
    assert db.find_document_with_version("mem_vers", rid) == {"_id": rid, "n": 1, "_version": 0}


def test_inner_level_committed_inside_an_outer_level_that_rolls_back(db):
    with pytest.raises(RuntimeError, match="undo"):
        with db:
            with db.transaction():
                MemPromo(name="gone").save(db=db)
                db.ensure_table("mem_vers", versioned=True)
            raise RuntimeError("undo")
    MemPromo(name="kept", status="x").save(db=db)
    rid, version = db.upsert_with_version("mem_vers", None, {"n": 1}, None)
    assert (_statuses(db), version) == (["x"], 0)


def test_a_table_remembered_before_the_transaction_stays_remembered_after_its_rollback(db):
    MemPromo(name="a", status="s").save(db=db)
    db.upsert_with_version("mem_vers", None, {"n": 1}, None)
    with pytest.raises(RuntimeError, match="undo"):
        with db.transaction():
            MemPromo(name="b").save(db=db)
            raise RuntimeError("undo")
    with Traced(db) as sent:
        MemPromo(name="c", status="t").save(db=db)
        db.upsert_with_version("mem_vers", None, {"n": 2}, None)
    assert (_kind(sent, "PRAGMA"), _kind(sent, "CREATE")) == ([], [])
    assert _statuses(db) == ["s", "t"]


def test_a_thousand_saves_in_one_transaction_create_the_new_table_once(db):
    with Traced(db) as sent:
        with db.transaction():
            for i in range(1000):
                MemPromo(name=str(i)).save(db=db)
    assert len(_kind(sent, "CREATE TABLE")) == 1
    assert MemPromo.using(db).count() == 1000


def test_another_thread_saves_after_a_table_made_in_a_rolled_back_transaction(tmp_path):
    db = SQLerDB.on_disk(str(tmp_path / "threads.db"))
    a_rolled_back = threading.Event()
    errors: list[str] = []

    def thread_a():
        try:
            with pytest.raises(RuntimeError, match="undo"):
                with db.transaction():
                    MemPromo(name="a", status="a").save(db=db)
                    raise RuntimeError("undo")
        finally:
            a_rolled_back.set()

    def thread_b():
        assert a_rolled_back.wait(timeout=10)
        try:
            MemPromo(name="b", status="b").save(db=db)
        except Exception as exc:  # the outcome is what the test asserts
            errors.append(str(exc))

    threads = [threading.Thread(target=thread_a), threading.Thread(target=thread_b)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert (errors, _statuses(db)) == ([], ["b"])
    finally:
        db.close()


def test_another_thread_saves_after_a_rolled_back_transaction_on_the_single_memory_connection():
    db = SQLerDB(SQLiteAdapter(":memory:"))
    errors: list[str] = []

    def thread_b():
        try:
            MemPromo(name="b", status="b").save(db=db)
        except Exception as exc:  # the outcome is what the test asserts
            errors.append(str(exc))

    try:
        with pytest.raises(RuntimeError, match="undo"):
            with db.transaction():
                MemPromo(name="a", status="a").save(db=db)
                raise RuntimeError("undo")
        t = threading.Thread(target=thread_b)
        t.start()
        t.join(timeout=20)
        assert (errors, _statuses(db)) == ([], ["b"])
    finally:
        db.close()


def test_a_table_made_in_a_transaction_the_adapter_opened_directly_is_not_remembered(db):
    db.adapter.begin_transaction()
    db.ensure_table("mem_promos", promoted=MemPromo.__promoted__)
    db.adapter.end_transaction(commit=False)
    MemPromo(name="kept", status="x").save(db=db)
    assert _statuses(db) == ["x"]


def test_reference_to_a_lite_safe_row_loads_after_the_column_promotion_rolled_back(db):
    db.ensure_table("mem_ref_children", versioned=True)
    cid, _ = db.upsert_with_version("mem_ref_children", None, {"name": "c"}, None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        MemRefParent.set_db(db)
        pid = db.insert_document(
            "mem_ref_parents", {"name": "p", "child": {"_table": "mem_ref_children", "_id": cid}}
        )
        with pytest.raises(RuntimeError, match="undo"):
            with db:
                MemRefChild.set_db(db)  # adds the promoted column, undone below
                raise RuntimeError("undo")
    got = MemRefParent.from_id(pid)
    assert (type(got.child).__name__, got.child.name, got.child._version) == ("MemRefChild", "c", 0)


class MemSafe(SQLerSafeModel):
    __tablename__ = "mem_safes"
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


@dataclass
class MemLiteSafe(SQLerLiteSafeModel):
    __tablename__ = "mem_lite_safes"
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


class MemVersioned(SQLerSafeModel):
    __tablename__ = "mem_versioned"
    name: str = ""


def test_safe_model_bound_in_a_rolled_back_transaction_saves_its_promoted_column_afterwards(db):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(RuntimeError, match="undo"):
            with db:
                MemSafe.set_db(db)
                raise RuntimeError("undo")
    MemSafe(name="k", status="x").save()
    assert [g.status for g in MemSafe.query().all()] == ["x"]


def test_safe_model_first_saved_in_a_rolled_back_transaction_saves_again_afterwards(db):
    with pytest.raises(RuntimeError, match="undo"):
        with db:
            MemSafe(name="gone").save(db=db)
            raise RuntimeError("undo")
    MemSafe(name="k", status="x").save(db=db)
    assert [g.status for g in MemSafe.using(db).all()] == ["x"]


def test_lite_safe_model_first_saved_in_a_rolled_back_transaction_saves_again_afterwards(db):
    with pytest.raises(RuntimeError, match="undo"):
        with db:
            MemLiteSafe(name="gone").save(db=db)
            raise RuntimeError("undo")
    MemLiteSafe(name="k", status="x").save(db=db)
    assert [g.status for g in MemLiteSafe.using(db).all()] == ["x"]


def test_save_many_of_a_promoted_class_looks_the_table_up_once_for_two_calls(db):
    with query_logger.capture() as sent:
        MemPromo.save_many([MemPromo(name="a")], db=db)
        MemPromo.save_many([MemPromo(name="b")], db=db)
    assert len([q for q in sent if "sqlite_master" in q.sql]) == 1
    assert MemPromo.using(db).count() == 2


def test_a_promoted_table_made_in_an_adapter_savepoint_rolled_back_inside_a_level_saves_afterwards(db):
    with db:
        db.adapter.begin_transaction()
        db.ensure_table("mem_promos", promoted=MemPromo.__promoted__)
        db.adapter.end_transaction(commit=False)
    MemPromo(name="k", status="x").save(db=db)
    assert _statuses(db) == ["x"]


def test_a_versioned_table_made_in_an_adapter_savepoint_rolled_back_inside_a_level_saves_afterwards(db):
    with db:
        db.adapter.begin_transaction()
        MemVersioned(name="gone").save(db=db)
        db.adapter.end_transaction(commit=False)
    MemVersioned(name="k").save(db=db)
    assert [v.name for v in MemVersioned.using(db).all()] == ["k"]


def test_a_level_committed_inside_an_adapter_savepoint_that_rolls_back_is_not_remembered(db):
    with db:
        db.adapter.begin_transaction()
        with db.transaction():
            MemPromo(name="gone").save(db=db)
        db.adapter.end_transaction(commit=False)
    MemPromo(name="k", status="x").save(db=db)
    assert _statuses(db) == ["x"]


def test_a_transaction_ended_on_another_thread_raises_nothing_and_merges_nothing(db):
    txn = db.transaction()
    txn.__enter__()
    seen: list[str] = []

    def end():
        try:
            txn.__exit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 - the test reports what was raised
            seen.append(type(exc).__name__)

    t = threading.Thread(target=end)
    t.start()
    t.join(timeout=10)
    assert seen == []
    txn.__exit__(None, None, None)
