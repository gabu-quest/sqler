"""The safe-model save: its one loop and the retry rule it follows.

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
lock wait like any other (:meth:`SaveRetry.reread`); so is a lock met while the
model of the rebased payload is built (:meth:`SaveRetry.build`).
Time spent on tries that ended in a conflict is not lock time.

The save is written once for all four safe bases (sync and async, pydantic and
lite). One try is the calls :func:`_try` names and, when its write lands,
:func:`_landed`. :func:`run` makes the first try with plain calls and :func:`arun`
awaits them; most saves end there. A try that fails goes on in the generator
:func:`save_steps`, which holds the loop: where it needs the database, the
reference reads or a sleep, it yields a step ``(fn, args)`` and is sent the result
back; an error the call raises is thrown into it at that point, so each ``except``
arm sits in the loop once. A rebased try writes :func:`rebase`, and after it
succeeds :func:`settle` brings the model up to what was written.
"""

from __future__ import annotations

import asyncio
import functools
import random
import sqlite3
import time
from typing import Any, Callable, Generator, Optional

from sqler.exceptions import LockTimeoutError, StaleVersionError

from . import _dirty, _refs
from .utils import (
    DEFAULT_REBASE_CONFIG,
    RebaseConfig,
    apply_numeric_scalar_deltas,
    can_rebase_deltas,
    compute_numeric_scalar_deltas,
)

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

#: One request of the save loop: call ``fn(*args)`` (await it on the async side) and send
#: the result back. ``fn`` is :func:`pause` for a sleep.
Step = tuple[Callable[..., Any], tuple[Any, ...]]
Steps = Generator[Step, Any, Any]


def pause(seconds: float) -> None:
    """The step that sleeps ``seconds``; each driver sleeps its own way (:data:`sleep`,
    :data:`asleep`), so the step itself is never called."""
    raise AssertionError("a pause step is performed by the driver")


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

    def reread(self, fn: Callable[..., Any], *args: Any) -> Steps:
        """The steps of ``fn(*args)`` (the re-read after a conflict), waiting out lock errors.

        Each call of ``fn`` is a try; a lock error is handled by :meth:`after_lock`, so it
        counts against ``lock_timeout`` and ends in
        :class:`~sqler.exceptions.LockTimeoutError`. Other errors propagate.
        """
        while True:
            self.begin_try()
            try:
                return (yield fn, args)
            except sqlite3.OperationalError as e:
                if not is_lock_error(e):
                    raise
                delay = self.after_lock(e)
            yield pause, (delay,)

    def build(
        self, stale: StaleVersionError, cls: Any, resolve: Callable[..., Any], payload: dict, db: Any
    ) -> Steps:
        """The steps that build the model of the rebased ``payload``, before anything is written.

        ``resolve(payload, db, cls)`` reads the rows the payload's references name; a lock
        met there is waited out like the re-read's. A row that cannot be loaded as this
        class cannot be merged: the ``stale`` conflict the caller caught is raised, with
        the validation error as its cause.
        """
        try:
            resolved = yield from self.reread(resolve, payload, db, cls)
            return cls.model_validate(resolved)
        except ValueError as e:  # pydantic's ValidationError is one; pydantic is optional
            raise stale from e


def rebase(
    latest: dict, held: dict, snap: dict, delta: dict[str, int], stale: StaleVersionError, stamped: Any = ()
) -> dict:
    """The payload of a save rebased onto the newer row ``latest``.

    It is that row, with the fields the caller changed since its snapshot ``snap`` taken
    from its payload ``held``, and the counter ``delta`` added. A field only the other
    writer changed keeps the other writer's value. A field both changed, to two different
    values, cannot be merged: the ``stale`` conflict the caller caught is raised. A field in
    ``stamped`` (what every save of the class stamps, ``_update_stamps()``: ``updated_at``)
    is no such field: each save moved it, and this one's stamp is the one that stays.
    """
    out = {k: v for k, v in latest.items() if k not in ("_id", "_version")}
    mine = (_dirty.changed_fields(snap, held) - delta.keys()) & held.keys()
    both = mine & _dirty.changed_fields(snap, out) & _dirty.changed_fields(out, held)
    if both - set(stamped):
        raise stale
    for k in mine:
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


def _try(cls: Any, inst: Any, db: Any, table: str, partial: bool, payload: dict, base: Any, version: Any) -> tuple[Step, ...]:
    """The calls of one try, in order: the promoted columns ensured where the class has any,
    then the write of ``payload`` at ``version`` (only the fields changed since ``base`` when
    ``partial``)."""
    promoted = getattr(cls, "__promoted__", None)
    if partial:
        changed = _dirty.changed_fields(base or {}, payload)
        write: Step = db._update_fields, (table, inst._id, payload, changed, promoted or (), version)
    elif promoted:
        write = db.upsert_with_version_promoted, (table, inst._id, payload, version, list(promoted.keys()))
    else:
        return ((db.upsert_with_version, (table, inst._id, payload, version)),)
    if not promoted:
        return (write,)
    ensure = functools.partial(db._ensure_promoted, model=cls)
    return (ensure, (table, promoted, getattr(cls, "__checks__", None))), write


def _landed(inst: Any, assign: Callable[[Any, str, Any], None], partial: bool, payload: dict, result: Any) -> Any:
    """``inst`` after its write landed: at the id and version the write gave (``result``, what
    the last call of :func:`_try` returned), with ``payload`` as its snapshot."""
    new_id, new_version = (inst._id, result) if partial else result
    assign(inst, "_id", new_id)
    assign(inst, "_version", new_version)
    assign(inst, "_snapshot", _dirty.snapshot(payload))
    return inst


