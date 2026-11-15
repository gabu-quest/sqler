"""Land an exception at a chosen point of the synchronous adapter, by real means.

Shared by the tests of the ``":memory:"`` turn and of the ends of a transaction. Nothing
of sqler is replaced: SQLite's progress handler, a trace function, a signal or
``PyThreadState_SetAsyncExc`` raises the exception where CPython raises one.
"""

import ctypes
import functools
import linecache
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

ADAPTER = "sqler.adapter.synchronous"


class Skipped(Exception):
    """The exception a trace function or another thread raises at the chosen point."""


def stop_once_and_interrupt(conn) -> None:
    """The next step SQLite takes on ``conn`` stops its statement, once, and sends this
    process SIGINT through libc with no Python frame of its own, so the interrupt is pending
    as the statement fails (``signal.raise_signal`` or ``os.kill`` would check signals
    themselves, and sqlite3 would turn the interrupt into ``OperationalError``)."""
    stop = functools.partial(max, key=getattr(ctypes.CDLL(None), "raise"))
    conn.set_progress_handler(functools.partial(next, map(stop, [[int(signal.SIGINT)]]), 0), 1)


def _raise_from_another_thread() -> None:
    """Have another thread set ``Skipped`` on the calling thread, then run until CPython
    raises it here (a backward jump checks for it)."""
    target = threading.get_ident()
    sent = threading.Event()

    def send() -> None:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(target), ctypes.py_object(Skipped))
        sent.set()

    sender = threading.Thread(target=send)
    sender.start()
    assert sent.wait(10)
    for _ in range(1_000_000):
        pass
    raise AssertionError("the exception set from another thread was not raised")


def _raise_sigint() -> None:
    signal.raise_signal(signal.SIGINT)  # the handler raises KeyboardInterrupt here
    raise AssertionError("SIGINT raised no KeyboardInterrupt")


def _raise_skipped() -> None:
    raise Skipped("raised by a trace function")


RAISERS: dict[str, Callable[[], None]] = {
    "trace": _raise_skipped,
    "another-thread": _raise_from_another_thread,
    "sigint": _raise_sigint,  # the main thread only
}
RAISED = {"trace": Skipped, "another-thread": Skipped, "sigint": KeyboardInterrupt}

# Where: (the trace event, the adapter function). "return" of ``take`` lands after it made
# the thread the holder and before the caller's ``try`` runs.
POINTS = {
    "take-entry": ("call", "take"),
    "take-return": ("return", "take"),
    "give-entry": ("call", "give"),
    "end_of-entry": ("call", "end_of"),
}


def trace_raising_at(point: str, how: str, fired: list[str]) -> Callable:
    """A trace function that raises, once, at ``point`` of the adapter by ``how``; appends
    ``point`` to ``fired`` as it does. A trace function that raises is switched off."""
    wanted, name = POINTS[point]
    raise_ = RAISERS[how]

    def on_return(frame, event, arg):
        if event == "return" and arg is not None and not fired:
            fired.append(point)
            raise_()
        return on_return

    def on_call(frame, event, arg):
        if fired or frame.f_code.co_name != name or frame.f_globals.get("__name__") != ADAPTER:
            return None
        if wanted == "return":
            return on_return
        fired.append(point)
        raise_()

    return on_call


def set_skipped_once_it_waits_in(ident: int, name: str) -> None:
    """Once thread ``ident`` is inside the adapter function ``name`` (waiting for a lock the
    caller holds), set ``Skipped`` on it from this thread: CPython raises it at the next
    function the thread enters after it got the lock. Fails after 10 s."""
    deadline = time.monotonic() + 10
    while True:
        frame = sys._current_frames().get(ident)
        while frame is not None and frame.f_code.co_name != name:
            frame = frame.f_back
        if frame is not None and frame.f_globals.get("__name__") == ADAPTER:
            break
        assert time.monotonic() < deadline, f"the thread never came to wait in {name}"
        time.sleep(0.001)
    del frame
    assert ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), ctypes.py_object(Skipped)) == 1


def trace_raising_at_line(name: str, n: int, fired: list[int]) -> Callable:
    """A trace function that raises ``Skipped``, once, at the ``n``-th line event (from 0) of
    the adapter function ``name``; appends the line number to ``fired`` as it does.

    The line events of a ``with`` line and of a ``try:`` line are not counted: CPython
    (3.12) sends them outside the protection of the blocks around them (a ``with`` line's
    as its block ends, before ``__exit__`` is called; a ``try:`` line's escapes every
    handler of the frame), so an exception a trace function raises there leaves any
    ``with`` unexited and any ``finally`` unrun, a lock's too. Only a trace function's
    line event raises there.
    """
    seen = [0]

    def on_line(frame, event, arg):
        if event == "line" and not fired:
            text = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
            if text.startswith("with ") or text == "try:":
                return on_line
            if seen[0] == n:
                fired.append(frame.f_lineno)
                raise Skipped(f"raised at line {frame.f_lineno}")
            seen[0] += 1
        return on_line

    def on_call(frame, event, arg):
        if fired or frame.f_code.co_name != name or frame.f_globals.get("__name__") != ADAPTER:
            return None
        return on_line

    return on_call


@contextmanager
def tracing(trace: Callable) -> Iterator[None]:
    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(None)


@contextmanager
def raising_at(point: str, how: str) -> Iterator[list[str]]:
    """In the calling thread, raise once at ``point`` by ``how`` while the block runs;
    yields the list that names the point once it was reached."""
    fired: list[str] = []
    with tracing(trace_raising_at(point, how, fired)):
        yield fired
