"""Two runners that start together: the slower one finds the work done and reports success.

The slower runner reads the current version before the faster one commits. Its
``Migration.version`` is read right after that read (to pick the target), so a
migration whose ``version`` runs a hook lets the test finish the other runner at
exactly that point, with no timing involved.
"""

import asyncio
import sqlite3

import pytest

from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB
from sqler.db.sqler_db import SQLerDB
from sqler.migrations import (
    MIGRATIONS_TABLE,
    AsyncMigration,
    AsyncMigrationRunner,
    Migration,
    MigrationRunner,
)


class _Hooked:
    """Mixin: the first read of ``version`` after arming runs ``_hook`` once."""

    @property
    def version(self):
        hook = self.__dict__.get("_hook")
        if hook is not None:
            self.__dict__["_hook"] = None
            hook()
        return self.__dict__["_version"]

    @version.setter
    def version(self, value):
        self.__dict__["_version"] = value


class HookedMigration(_Hooked, Migration):
    pass


class HookedAsyncMigration(_Hooked, AsyncMigration):
    pass


def up_runs(db):
    db.adapter.execute("CREATE TABLE IF NOT EXISTS runs (m TEXT)")
    db.adapter.execute("INSERT INTO runs VALUES ('m1')")


def up_fails(db):
    db.adapter.execute("CREATE TABLE IF NOT EXISTS runs (m TEXT)")
    db.adapter.execute("INSERT INTO runs VALUES ('failed')")
    raise RuntimeError("boom")


async def aup_runs(db):
    cur = await db.adapter.execute("CREATE TABLE IF NOT EXISTS runs (m TEXT)")
    await cur.close()
    cur = await db.adapter.execute("INSERT INTO runs VALUES ('m1')")
    await cur.close()


def _inspect(path):
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs = [r[0] for r in conn.execute("SELECT m FROM runs")] if "runs" in tables else None
        versions = [r[0] for r in conn.execute(f"SELECT version FROM {MIGRATIONS_TABLE}")]
        return runs, versions
    finally:
        conn.close()


def _sync_db(path):
    return SQLerDB.on_disk(path)


def test_sync_second_runner_reports_success_and_applies_nothing(tmp_path):
    path = str(tmp_path / "m.db")
    first = MigrationRunner(_sync_db(path), [Migration(1, "m1", up_runs)])
    slow_migration = HookedMigration(1, "m1", up_runs)
    second = MigrationRunner(_sync_db(path), [slow_migration])
    first_result = []
    slow_migration._hook = lambda: first_result.append(first.migrate())

    result = second.migrate()

    assert first_result[0].success and [r.version for r in first_result[0].applied] == [1]
    assert result.success, result.error
    assert result.applied == []
    assert result.current_version == 1
    assert _inspect(path) == (["m1"], [1])


def test_async_second_runner_reports_success_and_applies_nothing(tmp_path):
    path = str(tmp_path / "m.db")
    first = MigrationRunner(_sync_db(path), [Migration(1, "m1", up_runs)])
    first_result = []

    async def scenario():
        adb = AsyncSQLerDB(AsyncSQLiteAdapter(path, pool_size=1))
        await adb.connect()
        try:
            slow_migration = HookedAsyncMigration(1, "m1", aup_runs)
            second = AsyncMigrationRunner(adb, [slow_migration])
            await second.current_version()  # the table and the version check are done
            slow_migration._hook = lambda: first_result.append(first.migrate())
            return await second.migrate()
        finally:
            await adb.close()

    result = asyncio.run(scenario())

    assert first_result[0].success
    assert result.success, result.error
    assert result.applied == []
    assert result.current_version == 1
    assert _inspect(path) == (["m1"], [1])


def test_sync_second_runner_beside_an_async_first_runner(tmp_path):
    path = str(tmp_path / "m.db")
    slow_migration = HookedMigration(1, "m1", up_runs)
    second = MigrationRunner(_sync_db(path), [slow_migration])
    first_result = []

    def run_first():
        async def go():
            adb = AsyncSQLerDB(AsyncSQLiteAdapter(path, pool_size=1))
            await adb.connect()
            try:
                return await AsyncMigrationRunner(
                    adb, [AsyncMigration(1, "m1", aup_runs)]
                ).migrate()
            finally:
                await adb.close()

        first_result.append(asyncio.run(go()))

    slow_migration._hook = run_first

    result = second.migrate()

    assert first_result[0].success
    assert result.success, result.error
    assert result.applied == []
    assert _inspect(path) == (["m1"], [1])


