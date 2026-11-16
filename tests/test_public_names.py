"""The package's public names: the thread-pooled database is gone, ``PoolStats`` stays, and
an async class has every public name its sync twin has.

``sqler.PoolStats`` is the class the async pool's ``pool_stats()`` returns. The
removed names are checked in a fresh interpreter, so a module imported earlier in
the test run, or a stale ``__pycache__`` entry, cannot make them appear to exist.
"""

import pickle
import subprocess
import sys
import textwrap
from datetime import datetime

import pytest
import sqler
from sqler import SQLerError
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.db.async_db import AsyncSQLerDB

REMOVED = ("PooledSQLerDB", "ConnectionPool", "PooledSQLiteAdapter")


def _run_fresh(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=60
    )


def test_removed_names_are_not_exported():
    for name in REMOVED:
        assert name not in sqler.__all__
        assert not hasattr(sqler, name)


def test_the_pool_module_does_not_import_in_a_fresh_interpreter():
    result = _run_fresh(
        """
        import importlib.util
        try:
            import sqler.pool
        except ModuleNotFoundError as error:
            assert error.name == "sqler.pool", error.name
        else:
            raise SystemExit("sqler.pool imported")
        assert importlib.util.find_spec("sqler.pool") is None
        for name in ("PooledSQLerDB", "ConnectionPool", "PooledSQLiteAdapter"):
            try:
                exec(f"from sqler import {name}")
            except ImportError:
                pass
            else:
                raise SystemExit(name + " imported")
        print("ok")
        """
    )
    assert (result.returncode, result.stdout.strip()) == (0, "ok"), result.stderr


def test_star_import_brings_poolstats_and_none_of_the_removed_names():
    result = _run_fresh(
        """
        from sqler import *
        names = set(globals())
        assert "PoolStats" in names
        assert not names & {"PooledSQLerDB", "ConnectionPool", "PooledSQLiteAdapter"}, names
        print("ok")
        """
    )
    assert (result.returncode, result.stdout.strip()) == (0, "ok"), result.stderr


@pytest.mark.asyncio
async def test_poolstats_is_the_class_pool_stats_returns(tmp_path):
    adapter = AsyncSQLiteAdapter.on_disk(str(tmp_path / "a.db"))
    await adapter.connect()
    db = AsyncSQLerDB.on_disk(str(tmp_path / "b.db"))
    await db.connect()
    try:
        assert type(adapter.pool_stats()) is sqler.PoolStats
        assert type(db.pool_stats()) is sqler.PoolStats
    finally:
        await db.close()
        await adapter.close()


def test_poolstats_pickles_under_its_module_path():
    stats = sqler.PoolStats(
        total_connections=2,
        available_connections=1,
        in_use_connections=1,
        max_connections=2,
        total_checkouts=3,
        total_timeouts=0,
        avg_wait_time_ms=0.5,
        max_wait_time_ms=1.0,
        created_at=datetime(2026, 1, 1),
    )
    assert sqler.PoolStats.__module__ == "sqler.adapter.asynchronous"
    assert pickle.loads(pickle.dumps(stats)) == stats


# ---- an async class has every public name of its sync twin ----------------------------------

TWINS = [
    ("SQLerDB", "AsyncSQLerDB"),
    ("SQLiteAdapter", "AsyncSQLiteAdapter"),
    ("SQLerQuery", "AsyncSQLerQuery"),
    ("SQLerQuerySet", "AsyncSQLerQuerySet"),
    ("SQLerModel", "AsyncSQLerModel"),
    ("SQLerSafeModel", "AsyncSQLerSafeModel"),
    ("SQLerLiteModel", "AsyncSQLerLiteModel"),
    ("SQLerLiteSafeModel", "AsyncSQLerLiteSafeModel"),
    ("MigrationRunner", "AsyncMigrationRunner"),
    ("Migration", "AsyncMigration"),
    ("TrackedModel", "AsyncTrackedModel"),
    ("PartialUpdateMixin", "AsyncPartialUpdateMixin"),
    ("HooksMixin", "AsyncHooksMixin"),
    ("AuditLogMixin", "AsyncAuditLogMixin"),
]


@pytest.mark.parametrize("sync_name, async_name", TWINS)
def test_an_async_class_has_every_public_name_of_its_sync_twin(sync_name, async_name):
    assert {sync_name, async_name} <= set(sqler.__all__)
    sync_cls, async_cls = getattr(sqler, sync_name), getattr(sqler, async_name)
    theirs = {n for n in dir(async_cls) if not n.startswith("_")}
    missing = sorted(
        n for n in dir(sync_cls) if not n.startswith("_") and n not in theirs and "a" + n not in theirs
    )
    assert missing == []


class TestExceptionHierarchy:
    """Tests for the exception hierarchy."""

    def test_sqler_error_base(self):
        """Test SQLerError base class."""
        error = SQLerError("Test error", details={"key": "value"})

        assert error.message == "Test error"
        assert error.details == {"key": "value"}
