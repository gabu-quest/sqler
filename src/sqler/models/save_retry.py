"""The retry rule shared by every safe-model save loop.

A safe save can fail for two reasons that are worth another try:

* the database is locked by another writer (``sqlite3.OperationalError``
  whose text says "locked"); the save waits and tries again, but only for
  ``RebaseConfig.lock_timeout`` seconds in total, then raises
  :class:`~sqler.exceptions.LockTimeoutError`;
* a version conflict that could be rebased; the save tries again up to
  ``RebaseConfig.max_retries`` conflicts, then raises
  ``StaleVersionError("save retries exhausted")``.

Between two tries the save sleeps at most :data:`MAX_SLEEP_S`, and it never
sleeps after its last try. Lock time is counted on a monotonic clock: the
length of every try that ended "locked" plus the sleep chosen after it.
The re-read of the row after a conflict is a try too: a lock met there is a
lock wait like any other (:meth:`SaveRetry.reread`, :meth:`SaveRetry.areread`).
Time spent on tries that ended in a conflict is not lock time.

The four save loops (sync and async, pydantic and lite) all use
:class:`SaveRetry`, so they behave the same. A rebased try writes :func:`rebase`, and
after it succeeds :func:`settle` brings the model up to what was written.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from typing import Any, Awaitable, Callable, Optional

from sqler.exceptions import LockTimeoutError, StaleVersionError

from . import _dirty, _refs
from .utils import RebaseConfig, apply_numeric_scalar_deltas

#: Longest sleep between two tries, in seconds.
MAX_SLEEP_S = 0.05
#: First sleep between two tries, in seconds; doubles per failed try.
BASE_SLEEP_S = 0.002
#: Upper bound of the random jitter added to each sleep, in seconds.
JITTER_S = 0.0005

DEFAULT_MAX_RETRIES = 128
DEFAULT_LOCK_TIMEOUT_S = 5.0

# Looked up at call time so tests can replace them.
monotonic: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep
asleep = asyncio.sleep


def is_lock_error(exc: BaseException) -> bool:
    """True when ``exc`` is SQLite saying the database or a table is locked."""
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


class SaveRetry:
    """Decides, after each failed try of one save, whether and how long to wait.

    Use one instance per save call: call :meth:`begin_try` before every try,
    then :meth:`after_lock` or :meth:`after_conflict` when the try fails. Both
    return the seconds to sleep before the next try, or raise when the save
    must give up.
    """

    def __init__(self, config: Optional[RebaseConfig]) -> None:
        self.max_retries = config.max_retries if config is not None else DEFAULT_MAX_RETRIES
        self.lock_timeout = (
            config.lock_timeout if config is not None else DEFAULT_LOCK_TIMEOUT_S
        )
        self.tries = 0
        self.conflicts = 0
        self.lock_waited = 0.0
        self._try_started = 0.0

    def begin_try(self) -> None:
        self.tries += 1
        self._try_started = monotonic()

    def _next_sleep(self) -> float:
        failed = self.tries - 1
        raw = BASE_SLEEP_S * (2 ** min(failed, 10)) + random.random() * JITTER_S
        return min(raw, MAX_SLEEP_S)

    def after_lock(self, exc: BaseException) -> float:
        """Account for a try that found the database locked.

        Returns:
            float: Seconds to sleep before the next try.

        Raises:
            LockTimeoutError: When the lock wait has used up ``lock_timeout``.
        """
        # A clock that went backwards adds nothing rather than a negative.
        self.lock_waited += max(0.0, monotonic() - self._try_started)
        remaining = self.lock_timeout - self.lock_waited
        if remaining <= 0:
            raise LockTimeoutError(
                f"save gave up: database stayed locked for {self.lock_waited:.3f}s "
                f"over {self.tries} tries (lock_timeout={self.lock_timeout}s)",
                details={
                    "waited_s": self.lock_waited,
                    "tries": self.tries,
                    "lock_timeout_s": self.lock_timeout,
                },
            ) from exc
        delay = min(self._next_sleep(), remaining)
        self.lock_waited += delay
        return delay

    def after_conflict(self) -> float:
        """Account for a version conflict that the caller is able to rebase.

        Returns:
            float: Seconds to sleep before the next try.

        Raises:
            StaleVersionError: When ``max_retries`` conflicts have happened.
        """
        self.conflicts += 1
        if self.conflicts >= self.max_retries:
            raise StaleVersionError(
                "save retries exhausted",
                details={"conflicts": self.conflicts, "tries": self.tries},
            )
        return self._next_sleep()

    def reread(self, read: Callable[[], Any]) -> Any:
        """Run ``read`` (the re-read after a conflict), waiting out lock errors.

        Each call of ``read`` is a try; a lock error is handled by
        :meth:`after_lock`, so it counts against ``lock_timeout`` and ends in
        :class:`~sqler.exceptions.LockTimeoutError`. Other errors propagate.
        """
        while True:
            self.begin_try()
            try:
                return read()
            except sqlite3.OperationalError as e:
                if not is_lock_error(e):
                    raise
                delay = self.after_lock(e)
            sleep(delay)

    async def areread(self, read: Callable[[], Awaitable[Any]]) -> Any:
        """Async twin of :meth:`reread`."""
        while True:
            self.begin_try()
            try:
                return await read()
            except sqlite3.OperationalError as e:
                if not is_lock_error(e):
                    raise
                delay = self.after_lock(e)
            await asleep(delay)

    def build(self, stale: StaleVersionError, make: Callable[[], Any]) -> Any:
        """Build the model of a rebased payload with ``make``, before anything is written.

        A lock met on the reference read is waited out like the re-read's. A row that
        cannot be loaded as this class cannot be merged: the ``stale`` conflict the caller
        caught is raised, with the validation error as its cause.
        """
        try:
            return self.reread(make)
        except ValueError as e:  # pydantic's ValidationError is one; pydantic is optional
            raise stale from e

    async def abuild(self, stale: StaleVersionError, make: Callable[[], Awaitable[Any]]) -> Any:
        """Async twin of :meth:`build`."""
        try:
            return await self.areread(make)
        except ValueError as e:  # pydantic's ValidationError is one; pydantic is optional
            raise stale from e


def rebase(latest: dict, held: dict, snap: dict, delta: dict[str, int]) -> dict:
    """The payload of a save rebased onto the newer row ``latest``.

    It is that row, with the fields the caller changed since its snapshot ``snap`` taken
    from its payload ``held``, and the counter ``delta`` added. A field only the other
    writer changed keeps the other writer's value.
    """
    out = {k: v for k, v in latest.items() if k not in ("_id", "_version")}
    for k in _dirty.changed_fields(snap, held) - delta.keys():
        if k in held:
            out[k] = held[k]
    return apply_numeric_scalar_deltas(out, delta)


def settle(inst: Any, held: dict, written: Any) -> None:
    """Bring ``inst``, whose save was rebased, up to the row it wrote.

    ``held`` is the payload of ``inst`` when save() began; ``written`` is the model built
    (:meth:`SaveRetry.build`) from the payload that was written. Each field where the two differ takes ``written``'s value;
    the others keep the objects ``inst`` holds.
    """
    for name in _dirty.changed_fields(inst._snapshot, held) & held.keys():
        _refs._set(inst, name, getattr(written, name))
