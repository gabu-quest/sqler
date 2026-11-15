"""Base class for dataclass-based SQLer models.

This module provides the foundation for SQLerLiteModel - a Pydantic-free
alternative that uses standard library dataclasses. Compatible with Pyodide
and other environments where Pydantic cannot be installed.
"""

from __future__ import annotations

import copy
import dataclasses
import typing
from dataclasses import fields
from datetime import date, datetime
from typing import Any, ClassVar, Optional, Type, TypeVar

from sqler.models import _dirty

T = TypeVar("T", bound="SQLerLiteModelBase")


def _is_classvar(ann: object) -> bool:
    """Check if an annotation is ClassVar[X]."""
    if isinstance(ann, str):
        return "ClassVar" in ann
    return typing.get_origin(ann) is ClassVar


def _asdict(value: Any, within: tuple) -> Any:
    """``dataclasses.asdict(value)`` that raises ``ValueError`` on a dataclass met again
    inside itself instead of recursing until ``RecursionError``."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if id(value) in within:
            raise ValueError("Circular reference detected (id repeated)")
        within = (*within, id(value))
        return {f.name: _asdict(getattr(value, f.name), within) for f in fields(value)}
    if isinstance(value, tuple) and hasattr(value, "_fields"):  # a named tuple
        return type(value)(*[_asdict(v, within) for v in value])
    if isinstance(value, (list, tuple)):
        return type(value)(_asdict(v, within) for v in value)
    if isinstance(value, dict):
        return {_asdict(k, within): _asdict(v, within) for k, v in value.items()}
    return copy.deepcopy(value)


# {class: the names of its fields ``__init__`` takes from a document} (see ``_init_fields``)
_INIT_NAMES: dict[type, frozenset] = {}


class SQLerLiteModelBase:
    """Base class for lite models providing Pydantic-like interface.

    This is NOT a dataclass itself - subclasses should be decorated with
    @dataclass. This class provides the common interface methods.
    """

    # Private attributes (set via object.__setattr__ to bypass frozen)
    # These are NOT dataclass fields
    _id: Optional[int] = None
    _snapshot: Optional[dict] = None
    # table this row was read from, when ``using(db, table)`` named one other than the class's
    _source_table: Optional[str] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Inject annotations from non-dataclass mixin bases.

        When a mixin like TimestampMixin declares ``created_at: Optional[datetime] = None``,
        ``@dataclass`` on the subclass won't see it (only dataclass bases contribute fields).
        This hook copies those annotations into the subclass so ``@dataclass`` picks them up.
        """
        super().__init_subclass__(**kwargs)
        own_ann = cls.__dict__.get("__annotations__", None)
        if own_ann is None:
            own_ann = {}
            cls.__annotations__ = own_ann

        for base in cls.__mro__[1:]:
            if base is object or base is SQLerLiteModelBase:
                continue
            if dataclasses.is_dataclass(base):
                continue
            base_ann = base.__dict__.get("__annotations__", {})
            for name, ann in base_ann.items():
                if name in own_ann or name.startswith("_"):
                    continue
                if _is_classvar(ann):
                    continue
                own_ann[name] = ann
                # Copy default value so @dataclass can find it in cls.__dict__
                if name in base.__dict__:
                    setattr(cls, name, base.__dict__[name])

        from sqler.models._table_names import _register_defined_class

        _register_defined_class(cls)

    def __post_init__(self) -> None:
        """Initialize private attributes after dataclass __init__."""
        # Use object.__setattr__ to set private attrs without triggering
        # any potential frozen dataclass restrictions
        object.__setattr__(self, "_id", None)
        object.__setattr__(self, "_snapshot", None)

    @classmethod
    def model_validate(cls: Type[T], data: dict) -> T:
        """Create instance from dict (Pydantic-compatible method).

        Args:
            data: Dictionary with field values.

        Returns:
            New instance populated from dict.
        """
        inst = cls(**cls._init_fields(data))

        # Set _id if present in data
        if "_id" in data:
            object.__setattr__(inst, "_id", data["_id"])

        return inst

    @classmethod
    def _init_fields(cls, data: dict) -> dict:
        """The keyword arguments of ``__init__`` that ``data`` gives: its keys that are fields,
        none starting with ``_`` (others are ignored, like pydantic's ``extra="ignore"``).
        ``model_validate`` and the reference loader, which runs ``__init__`` on an object it
        made before the row was built (a cycle), both take them from here."""
        names = _INIT_NAMES.get(cls)
        if names is None:  # a class's fields are fixed once ``@dataclass`` made it
            names = _INIT_NAMES[cls] = frozenset(
                f.name for f in fields(cls) if not f.name.startswith("_")  # type: ignore[arg-type]
            )
        return {k: v for k, v in data.items() if k in names}

    def model_dump(
        self,
        *,
        exclude: Optional[set[str]] = None,
        exclude_unset: bool = False,
        exclude_none: bool = False,
    ) -> dict:
        """Convert to dict (Pydantic-compatible method).

        Args:
            exclude: Field names to exclude.
            exclude_unset: Not supported (for Pydantic compat).
            exclude_none: Exclude fields with None values.

        Returns:
            Dictionary representation of the model.
        """
        exclude = exclude or set()
        result = {}

        for f in fields(self):  # type: ignore[arg-type]
            if f.name.startswith("_"):
                continue
            if f.name in exclude:
                continue
            value = getattr(self, f.name)
            if exclude_none and value is None:
                continue
            result[f.name] = self._serialize_value(value, (id(self),))

        return result

    @staticmethod
    def _serialize_value(value: Any, within: tuple = ()) -> Any:
        """Serialize a value for JSON storage. ``within``: the ids of the models being dumped
        around ``value``; a model met again inside itself (a cycle of rows) raises
        ``ValueError``, as pydantic's ``model_dump()`` does."""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, (list, tuple)):
            return [SQLerLiteModelBase._serialize_value(v, within) for v in value]
        if isinstance(value, dict):
            return {k: SQLerLiteModelBase._serialize_value(v, within) for k, v in value.items()}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _asdict(value, within)
        return value

    class _ModelFieldsDescriptor:
        """Descriptor that computes model_fields per-class (replaces @classmethod @property)."""

        def __set_name__(self, owner: type, name: str) -> None:
            self._name = name

        def __get__(self, obj: Any, cls: type | None = None) -> dict[str, Any]:
            if cls is None:
                cls = type(obj)
            return {f.name: f for f in fields(cls) if not f.name.startswith("_")}  # type: ignore[arg-type]

    model_fields: dict[str, Any] = _ModelFieldsDescriptor()  # type: ignore[assignment]

    @classmethod
    def get_field_names(cls) -> set[str]:
        """Return set of public field names."""
        return {f.name for f in fields(cls) if not f.name.startswith("_")}  # type: ignore[arg-type]

    # ----- dirty tracking -----
    is_dirty = _dirty.is_dirty
    get_dirty_fields = _dirty.get_dirty_fields

    def __repr__(self) -> str:
        """Return string representation."""
        cls_name = self.__class__.__name__
        field_strs = []
        for f in fields(self):  # type: ignore[arg-type]
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            field_strs.append(f"{f.name}={value!r}")
        # Include _id if set
        if self._id is not None:
            field_strs.insert(0, f"_id={self._id}")
        return f"{cls_name}({', '.join(field_strs)})"
