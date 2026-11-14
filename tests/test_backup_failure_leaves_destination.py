"""A failed backup leaves the destination as it was and no temporary file."""

import os
import sqlite3
import stat

import pytest
import pytest_asyncio
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.db.sqler_db import SQLerDB
from sqler.ops import async_backup, backup

_CAN_WRITE_READONLY = os.geteuid() == 0
_skip_root = pytest.mark.skipif(
    _CAN_WRITE_READONLY, reason="the process can write a read-only file (root)"
)


def _make_source(path):
    db = SQLerDB.on_disk(str(path))
    db.adapter.execute("CREATE TABLE t (x INTEGER);")
    db.adapter.execute("INSERT INTO t VALUES (1), (2), (3);")
    db.adapter.commit()
    return db


def _old_destination(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE old (y TEXT)")
    conn.execute("INSERT INTO old VALUES ('keep me')")
    conn.commit()
    conn.close()
    return path.read_bytes()


def _rows(path, sql):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_sync_success_is_a_complete_database(tmp_path):
    db = _make_source(tmp_path / "src.db")
    dest = tmp_path / "out" / "b.db"
    dest.parent.mkdir()
    result = backup(db, str(dest))
    assert result.success, result.error
    assert _rows(dest, "SELECT x FROM t ORDER BY x") == [(1,), (2,), (3,)]
    assert result.size_bytes == dest.stat().st_size
    assert sorted(os.listdir(dest.parent)) == ["b.db"]


def test_sync_missing_directory_fails_and_creates_nothing(tmp_path):
    db = _make_source(tmp_path / "src.db")
    dest = tmp_path / "nope" / "b.db"
    result = backup(db, str(dest))
    assert not result.success
    assert not (tmp_path / "nope").exists()


def test_sync_failure_leaves_absent_destination_absent(tmp_path):
    db = _make_source(tmp_path / "src.db")
    db.adapter._conn().close()  # the source connection dies under the backup
    dest = tmp_path / "b.db"
    result = backup(db, str(dest))
    assert not result.success
    assert not dest.exists()
    assert not [n for n in os.listdir(tmp_path) if n.startswith("b.db")]


def test_sync_failure_keeps_existing_destination(tmp_path):
    db = _make_source(tmp_path / "src.db")
    dest = tmp_path / "b.db"
    before = _old_destination(dest)
    db.adapter._conn().close()  # the source connection dies under the backup
    result = backup(db, str(dest))
    assert not result.success
    assert dest.read_bytes() == before
    assert _rows(dest, "SELECT y FROM old") == [("keep me",)]
    assert not [n for n in os.listdir(tmp_path) if n.startswith("b.db") and n != "b.db"]


@_skip_root
def test_sync_read_only_destination_fails_and_is_unchanged(tmp_path):
    db = _make_source(tmp_path / "src.db")
    dest = tmp_path / "b.db"
    before = _old_destination(dest)
    dest.chmod(stat.S_IRUSR)
    try:
        result = backup(db, str(dest))
    finally:
        dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert not result.success
    assert dest.read_bytes() == before
    assert not [n for n in os.listdir(tmp_path) if n.startswith("b.db") and n != "b.db"]


@pytest_asyncio.fixture
async def adb(tmp_path):
    d = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / "asrc.db"), pool_size=1))
    await d.connect()
    cur = await d.adapter.execute("CREATE TABLE t (x INTEGER);")
    await cur.close()
    cur = await d.adapter.execute("INSERT INTO t VALUES (1), (2), (3);")
    await cur.close()
    await d.adapter.commit()
    try:
        yield d
    finally:
        await d.close()


@pytest.mark.asyncio
async def test_async_success_is_a_complete_database(adb, tmp_path):
    dest = tmp_path / "b.db"
    result = await async_backup(adb, str(dest))
    assert result.success, result.error
    assert _rows(dest, "SELECT x FROM t ORDER BY x") == [(1,), (2,), (3,)]
    assert not [n for n in os.listdir(tmp_path) if n.startswith("b.db") and n != "b.db"]