def _begin(inst: Any, db: Any, table: str, partial: bool, held: dict) -> tuple[SaveRetry, Any, tuple[Step, ...]]:
    """The opening of one safe save: its retry state with the first try begun, the snapshot
    the model holds, and the calls of that first try."""
    cls = type(inst)
    retry = SaveRetry(getattr(cls, "_rebase_config", DEFAULT_REBASE_CONFIG))
    snap = getattr(inst, "_snapshot", None)
    retry.begin_try()
    return retry, snap, _try(cls, inst, db, table, partial, held, snap, inst._version)


def save_steps(
    inst: Any,
    db: Any,
    table: str,
    partial: bool,
    held: dict,
    snap: Any,
    retry: SaveRetry,
    failed: BaseException,
    resolve: Callable[[dict, Any, Any], Any],
    assign: Callable[[Any, str, Any], None],
) -> Steps:
    """The steps of a safe save of ``inst`` from the point its first try failed with ``failed``.

    ``held`` is the payload save() writes and ``snap`` the snapshot the model held when it
    began; ``retry`` has counted the first try; ``resolve(payload, db, cls)`` is the payload
    with the model of each reference in place; ``assign(inst, name, value)`` sets ``_id``,
    ``_version`` and ``_snapshot``. The model keeps its own version and snapshot until a write
    succeeds.

    A try that found the database locked waits (:meth:`SaveRetry.after_lock`). A stale version
    that qualifies for rebasing (``_rebase_config``) re-reads the row, rebases the caller's
    counter delta onto it and builds the model of that payload (:meth:`SaveRetry.after_conflict`);
    one that does not qualify, or a row that is gone, is raised as it came, and so is any other
    error. Then the save sleeps and tries again (:func:`_try`): a write that lands sets the
    instance (:func:`_landed`), settles it when the try was rebased, and ends the save.
    """
    cls = type(inst)
    config = getattr(cls, "_rebase_config", DEFAULT_REBASE_CONFIG)
    delta: Optional[dict[str, int]] = None
    can_rebase: Optional[bool] = None  # worked out at the first conflict
    payload, written = held, None
    version, base = inst._version, snap
    error: Optional[BaseException] = failed
    while True:
        try:
            if error is not None:  # the try the driver made: its error meets the arms below
                first, error = error, None
                raise first
            retry.begin_try()
            for step in _try(cls, inst, db, table, partial, payload, base, version):
                result = yield step
            _landed(inst, assign, partial, payload, result)
            if written is not None:  # rebased: the row holds the other writer's work too
                settle(inst, held, written)
            return inst
        except sqlite3.OperationalError as e:
            if not is_lock_error(e):
                raise
            delay = retry.after_lock(e)
        except StaleVersionError as stale:
            if can_rebase is None:  # the caller's intent: the counter deltas from its snapshot to its payload
                delta = compute_numeric_scalar_deltas(snap, held) if snap else None
                can_rebase = bool(delta and can_rebase_deltas(delta, config))
            if not can_rebase or inst._id is None:
                raise
            delay = retry.after_conflict()
            latest = yield from retry.reread(db.find_document_with_version, table, inst._id)
            if latest is None:
                raise
            assert snap is not None and delta is not None  # can_rebase holds only with both
            payload = rebase(latest, held, snap, delta, stale, getattr(cls, "_update_stamps", dict)())
            written = yield from retry.build(stale, cls, resolve, payload, db)
            version, base = latest.get("_version", 0), _dirty.snapshot(latest)
        yield pause, (delay,)


def run(
    inst: Any,
    db: Any,
    table: str,
    partial: bool,
    held: dict,
    resolve: Callable[[dict, Any, Any], Any],
    assign: Callable[[Any, str, Any], None],
) -> Any:
    """One safe save of ``inst`` with plain calls (``_write`` of the sync bases).

    The first try is made here, call by call; a save that meets no lock and no conflict ends
    with it. One that does goes on in :func:`save_steps`, whose steps are performed here.
    """
    retry, snap, calls = _begin(inst, db, table, partial, held)
    try:
        for fn, args in calls:
            result = fn(*args)
    except (sqlite3.OperationalError, StaleVersionError) as e:
        steps = save_steps(inst, db, table, partial, held, snap, retry, e, resolve, assign)
    else:
        return _landed(inst, assign, partial, held, result)
    try:
        fn, args = next(steps)
        while True:
            try:
                result = None if fn is pause else fn(*args)
                if fn is pause:
                    sleep(*args)
            except BaseException as e:  # thrown into the loop, whose arms decide
                fn, args = steps.throw(e)
            else:
                fn, args = steps.send(result)
    except StopIteration as done:
        return done.value


async def arun(
    inst: Any,
    db: Any,
    table: str,
    partial: bool,
    held: dict,
    resolve: Callable[[dict, Any, Any], Any],
    assign: Callable[[Any, str, Any], None],
) -> Any:
    """Async twin of :func:`run` (``_write`` of the async bases): each call is awaited."""
    retry, snap, calls = _begin(inst, db, table, partial, held)
    try:
        for fn, args in calls:
            result = await fn(*args)
    except (sqlite3.OperationalError, StaleVersionError) as e:
        steps = save_steps(inst, db, table, partial, held, snap, retry, e, resolve, assign)
    else:
        return _landed(inst, assign, partial, held, result)
    try:
        fn, args = next(steps)
        while True:
            try:
                result = await (asleep(*args) if fn is pause else fn(*args))
            except BaseException as e:  # thrown into the loop, whose arms decide
                fn, args = steps.throw(e)
            else:
                fn, args = steps.send(result)
    except StopIteration as done:
        return done.value
