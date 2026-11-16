"""Fixtures every suite under ``tests/`` can ask for."""

import itertools
import time
from datetime import datetime, timedelta, timezone

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
