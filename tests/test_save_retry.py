"""The safe-save retry rule: bounded lock waits, bounded conflict retries.

The same cases run against all four save loops: sync and async, pydantic and
lite. Lock errors and conflicts are injected by wrapping the database's
``upsert_with_version``; sleeps are recorded by replacing the rule module's
``sleep``/``asleep``, so the tests never really wait for a lock, except the
two real-lock tests which hold a write lock from a raw sqlite3 connection.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pytest
from sqler import AsyncSQLerDB, SQLerDB
from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.adapter.synchronous import SQLiteAdapter
from sqler.exceptions import LockTimeoutError, StaleVersionError
from sqler.models import save_retry
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.model import SQLerModel
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

    This wraps the database's ``find_document_with_version`` (a real database
    cannot be made to fail a chosen number of times): it raises ``sqlite3.OperationalError(TABLE_LOCKED)``
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


# ---- a rebased save: what it writes, and what the model holds afterwards ---------------
#
# Two instances of one row, loaded through from_id: "theirs" saves first, "mine" saves on
# the stale version and is rebased. The rows hold title, count, hits, tags and a typed
# reference to a child; seeded as ("first", 10, 0, ["a"], child 1).


class RbChild(SQLerModel):
    __tablename__ = "rb_child"
    label: str = ""


class RbDoc(SQLerSafeModel):
    __tablename__ = "rb_doc"
    title: str = ""
    count: int = 0
    hits: int = 0
    tags: list[str] = []
    child: Optional[RbChild] = None
    kids: list[RbChild] = []
    meta: dict = {}
    when: Optional[date] = None


class RbPromoted(SQLerSafeModel):
    __tablename__ = "rb_promoted"
    __promoted__ = {"status": "TEXT"}
    status: str = ""
    count: int = 0


class ARbChild(AsyncSQLerModel):
    __tablename__ = "rb_child"
    label: str = ""


class ARbDoc(AsyncSQLerSafeModel):
    __tablename__ = "rb_doc"
    title: str = ""
    count: int = 0
    hits: int = 0
    tags: list[str] = []
    child: Optional[ARbChild] = None
    kids: list[ARbChild] = []
    meta: dict = {}
    when: Optional[date] = None


class ARbPromoted(AsyncSQLerSafeModel):
    __tablename__ = "rb_promoted"
    __promoted__ = {"status": "TEXT"}
    status: str = ""
    count: int = 0


@dataclass
class LRbChild(SQLerLiteModel):
    __tablename__ = "rb_child"
    label: str = ""


@dataclass
class LRbDoc(SQLerLiteSafeModel):
    __tablename__ = "rb_doc"
    title: str = ""
    count: int = 0
    hits: int = 0
    tags: list[str] = field(default_factory=list)
    child: Optional[LRbChild] = None
    kids: list[LRbChild] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    when: Optional[date] = None


@dataclass
class LRbPromoted(SQLerLiteSafeModel):
    __tablename__ = "rb_promoted"
    __promoted__ = {"status": "TEXT"}
    status: str = ""
    count: int = 0


@dataclass
class ALRbChild(AsyncSQLerLiteModel):
    __tablename__ = "rb_child"
    label: str = ""


@dataclass
class ALRbDoc(AsyncSQLerLiteSafeModel):
    __tablename__ = "rb_doc"
    title: str = ""
    count: int = 0
    hits: int = 0
    tags: list[str] = field(default_factory=list)
    child: Optional[ALRbChild] = None
    kids: list[ALRbChild] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    when: Optional[date] = None


@dataclass
class ALRbPromoted(AsyncSQLerLiteSafeModel):
    __tablename__ = "rb_promoted"
    __promoted__ = {"status": "TEXT"}
    status: str = ""
    count: int = 0


REBASE_CLASSES = {
    "sync": (RbChild, RbDoc, RbPromoted),
    "async": (ARbChild, ARbDoc, ARbPromoted),
    "lite_sync": (LRbChild, LRbDoc, LRbPromoted),
    "lite_async": (ALRbChild, ALRbDoc, ALRbPromoted),
}


async def _aw(value):
    return await value if inspect.isawaitable(value) else value


class Rows:
    """A fresh database with two children and one seeded doc, for one kind of base."""

    def __init__(self, kind: str):
        self.kind = kind
        self.child_cls, self.doc_cls, self.promoted_cls = REBASE_CLASSES[kind]
        is_async = kind in ("async", "lite_async")
        self.db = AsyncSQLerDB.in_memory() if is_async else SQLerDB.in_memory()

    async def open(self):
        await _aw(self.db.connect()) if hasattr(self.db, "connect") else None
        for cls in (self.child_cls, self.doc_cls, self.promoted_cls):
            cls.set_db(self.db)
        self.c1 = await _aw(self.child_cls(label="one").save())
        self.c2 = await _aw(self.child_cls(label="two").save())
        doc = self.doc_cls(title="first", count=10, tags=["a"], child=self.c1)
        await _aw(doc.save())
        await _aw(self.promoted_cls(status="new", count=10).save())
        return self

    async def load(self, cls=None):
        return await _aw((cls or self.doc_cls).from_id(1))

    async def row(self, table="rb_doc"):
        return await _aw(self.db.find_document(table, 1))

    async def close(self):
        await _aw(self.db.close())


def _held(doc) -> tuple:
    child = doc.child
    return (doc.title, doc.count, doc.hits, list(doc.tags), None if child is None else child._id)


def _flat(doc) -> tuple:
    return (*_held(doc), dict(doc.meta), [k._id for k in doc.kids], doc.when)


def _stored(row) -> tuple:
    child = row["child"]
    return (row["title"], row["count"], row["hits"], row["tags"], None if child is None else child["_id"])


def _rebase(kind, steps):
    """Run ``steps(rows, mine, theirs)`` on a fresh database; returns what it returns."""

    async def scenario():
        rows = await Rows(kind).open()
        mine, theirs = await rows.load(), await rows.load()
        try:
            return await steps(rows, mine, theirs)
        finally:
            await rows.close()

    return asyncio.run(scenario())


def _sets_a_date(rows, doc):
    doc.when = date(2026, 11, 14)


_THEIR_EDITS = [
    ("title", lambda r, d: setattr(d, "title", "theirs"), ("theirs", 11, 0, ["a"], 1, {}, [], None)),
    ("dict-key", lambda r, d: d.meta.update(k="v"), ("first", 11, 0, ["a"], 1, {"k": "v"}, [], None)),
    ("typed-list", lambda r, d: setattr(d, "kids", [r.c2, r.c1]), ("first", 11, 0, ["a"], 1, {}, [2, 1], None)),
    ("none", lambda r, d: setattr(d, "child", None), ("first", 11, 0, ["a"], None, {}, [], None)),
]
_THEIR_EDIT_CASES = [
    pytest.param(kind, edit, expect, id=f"{name}-{kind}")
    for name, edit, expect in _THEIR_EDITS
    for kind in KINDS
] + [
    # a date field: the pydantic base gives a date back, the lite bases the stored string;
    # the async pydantic bases cannot save one at all, so there is no case for them
    pytest.param("sync", _sets_a_date, ("first", 11, 0, ["a"], 1, {}, [], date(2026, 11, 14)), id="date-sync"),
    pytest.param("lite_sync", _sets_a_date, ("first", 11, 0, ["a"], 1, {}, [], "2026-11-14"), id="date-lite_sync"),
    pytest.param("lite_async", _sets_a_date, ("first", 11, 0, ["a"], 1, {}, [], "2026-11-14"), id="date-lite_async"),
]


@pytest.mark.parametrize("kind, edit, expect", _THEIR_EDIT_CASES)
def test_rebase_keeps_the_field_the_other_writer_changed(kind, edit, expect):
    async def steps(rows, mine, theirs):
        edit(rows, theirs)
        await _aw(theirs.save())
        mine.count += 1
        await _aw(mine.save())
        fresh = await rows.load()
        stored = await rows.row()
        return _flat(mine), _flat(fresh), stored["count"], stored["_version"]

    held, fresh, count, version = _rebase(kind, steps)
    assert held == expect
    assert fresh == expect
    assert (count, version) == (11, 2)


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_where_both_changed_a_field_to_different_values_raises_and_writes_nothing(kind):
    async def steps(rows, mine, theirs):
        theirs.title = "theirs"
        await _aw(theirs.save())
        mine.title = "mine"
        mine.count += 1
        with pytest.raises(StaleVersionError, match="^Stale version: update rejected$"):
            await _aw(mine.save())
        stored = await rows.row()
        return _stored(stored), stored["_version"], _held(mine), mine._version

    assert _rebase(kind, steps) == (
        ("theirs", 10, 0, ["a"], 1), 1, ("mine", 11, 0, ["a"], 1), 0,
    )


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_where_both_changed_a_field_to_one_value_adds_the_counter(kind):
    async def steps(rows, mine, theirs):
        theirs.title = "agreed"
        await _aw(theirs.save())
        mine.title = "agreed"
        mine.count += 1
        await _aw(mine.save())
        stored = await rows.row()
        return _stored(stored), stored["_version"], _held(mine), mine._version

    assert _rebase(kind, steps) == (
        ("agreed", 11, 0, ["a"], 1), 2, ("agreed", 11, 0, ["a"], 1), 2,
    )


def _moves_its_date(rows, doc):
    doc.when = date(2026, 11, 15)


_MY_EDITS = [
    ("list", lambda r, d: d.tags.append("b"), ("theirs", 11, 0, ["a", "b"], 1, {"k": 1}, [1], None)),
    ("dict-key", lambda r, d: d.meta.update(k=2), ("theirs", 11, 0, ["a"], 1, {"k": 2}, [1], None)),
    ("reference", lambda r, d: setattr(d, "child", r.c2), ("theirs", 11, 0, ["a"], 2, {"k": 1}, [1], None)),
    ("typed-list", lambda r, d: setattr(d, "kids", [r.c2]), ("theirs", 11, 0, ["a"], 1, {"k": 1}, [2], None)),
]
_MY_EDIT_CASES = [
    pytest.param(kind, None, edit, expect, id=f"{name}-{kind}")
    for name, edit, expect in _MY_EDITS
    for kind in KINDS
] + [
    # the async pydantic bases cannot save a date at all, so there is no case for them
    pytest.param(kind, date(2026, 11, 14), _moves_its_date,
                 ("theirs", 11, 0, ["a"], 1, {"k": 1}, [1], date(2026, 11, 15)), id=f"date-{kind}")
    for kind in ("sync", "lite_sync", "lite_async")
]


@pytest.mark.parametrize("kind, when, edit, expect", _MY_EDIT_CASES)
def test_rebase_of_a_model_never_reloaded_keeps_the_field_only_it_changed(kind, when, edit, expect):
    # its snapshot is the payload it saved, not a row it read
    async def steps(rows, _mine, _theirs):
        mine = rows.doc_cls(
            title="own", count=10, tags=["a"], child=rows.c1, kids=[rows.c1], meta={"k": 1}, when=when
        )
        await _aw(mine.save())
        theirs = await _aw(rows.doc_cls.from_id(mine._id))
        theirs.title = "theirs"
        await _aw(theirs.save())
        edit(rows, mine)
        mine.count += 1
        await _aw(mine.save())
        stored = await _aw(rows.db.find_document("rb_doc", mine._id))
        return _flat(mine), stored["title"], stored["count"], stored["_version"]

    assert _rebase(kind, steps) == (expect, "theirs", 11, 2)


@pytest.mark.parametrize("kind", KINDS)
def test_two_writers_adding_one_in_turn_keep_every_increment(kind):
    async def steps(rows, one, two):
        saved = 0
        for _ in range(5):
            for writer in (one, two):
                writer.count += 1
                try:
                    await _aw(writer.save())
                    saved += 1
                except StaleVersionError:
                    pass
        return saved, (await rows.row())["count"], (one.count, two.count)

    assert _rebase(kind, steps) == (10, 20, (19, 20))


@pytest.mark.parametrize("kind", KINDS)
def test_rebased_model_holds_what_was_written_and_a_second_save_keeps_it(kind, monkeypatch):
    async def steps(rows, mine, theirs):
        monkeypatch.setattr(
            rows.doc_cls, "_rebase_config", RebaseConfig(allowed_fields={"hits"}, max_delta=5)
        )
        seed = await rows.load()
        seed.hits = 10
        await _aw(seed.save())  # the row holds 10
        mine, theirs = await rows.load(), await rows.load()
        theirs.hits = 15
        await _aw(theirs.save())  # the row holds 15
        mine.hits = 13
        await _aw(mine.save())
        first = (await rows.row())["hits"]
        held = mine.hits
        await _aw(mine.save())
        return first, held, (await rows.row())["hits"]

    assert _rebase(kind, steps) == (18, 18, 18)


@pytest.mark.parametrize("kind", ["sync", "lite_sync", "lite_async"])
def test_rebased_model_is_not_dirty(kind):
    async def steps(rows, mine, theirs):
        theirs.title = "theirs"
        theirs.count += 1
        await _aw(theirs.save())
        mine.count += 1
        await _aw(mine.save())
        return mine.is_dirty(), mine.get_dirty_fields()

    assert _rebase(kind, steps) == (False, set())


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_keeps_a_list_the_other_writer_changed_in_place(kind):
    async def steps(rows, mine, theirs):
        theirs.tags.append("b")
        await _aw(theirs.save())
        mine.count += 1
        await _aw(mine.save())
        return _stored(await rows.row()), _held(mine)

    assert _rebase(kind, steps) == (("first", 11, 0, ["a", "b"], 1), ("first", 11, 0, ["a", "b"], 1))


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_keeps_a_reference_the_other_writer_set(kind):
    async def steps(rows, mine, theirs):
        theirs.child = rows.c2
        await _aw(theirs.save())
        mine.count += 1
        await _aw(mine.save())
        child = mine.child
        return (
            _stored(await rows.row()),
            _held(mine),
            type(child) is rows.child_cls,
            child.label,
            child._id,
        )

    assert _rebase(kind, steps) == (("first", 11, 0, ["a"], 2), ("first", 11, 0, ["a"], 2), True, "two", 2)


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_keeps_a_promoted_column_the_other_writer_changed(kind):
    async def steps(rows, mine, theirs):
        mine, theirs = await rows.load(rows.promoted_cls), await rows.load(rows.promoted_cls)
        theirs.status = "done"
        await _aw(theirs.save())
        mine.count += 1
        await _aw(mine.save())
        row = await rows.row("rb_promoted")
        fresh = await rows.load(rows.promoted_cls)
        return (row["status"], row["count"]), (mine.status, mine.count), (fresh.status, fresh.count)

    assert _rebase(kind, steps) == (("done", 11), ("done", 11), ("done", 11))


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_of_a_deleted_row_raises_stale(kind):
    async def steps(rows, mine, theirs):
        theirs.title = "theirs"
        await _aw(theirs.save())
        await _aw(theirs.delete())
        mine.count += 1
        with pytest.raises(StaleVersionError):
            await _aw(mine.save())
        return await rows.row(), _held(mine)

    assert _rebase(kind, steps) == (None, ("first", 11, 0, ["a"], 1))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda d: setattr(d, "count", d.count + 2), id="delta-above-max"),
        pytest.param(lambda d: (setattr(d, "count", d.count + 1), setattr(d, "hits", 1)), id="two-counters"),
    ],
)
def test_change_that_does_not_qualify_raises_and_touches_nothing(kind, edit, monkeypatch):
    async def steps(rows, mine, theirs):
        monkeypatch.setattr(rows.doc_cls, "_rebase_config", RebaseConfig(allowed_fields=None))
        theirs.title = "theirs"
        await _aw(theirs.save())
        edit(mine)
        held, snap = _held(mine), dict(mine._snapshot)
        with pytest.raises(StaleVersionError):
            await _aw(mine.save())
        return (
            _stored(await rows.row()),
            _held(mine) == held,
            mine._snapshot == snap,
            mine._version,
        )

    assert _rebase(kind, steps) == (("theirs", 10, 0, ["a"], 1), True, True, 0)


@pytest.mark.parametrize("kind", KINDS)
def test_three_rebases_in_a_row_keep_every_increment(kind):
    async def steps(rows, mine, theirs):
        for _ in range(3):
            other = await rows.load()
            other.count += 1
            await _aw(other.save())
            mine.count += 1
            await _aw(mine.save())
        return (await rows.row())["count"], mine.count

    assert _rebase(kind, steps) == (16, 16)


@pytest.mark.parametrize("kind", KINDS)
def test_save_that_is_not_rebased_assigns_no_field(kind):
    async def steps(rows, mine, theirs):
        tags, child = mine.tags, mine.child
        mine.count += 1
        await _aw(mine.save())
        return mine.tags is tags, mine.child is child, _stored(await rows.row())

    assert _rebase(kind, steps) == (True, True, ("first", 11, 0, ["a"], 1))


# ---- the model of the merged row is built before anything is written --------------------


_full = _flat


async def _drop_child(rows, id_):
    await _aw(rows.db.adapter.execute("DELETE FROM rb_child WHERE _id = ?", [id_]))
    await _aw(rows.db.adapter.commit())


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_onto_a_row_naming_a_deleted_child(kind):
    # the other writer saved a list naming child 2; child 2 is deleted with SQL.
    async def scenario():
        rows = await Rows(kind).open()
        try:
            mine, theirs = await rows.load(), await rows.load()
            theirs.kids = [rows.c2]
            await _aw(theirs.save())
            await _drop_child(rows, 2)
            mine.count += 1
            error = None
            try:
                await _aw(mine.save())
            except Exception as exc:  # noqa: BLE001 - the type is the assertion below
                error = exc
            row = await rows.row()
            return (
                None if error is None else (type(error).__name__, type(error.__cause__).__name__),
                row["count"],
                [k["_id"] for k in row["kids"]],
                row["_version"],
                mine.count,
                [getattr(k, "_id", None) for k in mine.kids],
                mine._version,
            )
        finally:
            await rows.close()

    # the reference to the deleted child reads as no row, so the merged model is built on
    # every base; save() writes only ``count``, and the stored list keeps what it held
    assert asyncio.run(scenario()) == (None, 11, [2], 2, 11, [], 2)


@pytest.mark.parametrize("kind", KINDS)
def test_rebase_onto_a_row_the_class_cannot_load_raises_stale_and_writes_nothing(kind):
    # the other writer left text where the class has a list of strings
    async def scenario():
        rows = await Rows(kind).open()
        try:
            mine = await rows.load()
            await _aw(
                rows.db.adapter.execute(
                    "UPDATE rb_doc SET data = json_set(data, '$.tags', 'text', '$._version', _version + 1),"
                    " _version = _version + 1 WHERE _id = 1"
                )
            )
            await _aw(rows.db.adapter.commit())
            mine.count += 1
            error = None
            try:
                await _aw(mine.save())
            except Exception as exc:  # noqa: BLE001 - the type is the assertion below
                error = exc
            row = await rows.row()
            return (
                None if error is None else (type(error).__name__, type(error.__cause__).__name__),
                row["count"],
                row["tags"],
                row["_version"],
                mine.count,
                mine._version,
            )
        finally:
            await rows.close()

    got = asyncio.run(scenario())
    if kind in ("sync", "async"):
        # the merged row is not a model of the class: the conflict is raised, nothing is written
        assert got == (("StaleVersionError", "ValidationError"), 10, "text", 1, 11, 0)
    else:
        # a lite class checks no type: the save is rebased onto the row as it is
        assert got == (None, 11, "text", 2, 11, 2)


@pytest.mark.parametrize("kind", KINDS)
def test_a_save_that_raises_on_its_second_rebase_leaves_the_model_as_it_was(kind):
    # two other writers get in during one save: the first changes a dict key (merged), the
    # second the title this writer changed too (cannot be merged)
    async def steps(rows, mine, theirs):
        db = rows.db
        real = db.upsert_with_version
        writes = [
            "json_set(data, '$.meta.k1', 1, '$._version', _version + 1)",
            "json_set(data, '$.title', 'third', '$._version', _version + 1)",
        ]

        def bump():
            return f"UPDATE rb_doc SET data = {writes.pop(0)}, _version = _version + 1 WHERE _id = 1;"

        async def wrapped(table, _id, payload, version):
            if writes and table == "rb_doc":
                await _aw(db.adapter.execute(bump()))
                await _aw(db.adapter.commit())
            return await _aw(real(table, _id, payload, version))

        if rows.kind in ("async", "lite_async"):
            db.upsert_with_version = wrapped
        else:

            def sync_wrapped(table, _id, payload, version):
                if writes and table == "rb_doc":
                    db.adapter.execute(bump())
                    db.adapter.commit()
                return real(table, _id, payload, version)

            db.upsert_with_version = sync_wrapped
        mine.title = "mine"
        mine.count += 1
        with pytest.raises(StaleVersionError, match="^Stale version: update rejected$"):
            await _aw(mine.save())
        del db.upsert_with_version
        row = await rows.row()
        return (
            (row["title"], row["count"], row["meta"], row["_version"]),
            (mine.title, mine.count, dict(mine.meta), mine._version),
            (mine._snapshot["title"], mine._snapshot["count"], mine._snapshot["meta"]),
        )

    assert _rebase(kind, steps) == (
        ("third", 10, {"k1": 1}, 2),
        ("mine", 11, {}, 0),
        ("first", 10, {}),
    )


@pytest.mark.parametrize("kind", KINDS)
def test_two_rebases_inside_one_save_keep_both_foreign_saves(kind):
    async def steps(rows, mine, theirs):
        theirs.title = "theirs"
        await _aw(theirs.save())
        db = rows.db
        real = db.upsert_with_version
        left = [2]

        def bump(n):
            return (
                f"UPDATE rb_doc SET data = json_set(data, '$.meta.k{n}', {n}, "
                f"'$._version', _version + 1), _version = _version + 1 WHERE _id = 1;"
            )

        async def wrapped(table, _id, payload, version):
            if left[0] and table == "rb_doc":
                left[0] -= 1
                await _aw(db.adapter.execute(bump(2 - left[0])))
                await _aw(db.adapter.commit())
            return await _aw(real(table, _id, payload, version))

        if rows.kind in ("async", "lite_async"):
            db.upsert_with_version = wrapped
        else:

            def sync_wrapped(table, _id, payload, version):
                if left[0] and table == "rb_doc":
                    left[0] -= 1
                    db.adapter.execute(bump(2 - left[0]))
                    db.adapter.commit()
                return real(table, _id, payload, version)

            db.upsert_with_version = sync_wrapped
        mine.count += 1
        await _aw(mine.save())
        del db.upsert_with_version
        row = await rows.row()
        return (
            (row["count"], row["meta"], row["title"], row["_version"]),
            _full(mine) == _full(await rows.load()),
            dict(mine.meta),
        )

    assert _rebase(kind, steps) == ((11, {"k1": 1, "k2": 2}, "theirs", 4), True, {"k1": 1, "k2": 2})


@pytest.mark.parametrize("kind", KINDS)
def test_lock_on_the_reference_read_of_the_merged_row_writes_it_once(kind, sleeps):
    async def steps(rows, mine, theirs):
        db = rows.db
        real = db._read_rows
        locked = [1]
        reads = [0]

        def pick():
            reads[0] += 1
            if locked[0]:
                locked[0] -= 1
                raise sqlite3.OperationalError(TABLE_LOCKED)

        if kind in ("async", "lite_async"):

            async def wrapped(*args, **kwargs):
                pick()
                return await real(*args, **kwargs)

        else:

            def wrapped(*args, **kwargs):
                pick()
                return real(*args, **kwargs)

        writes = [0]
        real_write = db.upsert_with_version

        if kind in ("async", "lite_async"):

            async def counted(*args):
                writes[0] += 1
                return await real_write(*args)

        else:

            def counted(*args):
                writes[0] += 1
                return real_write(*args)

        theirs.child = rows.c2
        await _aw(theirs.save())
        db._read_rows = wrapped
        db.upsert_with_version = counted
        mine.count += 1
        await _aw(mine.save())
        del db._read_rows
        del db.upsert_with_version
        row = await rows.row()
        # the first write is refused as stale; the second is the only one that lands
        return row["count"], row["_version"], row["child"]["_id"], mine.child._id, locked[0], writes[0]

    assert _rebase(kind, steps) == (11, 2, 2, 2, 0, 2)
