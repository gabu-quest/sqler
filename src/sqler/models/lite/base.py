"""Base class for dataclass-based SQLer models.

This module provides the foundation for SQLerLiteModel - a Pydantic-free
alternative that uses standard library dataclasses. Compatible with Pyodide
and other environments where Pydantic cannot be installed.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import fields
from datetime import date, datetime
from typing import Any, ClassVar, Optional, Type, TypeVar

T = TypeVar("T", bound="SQLerLiteModelBase")


def _is_classvar(ann: object) -> bool:
    """Check if an annotation is ClassVar[X]."""
    if isinstance(ann, str):
        return "ClassVar" in ann
    return typing.get_origin(ann) is ClassVar


class SQLerLiteModelBase:
    """Base class for lite models providing Pydantic-like interface.

    This is NOT a dataclass itself - subclasses should be decorated with
    @dataclass. This class provides the common interface methods.
    """

    # Private attributes (set via object.__setattr__ to bypass frozen)
    # These are NOT dataclass fields
    _id: Optional[int] = None
    _snapshot: Optional[dict] = None

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
        # Get dataclass field names
        field_names = {f.name for f in fields(cls)}  # type: ignore[arg-type]

        # Get annotations for relationship resolution
        annotations = getattr(cls, "__annotations__", {})

        # Filter to only known fields (ignore extra like Pydantic's extra="ignore")
        init_data = {}
        for k, v in data.items():
            if k not in field_names or k.startswith("_"):
                continue
            # Check if this is a dict that might be a related model
            if isinstance(v, dict) and "_id" in v and k in annotations:
                # Try to resolve using registry
                from sqler import registry

                # Check if this dict has _table (from _resolve_relations path)
                # or try to infer the model from the field annotation
                if "_table" in v:
                    # Already has table info, use registry
                    table = v.get("_table")
                    model_cls = registry.resolve(table) if isinstance(table, str) else None
                    if model_cls is not None and hasattr(model_cls, "model_validate"):
                        init_data[k] = model_cls.model_validate(v)
                        continue
                else:
                    # No _table - this is from batch_resolve, try to get model from annotation
                    ann = annotations.get(k)
                    model_cls = cls._resolve_annotation_to_model(ann)
                    if model_cls is not None:
                        init_data[k] = model_cls.model_validate(v)
                        continue
            init_data[k] = v

        # Create instance
        inst = cls(**init_data)

        # Set _id if present in data
        if "_id" in data:
            object.__setattr__(inst, "_id", data["_id"])

        return inst

    @classmethod
    def _resolve_annotation_to_model(cls, ann: Any) -> Optional[Type["SQLerLiteModelBase"]]:
        """Try to resolve an annotation to a SQLerLiteModelBase subclass.

        Args:
            ann: The type annotation (could be a type, string, or Optional/Union).

        Returns:
            The model class if found, else None.
        """
        if ann is None:
            return None

        # If it's already a class, check if it's a model
        if isinstance(ann, type):
            if issubclass(ann, SQLerLiteModelBase):
                return ann
            return None

        # Handle Optional[T] / Union[T, None] - get __args__
        args = getattr(ann, "__args__", None)
        if args:
            for arg in args:
                if arg is type(None):
                    continue
                result = cls._resolve_annotation_to_model(arg)
                if result is not None:
                    return result

        # Handle string annotation (forward reference)
        if isinstance(ann, str):
            # Try to find in registry by pluralized name
            from sqler import registry

            # Try common table name patterns
            lower = ann.lower()
            for table_name in [lower, lower + "s", lower + "es"]:
                try:
                    model_cls = registry.resolve(table_name)
                    if model_cls is not None and issubclass(model_cls, SQLerLiteModelBase):
                        return model_cls
                except (LookupError, TypeError):
                    pass

        return None

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
            result[f.name] = self._serialize_value(value)

        return result

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        """Serialize a value for JSON storage."""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, (list, tuple)):
            return [SQLerLiteModelBase._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: SQLerLiteModelBase._serialize_value(v) for k, v in value.items()}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.asdict(value)
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
