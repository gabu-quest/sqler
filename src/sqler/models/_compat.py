"""Compatibility layer for optional dependencies (Pydantic, msgspec).

This module provides detection and helper functions for working with
optional model backends. SQLer supports three modes:

1. Full mode (with Pydantic): Use SQLerModel, SQLerSafeModel, etc.
2. Lite mode (without Pydantic): Use SQLerLiteModel, SQLerLiteSafeModel, etc.
3. Msgspec mode (with msgspec): Use SQLerMsgspecModel for C-level speed.

The lite mode uses standard library dataclasses and is compatible with
Pyodide and other environments where Pydantic's native extensions
cannot be installed.
"""

from __future__ import annotations

import sys
import types
import typing
import weakref
from typing import Any

# Detect if Pydantic is available
PYDANTIC_VERSION: str | None
try:
    import pydantic

    PYDANTIC_AVAILABLE = True
    PYDANTIC_VERSION = pydantic.VERSION
except ImportError:
    PYDANTIC_AVAILABLE = False
    PYDANTIC_VERSION = None


def require_pydantic(feature: str = "This feature") -> None:
    """Raise ImportError if Pydantic is not installed.

    Args:
        feature: Name of the feature requiring Pydantic for error message.

    Raises:
        ImportError: If Pydantic is not installed.
    """
    if not PYDANTIC_AVAILABLE:
        raise ImportError(
            f"{feature} requires Pydantic.\n"
            f"Install with: pip install 'sqler[pydantic]' or pip install pydantic\n\n"
            f"For Pyodide/no-Pydantic environments, use lite models instead:\n"
            f"    from sqler import SQLerLiteModel, SQLerLiteSafeModel"
        )


def get_model_backend() -> str:
    """Return current model backend availability.

    Returns:
        'pydantic' if Pydantic is available, 'dataclass' otherwise.
    """
    return "pydantic" if PYDANTIC_AVAILABLE else "dataclass"


def is_pydantic_model(cls: type) -> bool:
    """Check if a class is a Pydantic BaseModel subclass.

    Args:
        cls: Class to check.

    Returns:
        True if cls is a Pydantic BaseModel subclass.
    """
    if not PYDANTIC_AVAILABLE:
        return False
    from pydantic import BaseModel

    return isinstance(cls, type) and issubclass(cls, BaseModel)


# Detect if msgspec is available
MSGSPEC_VERSION: str | None
try:
    import msgspec

    MSGSPEC_AVAILABLE = True
    MSGSPEC_VERSION = msgspec.__version__
except ImportError:
    MSGSPEC_AVAILABLE = False
    MSGSPEC_VERSION = None


def require_msgspec(feature: str = "This feature") -> None:
    """Raise ImportError if msgspec is not installed.

    Args:
        feature: Name of the feature requiring msgspec for error message.

    Raises:
        ImportError: If msgspec is not installed.
    """
    if not MSGSPEC_AVAILABLE:
        raise ImportError(
            f"{feature} requires msgspec.\n"
            f"Install with: pip install 'sqler[msgspec]' or pip install msgspec\n\n"
            f"For Pydantic-free environments, use lite models instead:\n"
            f"    from sqler import SQLerLiteModel"
        )


def is_msgspec_model(cls: type) -> bool:
    """Check if a class is a SQLerMsgspecModel subclass.

    Args:
        cls: Class to check.

    Returns:
        True if cls is a SQLerMsgspecModelBase subclass.
    """
    if not MSGSPEC_AVAILABLE:
        return False
    from sqler.models.msgspec.base import SQLerMsgspecModelBase

    return isinstance(cls, type) and issubclass(cls, SQLerMsgspecModelBase)


def is_lite_model(cls: type) -> bool:
    """Check if a class is a SQLerLiteModel subclass.

    Args:
        cls: Class to check.

    Returns:
        True if cls is a SQLerLiteModel subclass.
    """
    # Import here to avoid circular imports
    from sqler.models.lite.base import SQLerLiteModelBase

    return isinstance(cls, type) and issubclass(cls, SQLerLiteModelBase)


# {class: {field: its declared type}} (see ``field_types``); a class's fields are fixed once
# made, and the entry goes when the class does
_FIELD_TYPES: weakref.WeakKeyDictionary[type, dict[str, Any]] = weakref.WeakKeyDictionary()


def field_types(cls: type) -> dict[str, Any]:
    """The type each public field of the model class ``cls`` declares, on every base: a
    pydantic field's annotation, a lite (dataclass) field's type, and a msgspec field's type,
    the fields a mixin gives a lite or msgspec class among them. A type written as a string
    is resolved where it was written; one that names nothing there is ``Any``.

    Found at the first ask, once the class and the classes it names are defined.
    """
    found = _FIELD_TYPES.get(cls)
    if found is None:
        found = _FIELD_TYPES[cls] = _declared(cls)
    return found


def _declared(cls: type) -> dict[str, Any]:
    if hasattr(cls, "__pydantic_validator__"):  # a pydantic class: its fields know their types
        fields = getattr(cls, "model_fields")
        return {n: f.annotation for n, f in fields.items() if not n.startswith("_")}
    names = [n for n in getattr(cls, "model_fields", {}) if not n.startswith("_")]
    try:
        hints = typing.get_type_hints(cls)
    except (NameError, AttributeError, SyntaxError, TypeError):  # one field names nothing there
        return {n: _hint_of(cls, n) for n in names}
    return {n: hints.get(n, Any) for n in names}


def _hint_of(cls: type, name: str) -> Any:
    """The type the field ``name`` of ``cls`` declares, read alone, in the module of the class
    that declares it (``Any`` when it names nothing there)."""
    for klass in cls.__mro__:
        written = vars(klass).get("__annotations__", {})
        if name not in written:
            continue
        hint = written[name]
        if not isinstance(hint, str):
            return hint
        module = sys.modules.get(klass.__module__)
        alone = types.SimpleNamespace(__annotations__={name: hint})
        try:
            return typing.get_type_hints(
                alone, globalns=vars(module) if module else {}, localns={klass.__name__: klass}
            )[name]
        except (NameError, AttributeError, SyntaxError, TypeError):  # it names nothing there
            return Any
    return Any
