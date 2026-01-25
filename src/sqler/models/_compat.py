"""Compatibility layer for optional Pydantic dependency.

This module provides detection and helper functions for working with
optional Pydantic dependency. SQLer can work in two modes:

1. Full mode (with Pydantic): Use SQLerModel, SQLerSafeModel, etc.
2. Lite mode (without Pydantic): Use SQLerLiteModel, SQLerLiteSafeModel, etc.

The lite mode uses standard library dataclasses and is compatible with
Pyodide and other environments where Pydantic's native extensions
cannot be installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
