"""Fixtures every suite under ``tests/`` can ask for."""

import asyncio
import contextlib
import itertools
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from sqler.models import mixins


@pytest.fixture
def ticking_clock(monkeypatch):
    """The clock the stamps read (``created_at``, ``updated_at``) is a millisecond later at
    every reading, whatever the machine's clock does. A test whose subject is what a second
    write stamps asks for it: Windows before Python 3.13 reads the same time for about 15 ms,
    so two writes a moment apart get one stamp there. Every reading has a fraction of a
    second, so every stamp is written in the one form a real reading almost always has."""
    readings = itertools.count()
    start = datetime(2026, 1, 1, 12, 0, 0, 1, tzinfo=timezone.utc)

    class Ticking(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(milliseconds=next(readings))

    monkeypatch.setattr(mixins, "datetime", Ticking)


@pytest.fixture
def clock_tick():
    """The step, in seconds, of the clock a wait is measured with (``time.monotonic``, which is
    also an event loop's ``time()``). A test that says a wait lasted at least its bound allows
    one step: two readings a wait apart can show up to a step less than the wait, and an event
    loop runs a timer up to a step before its time. The step is about 15.6 ms on Windows before
    Python 3.13 and under a microsecond everywhere else."""
    return time.get_clock_info("monotonic").resolution


# seconds a left-open connection gets to close (it takes milliseconds); twice, see below
CLOSE_BOUND = 3


@pytest.fixture(autouse=True)
def _no_async_database_left_open(monkeypatch):
    """aiosqlite runs each connection on a thread that is not a daemon: a test that fails
    before it closes its async database would keep the whole pytest process from ending.
    Every connection the test opens is noted as it is opened (what class aiosqlite runs it on
    differs between its releases); one the test did not close is closed when the test ends.
    A connection that cannot be closed fails the test that left it, and the others are still closed."""
    opened = []
    real_connect = aiosqlite.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", connect)
    yield
    # ``_connection`` is None once aiosqlite has closed a connection (and before it has
    # opened one); a release without the attribute is asked to close, which is always safe
    opened = [conn for conn in opened if getattr(conn, "_connection", conn) is not None]
    if not opened:
        return

    async def close_all():
        problems = []
        for conn in opened:
            closing = asyncio.ensure_future(conn.close())  # returns at once for one that is closed
            done, _ = await asyncio.wait({closing}, timeout=CLOSE_BOUND)
            if not done:
                # queued behind a statement that does not end: stop the statement, then wait again
                with contextlib.suppress(Exception):
                    conn._connection.interrupt()
                done, _ = await asyncio.wait({closing}, timeout=CLOSE_BOUND)
                problems.append(
                    f"{conn!r} was left in a statement that did not end within {CLOSE_BOUND} s"
                )
            if not done:
                closing.cancel()
            elif closing.exception() is not None:
                problems.append(
                    f"{conn!r} was left open and its close raised {closing.exception()!r}"
                )
        return problems

    # a loop of its own, not made the thread's current loop: a test's own is left as it is
    loop = asyncio.new_event_loop()
    try:
        problems = loop.run_until_complete(close_all())
    finally:
        loop.close()
    if problems:
        pytest.fail("the test left async connections open: " + "; ".join(problems), pytrace=False)
