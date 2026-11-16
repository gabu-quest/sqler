"""``ensure_table`` is the public way to make a table with promoted columns, checks and a version.

Called with the argument shapes qler passes to the underscore methods today
(a ``{column: definition}`` dict, a ``{name: expression}`` checks dict, then a
version column), it leaves the same schema as those calls in qler's order.
"""

import sqlite3

import pytest

from sqler import AsyncSQLerDB, SQLerDB
from sqler.exceptions import InvalidColumnDefError, InvalidTableNameError

# The shapes qler passes (queue.py: _ensure_table_with_promoted(name, __promoted__, __checks__),
# then _ensure_versioned_table(name)); the values are made up for this test.
PROMOTED = {
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "eta": "REAL",
    "ulid": "TEXT NOT NULL DEFAULT ''",
}
CHECKS = {
    "status_valid": "status IN ('pending', 'running', 'done')",
    "priority_range": "priority BETWEEN -100 AND 100",
}


def _schema(path, table):
    conn = sqlite3.connect(path)
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", [table]).fetchone()[0]
    finally:
        conn.close()
    return info, sql


def _sync_old(path):
    db = SQLerDB.on_disk(path)
    db._ensure_table_with_promoted("qler_jobs", PROMOTED, CHECKS)
    db._ensure_versioned_table("qler_jobs")
    db.close()


async def _async_old(path):
    db = AsyncSQLerDB.on_disk(path)
    await db.connect()
    await db._ensure_table_with_promoted("qler_jobs", PROMOTED, CHECKS)
    await db._ensure_versioned_table("qler_jobs")
    await db.close()


class TestSync:
    def test_same_schema_as_qler_calls_and_idempotent(self, tmp_path):
        old, new = str(tmp_path / "old.db"), str(tmp_path / "new.db")
        _sync_old(old)
        db = SQLerDB.on_disk(new)
        db.ensure_table("qler_jobs", promoted=PROMOTED, checks=CHECKS, versioned=True)
        db.ensure_table("qler_jobs", promoted=PROMOTED, checks=CHECKS, versioned=True)
        db.close()
        assert _schema(new, "qler_jobs") == _schema(old, "qler_jobs")
        info, _ = _schema(new, "qler_jobs")
        assert [c[1] for c in info] == ["_id", "data", "status", "priority", "eta", "ulid", "_version"]

    def test_plain_table(self, tmp_path):
        path = str(tmp_path / "p.db")
        db = SQLerDB.on_disk(path)
        db.ensure_table("docs")
        db.close()
        info, _ = _schema(path, "docs")
        assert [(c[1], c[2], c[3], c[5]) for c in info] == [("_id", "INTEGER", 0, 1), ("data", "JSON", 1, 0)]

    def test_versioned_adds_the_column_to_an_existing_table(self, tmp_path):
        path = str(tmp_path / "v.db")
        db = SQLerDB.on_disk(path)
        db.insert_document("docs", {"a": 1})
        db.ensure_table("docs", versioned=True)
        assert db.find_document_with_version("docs", 1) == {"a": 1, "_id": 1, "_version": 0}
        db.close()
        assert [c[1] for c in _schema(path, "docs")[0]] == ["_id", "data", "_version"]

    def test_promoted_writes_work_after_it(self):
        db = SQLerDB.in_memory(shared=False)
        db.ensure_table("qler_jobs", promoted=PROMOTED, checks=CHECKS, versioned=True)
        _id, version = db.upsert_with_version_promoted(
            "qler_jobs", None, {"status": "running", "payload": 1}, None, list(PROMOTED)
        )
        doc = db.find_document_with_version("qler_jobs", _id)
        assert (version, doc["status"], doc["payload"], doc["priority"]) == (0, "running", 1, 0)
        db.close()

    @pytest.mark.parametrize("bad", ["TEXT; DROP TABLE t", "TEXT DEFAULT 'a';"], ids=repr)
    def test_column_definition_with_semicolon_is_refused(self, bad):
        db = SQLerDB.in_memory(shared=False)
        with pytest.raises(InvalidColumnDefError):
            db.ensure_table("t", promoted={"a": bad})
        assert db.adapter.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
        db.close()

    def test_check_with_semicolon_is_refused(self):
        db = SQLerDB.in_memory(shared=False)
        with pytest.raises(InvalidColumnDefError):
            db.ensure_table("t", promoted={"a": "TEXT"}, checks={"c": "a > 0; DROP TABLE x"})
        db.close()

    def test_bad_table_name_is_refused(self):
        db = SQLerDB.in_memory(shared=False)
        with pytest.raises(InvalidTableNameError):
            db.ensure_table("t\n", versioned=True)
        db.close()


class TestAsync:
    @pytest.mark.asyncio
    async def test_same_schema_as_qler_calls_and_idempotent(self, tmp_path):
        old, new = str(tmp_path / "old.db"), str(tmp_path / "new.db")
        await _async_old(old)
        db = AsyncSQLerDB.on_disk(new)
        await db.connect()
        await db.ensure_table("qler_jobs", promoted=PROMOTED, checks=CHECKS, versioned=True)
        await db.ensure_table("qler_jobs", promoted=PROMOTED, checks=CHECKS, versioned=True)
        await db.close()
        assert _schema(new, "qler_jobs") == _schema(old, "qler_jobs")
        # sync and async leave the same schema
        sync_path = str(tmp_path / "sync.db")
        _sync_old(sync_path)
        assert _schema(new, "qler_jobs") == _schema(sync_path, "qler_jobs")

    @pytest.mark.asyncio
    async def test_versioned_adds_the_column_to_an_existing_table(self, tmp_path):
        path = str(tmp_path / "v.db")
        db = AsyncSQLerDB.on_disk(path)
        await db.connect()
        await db.insert_document("docs", {"a": 1})
        await db.ensure_table("docs", versioned=True)
        assert await db.find_document_with_version("docs", 1) == {"a": 1, "_id": 1, "_version": 0}
        await db.close()
        assert [c[1] for c in _schema(path, "docs")[0]] == ["_id", "data", "_version"]

    @pytest.mark.asyncio
    async def test_column_definition_with_semicolon_is_refused(self):
        db = AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        with pytest.raises(InvalidColumnDefError):
            await db.ensure_table("t", promoted={"a": "TEXT; DROP TABLE t"})
        with pytest.raises(InvalidTableNameError):
            await db.ensure_table("t\n")
        await db.close()


def _api_database_blocks():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "API.md").read_text(encoding="utf-8")
    section = text.split("\n## Database\n", 1)[1].split("\n## ", 1)[0]
    return section.split("```python\n")[1:]


def test_api_database_section_blocks_run():
    blocks = _api_database_blocks()
    assert len(blocks) == 4
    for block in blocks:
        exec(compile(block.split("```", 1)[0], "docs/API.md#database", "exec"), {})


def _table_sql(path, table):
    return _schema(path, table)[1]


def test_empty_promoted_writes_the_same_table_sql_as_none_sync(tmp_path):
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    for path, kwargs in ((a, {}), (b, {"promoted": {}})):
        db = SQLerDB.on_disk(path)
        db.ensure_table("t", **kwargs)
        db.close()
    assert _table_sql(b, "t") == _table_sql(a, "t")


@pytest.mark.asyncio
async def test_empty_promoted_writes_the_same_table_sql_as_none_async(tmp_path):
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    for path, kwargs in ((a, {}), (b, {"promoted": {}})):
        db = AsyncSQLerDB.on_disk(path)
        await db.connect()
        await db.ensure_table("t", **kwargs)
        await db.close()
    assert _table_sql(b, "t") == _table_sql(a, "t")
