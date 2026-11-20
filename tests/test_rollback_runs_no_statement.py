"""Ending a sync transaction by rollback raises nothing of its own: the caller sees its exception.

What the transaction taught the database object about its tables is dropped with it; no
statement runs for that. With the adapter closed inside the ``with`` block, the caller's
exception is the one that leaves the block.
"""

import os
import tempfile

import pytest

from sqler import SQLerDB, SQLerModel


class SyncRollPromo(SQLerModel):
    __promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
    name: str = ""
    status: str = "new"


@pytest.fixture
def disk_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = SQLerDB.on_disk(os.path.join(tmpdir, "f.db"))
        try:
            yield db
        finally:
            db.close()


def test_rollback_on_a_closed_adapter_gives_the_callers_exception(disk_db):
    disk_db.ensure_table("vers", versioned=True)
    with pytest.raises(RuntimeError, match="mine"):
        with disk_db.transaction():
            disk_db.adapter.close()
            raise RuntimeError("mine")


def test_database_context_rollback_on_a_closed_adapter_gives_the_callers_exception(disk_db):
    SyncRollPromo(name="kept").save(db=disk_db)
    with pytest.raises(RuntimeError, match="mine"):
        with disk_db:
            disk_db.adapter.close()
            raise RuntimeError("mine")


def test_versioned_table_kept_through_a_rollback_still_reads_and_writes(disk_db):
    rid, _ = disk_db.upsert_with_version("vers", None, {"n": 1}, None)
    with pytest.raises(RuntimeError, match="undo"):
        with disk_db.transaction():
            disk_db.upsert_with_version("vers", rid, {"n": 2}, 0)
            raise RuntimeError("undo")
    assert disk_db.find_document_with_version("vers", rid) == {"_id": rid, "n": 1, "_version": 0}
    _, version = disk_db.upsert_with_version("vers", rid, {"n": 3}, 0)
    assert version == 1