@pytest.mark.asyncio
async def test_async_missing_directory_fails_and_creates_nothing(adb, tmp_path):
    result = await async_backup(adb, str(tmp_path / "nope" / "b.db"))
    assert not result.success
    assert not (tmp_path / "nope").exists()


@_skip_root
@pytest.mark.asyncio
async def test_async_read_only_destination_fails_and_is_unchanged(adb, tmp_path):
    dest = tmp_path / "b.db"
    before = _old_destination(dest)
    dest.chmod(stat.S_IRUSR)
    try:
        result = await async_backup(adb, str(dest))
    finally:
        dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert not result.success
    assert dest.read_bytes() == before
    assert not [n for n in os.listdir(tmp_path) if n.startswith("b.db") and n != "b.db"]


@pytest.mark.asyncio
async def test_async_failure_leaves_absent_destination_absent(adb, tmp_path):
    """A destination that cannot be completed (a directory) is not created or half-written."""
    dest = tmp_path / "isdir"
    dest.mkdir()
    result = await async_backup(adb, str(dest))
    assert not result.success
    assert dest.is_dir()
    assert os.listdir(dest) == []
    assert not [n for n in os.listdir(tmp_path) if n.startswith("isdir") and n != "isdir"]


@pytest.mark.asyncio
async def test_async_closed_source_leaves_absent_destination_absent(adb, tmp_path):
    conn = await adb.adapter._acquire()
    await conn.close()  # the source connection dies under the backup
    await adb.adapter._release()
    dest = tmp_path / "b.db"
    result = await async_backup(adb, str(dest))
    assert not result.success
    assert not dest.exists()
    assert not [n for n in os.listdir(tmp_path) if n.startswith("b.db")]


@pytest.mark.asyncio
async def test_async_closed_source_keeps_existing_destination(adb, tmp_path):
    dest = tmp_path / "b.db"
    before = _old_destination(dest)
    conn = await adb.adapter._acquire()
    await conn.close()
    await adb.adapter._release()
    result = await async_backup(adb, str(dest))
    assert not result.success
    assert dest.read_bytes() == before
    assert not [n for n in os.listdir(tmp_path) if n.startswith("b.db") and n != "b.db"]


def test_sync_symlink_destination_writes_the_target(tmp_path):
    db = _make_source(tmp_path / "src.db")
    other = tmp_path / "other"
    other.mkdir()
    target = other / "real.db"
    target.write_bytes(b"")
    link = tmp_path / "link.db"
    link.symlink_to(target)
    result = backup(db, str(link))
    assert result.success, result.error
    assert link.is_symlink()
    assert _rows(target, "SELECT x FROM t ORDER BY x") == [(1,), (2,), (3,)]
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert os.listdir(other) == ["real.db"]


def test_sync_existing_destination_keeps_its_mode(tmp_path):
    db = _make_source(tmp_path / "src.db")
    dest = tmp_path / "b.db"
    _old_destination(dest)
    dest.chmod(0o600)
    result = backup(db, str(dest))
    assert result.success, result.error
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert _rows(dest, "SELECT x FROM t ORDER BY x") == [(1,), (2,), (3,)]


@pytest.mark.asyncio
async def test_async_symlink_destination_writes_the_target(adb, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    target = other / "real.db"
    target.write_bytes(b"")
    link = tmp_path / "link.db"
    link.symlink_to(target)
    result = await async_backup(adb, str(link))
    assert result.success, result.error
    assert link.is_symlink()
    assert _rows(target, "SELECT x FROM t ORDER BY x") == [(1,), (2,), (3,)]
    assert os.listdir(other) == ["real.db"]
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


@pytest.mark.asyncio
async def test_async_existing_destination_keeps_its_mode(adb, tmp_path):
    dest = tmp_path / "b.db"
    _old_destination(dest)
    dest.chmod(0o600)
    result = await async_backup(adb, str(dest))
    assert result.success, result.error
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
