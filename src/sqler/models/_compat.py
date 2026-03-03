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

# Detect if Pydantic is available
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
