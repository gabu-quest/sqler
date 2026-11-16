"""Fixtures every suite under ``tests/`` can ask for."""

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from sqler.models import mixins


@pytest.fixture
def ticking_clock(monkeypatch):
    """The clock the stamps read (``created_at``, ``updated_at``) is a millisecond later at
    every reading, whatever the machine's clock does. A test whose subject is what a second
    write stamps asks for it: Windows before Python 3.13 reads the same time for about 15 ms,
    so two writes a moment apart get one stamp there."""
    readings = itertools.count()
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    class Ticking(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(milliseconds=next(readings))

    monkeypatch.setattr(mixins, "datetime", Ticking)
