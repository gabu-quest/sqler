"""What a forked child starts with: the one place that decides it.

SQLite forbids a child process to use, or even to close, a connection its parent opened, and
a lock that a thread of the parent held at the fork stays held in the child, where that
thread does not exist. So in a child every sqler object that holds connections, transaction
state or locks starts again: no connection, no open transaction, new locks.

The hook runs in the child as ``fork()`` returns there, before the code that called it goes
on, in a process with one thread: no other thread can run a statement between the fork and
the hook, and the hook takes no lock.
"""

import os
import weakref
from typing import Callable, Protocol


class _StartsInChild(Protocol):
    def _start_in_child(self) -> None: ...


# Every live object that starts again in a child; held weakly, so an object ends as usual.
_objects: "weakref.WeakSet[_StartsInChild]" = weakref.WeakSet()
# What starts again of module-level state, in the order registered.
_functions: list[Callable[[], None]] = []


def register(obj: _StartsInChild) -> None:
    """``obj._start_in_child()`` runs in every child forked while ``obj`` is alive."""
    _objects.add(obj)


def register_module(function: Callable[[], None]) -> Callable[[], None]:
    """``function()`` runs in every child forked from now on; returns it."""
    _functions.append(function)
    return function


def _in_child() -> None:
    for function in _functions:
        function()
    for obj in list(_objects):
        obj._start_in_child()


if hasattr(os, "register_at_fork"):  # every platform that can fork
    os.register_at_fork(after_in_child=_in_child)
