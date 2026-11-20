"""AuditLogMixin: a save and its audit row are both there or neither is."""

import pytest

from sqler import SQLerDB, SQLerModel
from sqler.exceptions import UniqueConstraintError
from sqler.models.mixins import AuditLogMixin


class AtomicItem(AuditLogMixin, SQLerModel):
    __tablename__ = "atomic_items"
    name: str


@pytest.fixture
def db():
    db = SQLerDB.in_memory(shared=False)
    AtomicItem.set_db(db)
    yield db
    db.close()


def _names(db):
    return [
        r[0]
        for r in db.adapter.execute(
            "SELECT json_extract(data, '$.name') FROM atomic_items ORDER BY _id;"
        ).fetchall()
    ]


def test_failing_audit_row_leaves_no_saved_change(db):
    item = AtomicItem(name="one").save()
    item.name = "two"
    item.save()
    db.adapter.execute("CREATE UNIQUE INDEX audit_once ON atomic_items_audit(record_id, action);")
    db.adapter.auto_commit()
    item.name = "three"
    with pytest.raises(UniqueConstraintError):
        item.save()
    assert _names(db) == ["two"]


def test_failing_audit_row_on_create_leaves_no_row(db):
    AtomicItem(name="seed").save()
    db.adapter.execute("CREATE UNIQUE INDEX audit_once ON atomic_items_audit(action);")
    db.adapter.auto_commit()
    with pytest.raises(UniqueConstraintError):
        AtomicItem(name="second").save()
    assert _names(db) == ["seed"]


def test_failing_audit_row_on_delete_keeps_the_row(db):
    item = AtomicItem(name="keep").save()
    item.name = "keep2"
    item.save()
    db.adapter.execute("CREATE UNIQUE INDEX audit_once ON atomic_items_audit(action);")
    db.adapter.execute(
        "INSERT INTO atomic_items_audit(record_id, action, timestamp) VALUES (99, 'delete', 'x');"
    )
    db.adapter.auto_commit()
    with pytest.raises(UniqueConstraintError):
        item.delete()
    assert _names(db) == ["keep2"]


def test_inside_a_rolled_back_transaction_both_vanish(db):
    AtomicItem(name="base").save()
    with pytest.raises(RuntimeError):
        with db.transaction():
            AtomicItem(name="inside").save()
            raise RuntimeError("boom")
    assert _names(db) == ["base"]
    n = db.adapter.execute("SELECT COUNT(*) FROM atomic_items_audit;").fetchone()[0]
    assert n == 1


def test_inside_a_committed_transaction_both_stay(db):
    with db.transaction():
        AtomicItem(name="inside").save()
    assert _names(db) == ["inside"]
    n = db.adapter.execute("SELECT COUNT(*) FROM atomic_items_audit;").fetchone()[0]
    assert n == 1


def test_ordinary_save_update_delete_still_log(db):
    item = AtomicItem(name="a").save()
    item.name = "b"
    item.save()
    assert [e["action"] for e in item.get_audit_log()] == ["update", "create"]
    item.delete()
    rows = db.adapter.execute("SELECT action FROM atomic_items_audit ORDER BY _id;").fetchall()
    assert [r[0] for r in rows] == ["create", "update", "delete"]


def test_failed_create_leaves_the_instance_unsaved_and_a_second_save_creates(db):
    AtomicItem(name="seed").save()
    db.adapter.execute("CREATE UNIQUE INDEX audit_once ON atomic_items_audit(action);")
    db.adapter.auto_commit()
    item = AtomicItem(name="second")
    with pytest.raises(UniqueConstraintError):
        item.save()
    assert item._id is None
    db.adapter.execute("DROP INDEX audit_once;")
    db.adapter.auto_commit()
    item.save()
    assert _names(db) == ["seed", "second"]


def test_delete_refused_by_a_trigger_leaves_no_delete_audit_row(db):
    item = AtomicItem(name="keep").save()
    db.adapter.execute(
        "CREATE TRIGGER no_delete BEFORE DELETE ON atomic_items "
        "BEGIN SELECT RAISE(ABORT, 'delete refused'); END;"
    )
    db.adapter.auto_commit()
    with pytest.raises(Exception, match="delete refused"):
        item.delete()
    assert _names(db) == ["keep"]
    rows = db.adapter.execute("SELECT action FROM atomic_items_audit;").fetchall()
    assert [r[0] for r in rows] == ["create"]
    assert item._id is not None
