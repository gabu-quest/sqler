"""The safe-save retry rule: bounded lock waits, bounded conflict retries.

The same cases run against all four save loops: sync and async, pydantic and
lite. Lock errors and conflicts are injected by wrapping the database's
``upsert_with_version``; sleeps are recorded by replacing the rule module's
``sleep``/``asleep``, so the tests never really wait for a lock, except the
two real-lock tests which hold a write lock from a raw sqlite3 connection.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass

import pytest
from sqler import AsyncSQLerDB, SQLerDB
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.adapter.synchronous import SQLiteAdapter
from sqler.exceptions import LockTimeoutError, StaleVersionError
from sqler.models import save_retry
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.safe import SQLerSafeModel
from sqler.models.utils import NO_REBASE_CONFIG, RebaseConfig

KINDS = ["sync", "async", "lite_sync", "lite_async"]
LOCKED = "database is locked"


def _model_class(kind: str, config: RebaseConfig):
    if kind == "sync":

        class Counter(SQLerSafeModel):
            name: str
            count: int = 0

    elif kind == "async":

        class Counter(AsyncSQLerSafeModel):  # type: ignore[no-redef]
            name: str
            count: int = 0

    elif kind == "lite_sync":

        @dataclass
        class Counter(SQLerLiteSafeModel):  # type: ignore[no-redef]
            __tablename__ = "counter"
            name: str = ""
            count: int = 0

    else:

        @dataclass
        class Counter(AsyncSQLerLiteSafeModel):  # type: ignore[no-redef]
            __tablename__ = "counter"
            name: str = ""
            count: int = 0

    Counter._rebase_config = config
    return Counter


class Harness:
    """One model class bound to one database, driven from a coroutine."""

    def __init__(self, kind: str, db, config: RebaseConfig):
        self.kind = kind
        self.is_async = kind in ("async", "lite_async")
        self.db = db
        self.model = _model_class(kind, config)
        self.model.set_db(db)

    async def call(self, fn, *args):
        result = fn(*args)
        if self.is_async:
            result = await result
        return result

    async def save(self, obj):
        return await self.call(obj.save)

    async def load(self, _id):
        return await self.call(self.model.from_id, _id)

    def inject(self, failures: list):
        """Make the next upserts raise from ``failures`` in order, then pass.

        Each entry is "lock" or "conflict". Returns the call counter list.
        """
        db = self.db
        real = db.upsert_with_version
        queue = list(failures)
        calls = [0]

        def pick():
            calls[0] += 1
            if queue:
                what = queue.pop(0)
                if what == "lock":
                    raise sqlite3.OperationalError(LOCKED)
                return "conflict"
            return None

        def bump(table, _id):
            # A real conflict: someone else saved the row meanwhile.
            return (
                f"UPDATE {table} SET data = json_set(data, '$.count', "
                f"json_extract(data, '$.count') + 1, '$._version', _version + 1), "
                f"_version = _version + 1 "
                f"WHERE _id = ?;"
            )

        if self.is_async:

            async def wrapped(table, _id, payload, version):
                if pick() == "conflict":
                    await db.adapter.execute(bump(table, _id), [_id])
                    await db.adapter.commit()
                return await real(table, _id, payload, version)

        else:

            def wrapped(table, _id, payload, version):
                if pick() == "conflict":
                    db.adapter.execute(bump(table, _id), [_id])
                    db.adapter.commit()
                return real(table, _id, payload, version)

        db.upsert_with_version = wrapped
        return calls


@pytest.fixture
def sleeps(monkeypatch):
    """Record every sleep a save asks for instead of sleeping."""
    recorded: list[float] = []

    def fake_sleep(seconds):
        recorded.append(seconds)

    async def fake_asleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(save_retry, "sleep", fake_sleep)
    monkeypatch.setattr(save_retry, "asleep", fake_asleep)
    return recorded


async def _open(kind: str, config: RebaseConfig, path=None):
    is_async = kind in ("async", "lite_async")
    if path is None:
        db = AsyncSQLerDB.in_memory() if is_async else SQLerDB.in_memory()
    else:
        # busy_timeout 0: a locked write fails at once, so the rule does the waiting.
        pragmas = ["PRAGMA busy_timeout = 0"]
        db = (
            AsyncSQLerDB(AsyncSQLiteAdapter(str(path), pragmas=pragmas))
            if is_async
            else SQLerDB(SQLiteAdapter(str(path), pragmas=pragmas))
        )
    if is_async:
        await db.connect()
    return Harness(kind, db, config)


async def _close(h: Harness):
    await h.call(h.db.close)


async def _seeded(kind, config, path=None):
    h = await _open(kind, config, path)
    obj = h.model(name="c", count=0)
    await h.save(obj)
    obj.count += 1
    return h, obj


@pytest.mark.parametrize("kind", KINDS)
def test_lock_then_success_sleeps_short(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig())
        calls = h.inject(["lock"] * 8)
        await h.save(obj)
        await _close(h)
        return calls[0], obj._version

    calls, version = asyncio.run(scenario())
    assert (calls, version) == (9, 1)
    assert len(sleeps) == 8
    assert max(sleeps) <= save_retry.MAX_SLEEP_S


@pytest.mark.parametrize("kind", KINDS)
def test_lock_timeout_zero_is_one_try(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig(lock_timeout=0))
        calls = h.inject(["lock"] * 1000)
        with pytest.raises(LockTimeoutError, match="over 1 tries"):
            await h.save(obj)
        h.db.upsert_with_version = type(h.db).upsert_with_version.__get__(h.db)
        row = await h.load(obj._id)
        await _close(h)
        return calls[0], row.count, row._version

    assert asyncio.run(scenario()) == (1, 0, 0)
    assert sleeps == []


@pytest.mark.parametrize("kind", KINDS)
def test_lock_wait_is_bounded_by_time_not_count(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig(lock_timeout=0.3, max_retries=2))
        calls = h.inject(["lock"] * 100000)
        with pytest.raises(LockTimeoutError) as info:
            await h.save(obj)
        await _close(h)
        return calls[0], info.value

    calls, err = asyncio.run(scenario())
    # More tries than max_retries: lock waits are not counted against it.
    assert calls == err.details["tries"]
    assert calls > 2
    # One sleep between each two tries, none after the last.
    assert len(sleeps) == calls - 1
    assert max(sleeps) <= save_retry.MAX_SLEEP_S
    assert err.details["waited_s"] >= 0.3
    assert f"over {calls} tries" in str(err)


@pytest.mark.parametrize("kind", KINDS)
def test_conflict_with_max_retries_one_raises_without_sleep(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig(max_retries=1))
        calls = h.inject(["conflict"])
        with pytest.raises(StaleVersionError, match="save retries exhausted"):
            await h.save(obj)
        await _close(h)
        return calls[0]

    assert asyncio.run(scenario()) == 1
    assert sleeps == []


@pytest.mark.parametrize("kind", KINDS)
def test_conflict_rebased_then_saved(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig())
        calls = h.inject(["conflict", "conflict"])
        await h.save(obj)
        row = await h.load(obj._id)
        await _close(h)
        return calls[0], row.count, row._version

    # Two foreign increments plus ours.
    assert asyncio.run(scenario()) == (3, 3, 3)
    assert len(sleeps) == 2


@pytest.mark.parametrize("kind", KINDS)
def test_conflict_not_rebasable_raises_at_once(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, NO_REBASE_CONFIG)
        calls = h.inject(["conflict"])
        with pytest.raises(StaleVersionError):
            await h.save(obj)
        await _close(h)
        return calls[0]

    assert asyncio.run(scenario()) == 1
    assert sleeps == []


@pytest.mark.parametrize("kind", KINDS)
def test_conflicts_exhaust_even_with_locks_between(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig(max_retries=3, lock_timeout=60))
        calls = h.inject(["lock", "conflict", "lock", "conflict", "lock", "conflict"])
        with pytest.raises(StaleVersionError, match="save retries exhausted"):
            await h.save(obj)
        await _close(h)
        return calls[0]

    assert asyncio.run(scenario()) == 6
    assert len(sleeps) == 5


def _hold_write_lock(path):
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


@pytest.mark.parametrize("kind", KINDS)
def test_real_lock_longer_than_bound_raises_in_time(kind, tmp_path):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig(lock_timeout=0.5), tmp_path / "x.db")
        holder = _hold_write_lock(tmp_path / "x.db")
        try:
            start = time.monotonic()
            with pytest.raises(LockTimeoutError):
                await h.save(obj)
            took = time.monotonic() - start
        finally:
            holder.rollback()
            holder.close()
        row = await h.load(obj._id)
        await _close(h)
        return took, row.count, row._version

    took, count, version = asyncio.run(scenario())
    assert took < 0.5 + 1.0
    assert (count, version) == (0, 0)


@pytest.mark.parametrize("kind", KINDS)
def test_real_lock_released_after_100ms_save_succeeds(kind, tmp_path):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig(lock_timeout=5), tmp_path / "x.db")
        holder = _hold_write_lock(tmp_path / "x.db")
        release = threading.Timer(0.1, holder.rollback)
        release.start()
        try:
            await h.save(obj)
        finally:
            release.join()
            holder.close()
        row = await h.load(obj._id)
        await _close(h)
        return row.count, row._version

    assert asyncio.run(scenario()) == (1, 1)


def test_ten_async_tasks_save_one_row():
    async def scenario():
        h = await _open("async", RebaseConfig())
        first = h.model(name="c", count=0)
        await h.save(first)
        ok = [0]

        async def bump():
            for _ in range(5):
                c = await h.load(first._id)
                c.count += 1
                try:
                    await h.save(c)
                    ok[0] += 1
                except (LockTimeoutError, StaleVersionError):
                    pass

        await asyncio.wait_for(asyncio.gather(*(bump() for _ in range(10))), 10)
        row = await h.load(first._id)
        await _close(h)
        return row.count, ok[0]

    count, ok = asyncio.run(scenario())
    assert count == ok
    assert ok == 50


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(save_retry, "monotonic", c)
    return c


def test_rule_counts_only_lock_time(clock):
    rule = save_retry.SaveRetry(RebaseConfig(lock_timeout=1.0, max_retries=100))
    # Ten pairs: a lock try of 0.01 s, then a conflict try of 10 s.
    for _ in range(10):
        rule.begin_try()
        clock.now += 0.01
        rule.after_lock(sqlite3.OperationalError(LOCKED))
        rule.begin_try()
        clock.now += 10
        rule.after_conflict()
    assert rule.lock_waited < 1.0
    assert rule.conflicts == 10


def test_rule_clock_backwards_adds_nothing(clock):
    rule = save_retry.SaveRetry(RebaseConfig(lock_timeout=0.2))
    delays = []
    with pytest.raises(LockTimeoutError):
        for _ in range(1000):
            rule.begin_try()
            clock.now -= 100
            delays.append(rule.after_lock(sqlite3.OperationalError(LOCKED)))
    # Only the sleeps were credited; they sum to the bound.
    assert sum(delays) == pytest.approx(0.2)
    assert max(delays) <= save_retry.MAX_SLEEP_S


def test_rule_default_config_matches_rebase_config():
    rule = save_retry.SaveRetry(None)
    assert (rule.max_retries, rule.lock_timeout) == (128, 5.0)
    assert RebaseConfig().lock_timeout == 5.0


def test_lock_timeout_error_is_exported_from_the_package():
    import sqler

    assert sqler.LockTimeoutError is LockTimeoutError
    assert "LockTimeoutError" in sqler.__all__


@pytest.mark.parametrize("kind", KINDS)
def test_longest_wait_is_the_limit_plus_one_busy_timeout(kind, tmp_path):
    """A try blocks inside SQLite for up to one busy timeout before the limit is looked at.

    lock_timeout 0.3 s and busy_timeout 0.4 s: the first try waits the whole
    0.4 s, which already uses up the limit, so the save gives up after one try,
    having waited more than the limit but not more than limit + busy timeout.
    """
    busy_s, limit_s = 0.4, 0.3

    async def scenario():
        is_async = kind in ("async", "lite_async")
        path = str(tmp_path / "x.db")
        pragmas = [f"PRAGMA busy_timeout = {int(busy_s * 1000)}", "PRAGMA journal_mode = WAL"]
        db = (
            AsyncSQLerDB(AsyncSQLiteAdapter(path, pragmas=pragmas))
            if is_async
            else SQLerDB(SQLiteAdapter(path, pragmas=pragmas))
        )
        if is_async:
            await db.connect()
        h = Harness(kind, db, RebaseConfig(lock_timeout=limit_s))
        obj = h.model(name="c", count=0)
        await h.save(obj)
        obj.count += 1
        holder = _hold_write_lock(path)
        try:
            start = time.monotonic()
            with pytest.raises(LockTimeoutError) as info:
                await h.save(obj)
            took = time.monotonic() - start
        finally:
            holder.rollback()
            holder.close()
        row = await h.load(obj._id)
        await _close(h)
        return took, info.value.details["tries"], row.count

    took, tries, count = asyncio.run(scenario())
    assert tries == 1
    assert busy_s - 0.05 <= took <= limit_s + busy_s
    assert count == 0


TABLE_LOCKED = "database table is locked"


def _fail_rereads(h: Harness, times: int) -> list:
    """Make the next ``times`` calls of ``find_document_with_version`` raise "locked".

    This wraps the database's ``find_document_with_version`` (allowed for this
    case by the brief): it raises ``sqlite3.OperationalError(TABLE_LOCKED)``
    ``times`` times, then passes each call through to the real method.
    """
    db = h.db
    real = db.find_document_with_version
    left = [times]
    calls = [0]

    def pick():
        calls[0] += 1
        if left[0] > 0:
            left[0] -= 1
            raise sqlite3.OperationalError(TABLE_LOCKED)

    if h.is_async:

        async def wrapped(table, _id):
            pick()
            return await real(table, _id)

    else:

        def wrapped(table, _id):
            pick()
            return real(table, _id)

    db.find_document_with_version = wrapped
    return calls


@pytest.mark.parametrize("kind", KINDS)
def test_lock_on_reread_once_then_save_succeeds(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig())
        h.inject(["conflict"])
        reads = _fail_rereads(h, 1)
        await h.save(obj)
        del h.db.find_document_with_version
        row = await h.load(obj._id)
        await _close(h)
        return reads[0], row.count, row._version

    # One foreign increment plus ours; the re-read ran twice.
    assert asyncio.run(scenario()) == (2, 2, 2)
    assert max(sleeps) <= save_retry.MAX_SLEEP_S


@pytest.mark.parametrize("kind", KINDS)
def test_lock_on_reread_until_limit_raises_lock_timeout(kind, sleeps):
    async def scenario():
        h, obj = await _seeded(kind, RebaseConfig(lock_timeout=0.3))
        h.inject(["conflict"])
        _fail_rereads(h, 10**9)
        with pytest.raises(LockTimeoutError) as info:
            await h.save(obj)
        await _close(h)
        return info.value

    err = asyncio.run(scenario())
    assert err.details["waited_s"] >= 0.3
    assert max(sleeps) <= save_retry.MAX_SLEEP_S
