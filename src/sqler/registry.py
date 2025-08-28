from __future__ import annotations

from typing import Dict, Optional

_REGISTRY: Dict[str, type] = {}


def register(table: str, cls: type) -> None:
    _REGISTRY[table] = cls


def resolve(table: str) -> Optional[type]:
    return _REGISTRY.get(table)


def tables() -> Dict[str, type]:
    """Return a copy of the table->class registry."""
    return dict(_REGISTRY)