def test_first_runner_whose_up_raises_leaves_nothing_and_the_second_applies(tmp_path):
    path = str(tmp_path / "m.db")
    first = MigrationRunner(_sync_db(path), [Migration(1, "m1", up_fails)])
    slow_migration = HookedMigration(1, "m1", up_runs)
    second = MigrationRunner(_sync_db(path), [slow_migration])
    first_result = []
    slow_migration._hook = lambda: first_result.append(first.migrate())

    result = second.migrate()

    assert not first_result[0].success
    assert first_result[0].error == "boom"
    assert first_result[0].error_at_version == 1
    assert result.success, result.error
    assert [r.version for r in result.applied] == [1]
    assert _inspect(path) == (["m1"], [1])  # the failed run left no row and no 'failed' value


def test_a_runner_whose_up_really_fails_still_reports_it(tmp_path):
    path = str(tmp_path / "m.db")
    runner = MigrationRunner(_sync_db(path), [Migration(1, "m1", up_fails)])
    result = runner.migrate()
    assert not result.success
    assert result.error == "boom"
    assert result.error_at_version == 1
    assert result.applied == []
    runs, versions = _inspect(path)
    assert runs is None and versions == []


def test_a_gap_in_the_versions_is_refused_by_both_runners(tmp_path):
    path = str(tmp_path / "m.db")
    with pytest.raises(ValueError, match="sequential"):
        MigrationRunner(_sync_db(path), [Migration(1, "a", up_runs), Migration(3, "c", up_runs)])

    async def scenario():
        adb = AsyncSQLerDB(AsyncSQLiteAdapter(path, pool_size=1))
        await adb.connect()
        try:
            runner = AsyncMigrationRunner(
                adb, [AsyncMigration(1, "a", aup_runs), AsyncMigration(3, "c", aup_runs)]
            )
            await runner.migrate()
        finally:
            await adb.close()

    with pytest.raises(ValueError, match="sequential"):
        asyncio.run(scenario())


# --- two runners rolling back together --------------------------------------


def down_runs(db):
    db.adapter.execute("CREATE TABLE IF NOT EXISTS downs (m TEXT)")
    db.adapter.execute("INSERT INTO downs VALUES ('m1')")


async def adown_runs(db):
    cur = await db.adapter.execute("CREATE TABLE IF NOT EXISTS downs (m TEXT)")
    await cur.close()
    cur = await db.adapter.execute("INSERT INTO downs VALUES ('m1')")
    await cur.close()


def _downs(path):
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        downs = [r[0] for r in conn.execute("SELECT m FROM downs")] if "downs" in tables else []
        versions = [r[0] for r in conn.execute(f"SELECT version FROM {MIGRATIONS_TABLE}")]
        return downs, versions
    finally:
        conn.close()


def test_sync_second_rollback_skips_a_version_the_first_removed(tmp_path):
    path = str(tmp_path / "m.db")
    assert (
        MigrationRunner(_sync_db(path), [Migration(1, "m1", up_runs, down_runs)]).migrate().success
    )
    first = MigrationRunner(_sync_db(path), [Migration(1, "m1", up_runs, down_runs)])
    slow_migration = HookedMigration(1, "m1", up_runs, down_runs)
    second = MigrationRunner(_sync_db(path), [slow_migration])
    first_result = []
    slow_migration._hook = lambda: first_result.append(first.rollback())

    result = second.rollback()

    assert first_result[0].success and [r.version for r in first_result[0].rolled_back] == [1]
    assert result.success, result.error
    assert result.rolled_back == []
    assert _downs(path) == (["m1"], [])  # down() ran once


def test_async_second_rollback_skips_a_version_the_first_removed(tmp_path):
    path = str(tmp_path / "m.db")
    assert (
        MigrationRunner(_sync_db(path), [Migration(1, "m1", up_runs, down_runs)]).migrate().success
    )
    first = MigrationRunner(_sync_db(path), [Migration(1, "m1", up_runs, down_runs)])
    first_result = []

    async def scenario():
        adb = AsyncSQLerDB(AsyncSQLiteAdapter(path, pool_size=1))
        await adb.connect()
        try:
            slow_migration = HookedAsyncMigration(1, "m1", aup_runs, adown_runs)
            second = AsyncMigrationRunner(adb, [slow_migration])
            await second.current_version()
            slow_migration._hook = lambda: first_result.append(first.rollback())
            return await second.rollback()
        finally:
            await adb.close()

    result = asyncio.run(scenario())

    assert first_result[0].success
    assert result.success, result.error
    assert result.rolled_back == []
    assert _downs(path) == (["m1"], [])
