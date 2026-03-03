"""Base class for msgspec Struct-based SQLer models.

This module provides the foundation for SQLerMsgspecModel — a high-performance
alternative to the dataclass-based lite model that uses msgspec Structs for
C-level JSON decode+validate speed.
"""

from datetime import date, datetime
from typing import Any, Optional, Type, TypeVar

import msgspec
import msgspec.structs

T = TypeVar("T", bound="SQLerMsgspecModelBase")


class SQLerMsgspecModelBase(msgspec.Struct, kw_only=True):
    """Base class for msgspec-backed models providing Pydantic-like interface.

    Subclasses define their fields as Struct fields. The `_id` and `_snapshot`
    fields are declared with defaults so they don't need to be passed to the
    constructor.

    ``kw_only=True`` allows subclasses to freely add required fields after
    these optional base fields (msgspec requires required fields before
    optional fields in positional mode).
    """

    _id: Optional[int] = None
    _snapshot: Optional[dict] = None

    @classmethod
    def model_validate(cls: Type[T], data: dict) -> T:
        """Create instance from dict (Pydantic-compatible method).

        Strips ``_``-prefixed keys before ``msgspec.convert()``, then restores
        ``_id`` from the original data.

        Args:
            data: Dictionary with field values.

        Returns:
            New instance populated from dict.
        """
        # Strip underscore-prefixed keys for conversion
        filtered = {k: v for k, v in data.items() if not k.startswith("_")}

        # strict=False mirrors Pydantic's permissive coercion (e.g. str "123" → int 123).
        # This is intentional for compatibility with data stored by other model backends.
        inst = msgspec.convert(filtered, cls, strict=False)

        # Restore _id from original data
        if "_id" in data:
            inst._id = data["_id"]

        return inst

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

        for f in msgspec.structs.fields(self):
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
            return [SQLerMsgspecModelBase._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: SQLerMsgspecModelBase._serialize_value(v) for k, v in value.items()}
        if isinstance(value, msgspec.Struct):
            return msgspec.structs.asdict(value)
        return value

    class _ModelFieldsDescriptor:
        """Descriptor that computes model_fields per-class."""

        def __set_name__(self, owner: type, name: str) -> None:
            self._name = name

        def __get__(self, obj: Any, cls: type | None = None) -> dict[str, Any]:
            if cls is None:
                cls = type(obj)
            return {
                f.name: f
                for f in msgspec.structs.fields(cls)
                if not f.name.startswith("_")
            }

    # No type annotation — prevents msgspec from treating this as a Struct field
    model_fields = _ModelFieldsDescriptor()  # type: ignore[assignment]

    @classmethod
    def get_field_names(cls) -> set[str]:
        """Return set of public field names."""
        return {
            f.name for f in msgspec.structs.fields(cls) if not f.name.startswith("_")
        }

    def __repr__(self) -> str:
        """Return string representation."""
        cls_name = self.__class__.__name__
        field_strs = []
        for f in msgspec.structs.fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            field_strs.append(f"{f.name}={value!r}")
        if self._id is not None:
            field_strs.insert(0, f"_id={self._id}")
        return f"{cls_name}({', '.join(field_strs)})"
