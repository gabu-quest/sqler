"""The stored form of a value: what a row holds for it, and what a filter binds for it.

Every writer (``save()`` of each model base, ``update``, the document writes of the database)
and every filter gets it here, so a value is found by the form it was written in:

- a ``datetime``, ``date`` or ``time`` is its ``isoformat()``;
- a ``UUID`` or a path is its ``str``;
- an ``Enum`` is its value, in stored form;
- a set is a list, sorted when its elements sort, else ordered by type name and ``repr``;
- a tuple is a list;
- a ``Decimal`` is the number it equals: an integer when it is whole, else a float, and only if
  reading that number back gives the same value (anything else is refused);
- a ``timedelta`` is its number of seconds: an integer when whole, else a float, and only if
  ``timedelta(seconds=<that number>)`` equals it (anything else is refused);
- a pydantic model is its fields (``model_dump()``), each in stored form;
- a model that is a row of its own is the reference to that row, where the caller names
  the model classes it stores that way;

the same at every depth of a list or dict. ``str``, ``int``, ``float``, ``bool`` and ``None``
are stored as they are. Any other value has no stored form and is passed on as it is: a
document that holds one is refused by ``json``. ``bytes``, ``bytearray`` and ``memoryview`` are
refused in a document (JSON has no binary value): promote the field to a ``BLOB`` column, or
store text. A promoted ``BLOB`` column takes them as they are.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import date, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any
from uuid import UUID

from sqler.exceptions import UnstorableValueError

_AS_IS = frozenset({str, int, float, bool, type(None)})
_BINARY = (bytes, bytearray, memoryview)


def stored(value: Any, models: Any = (), field: str = "") -> Any:
    """The stored form of ``value``. An instance of ``models`` (a class or a tuple of them)
    is stored as the reference to its row. ``field`` is where ``value`` sits in the document
    or table, so that a refused value is named by its place."""
    try:
        return _stored(value, models)
    except UnstorableValueError as err:  # the place is looked for only now that the value is refused
        place = _locate(value, field, models)
        if not place:
            raise
        raise UnstorableValueError(f"{err.message} (at {place})", field=place, value=err.value) from None


def _stored(value: Any, models: Any) -> Any:
    if type(value) in _AS_IS:
        return value
    if isinstance(value, dict):  # a stored reference too: its stored form is itself
        return {k: _stored(v, models) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stored(v, models) for v in value]
    if isinstance(value, models):
        return _reference(value)
    plain = _plain(value)
    return value if plain is value else _stored(plain, models)


def dumps(doc: Any) -> str:
    """The JSON text of ``doc`` with every value in stored form. ``json`` asks only for a
    value it does not know, so a document already in stored form is not walked again."""
    try:
        return _ENCODER.encode(doc)
    except UnstorableValueError as err:  # the place is looked for only now that the document is refused
        place = _locate(doc)
        if not place:
            raise
        raise UnstorableValueError(f"{err.message} (at {place})", field=place, value=err.value) from None


def json_arg(value: Any) -> tuple[str, Any]:
    """``(placeholder, parameter)`` that hand the stored form of ``value`` to SQLite's JSON
    functions: a list, dict or bool as JSON text through ``json(?)`` (bound as it is, a bool
    would be stored as the integer 1 or 0), anything else bound as it is."""
    value = stored(value)
    if isinstance(value, (dict, list, bool)):
        return "json(?)", dumps(value)
    if isinstance(value, _BINARY):
        _refuse_binary(value)
    return "?", value


def column_arg(value: Any) -> Any:
    """The parameter that puts the stored form of ``value`` into a column of its own: a list
    or dict as JSON text, anything else bound as it is."""
    value = stored(value)
    return dumps(value) if isinstance(value, (dict, list)) else value


def _plain(value: Any) -> Any:
    """One step from ``value`` toward what JSON has; ``value`` itself when it has no stored
    form (or needs none)."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, time)):  # a datetime is a date
        return value.isoformat()
    if isinstance(value, (UUID, PurePath)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        try:
            return sorted(value)
        except TypeError:  # mixed kinds: any order will do, if every process gives the same
            return sorted(value, key=lambda v: (type(v).__name__, repr(v)))
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, timedelta):
        return _seconds(value)
    pydantic = sys.modules.get("pydantic")  # never imported here: the lite bases run without it
    if pydantic is not None and isinstance(value, pydantic.BaseModel):
        return value.model_dump()
    return value


def _number(value: Decimal) -> int | float:
    """The JSON number ``value`` equals; refused when no number holds it exactly: not
    finite, a whole value outside SQLite's 64-bit integers, or a fraction whose float differs."""
    if value.is_finite():
        if value == value.to_integral_value():
            if -(2**63) <= value < 2**63:
                return int(value)
        else:
            number = float(value)
            if Decimal(repr(number)) == value:
                return number
    raise UnstorableValueError(f"Decimal {value} cannot be stored: a number cannot hold it", value=value)


def _seconds(value: timedelta) -> int | float:
    """The JSON number of seconds ``value`` equals; refused when no number holds it exactly."""
    if value.microseconds == 0:
        return value.days * 86400 + value.seconds
    number = value.total_seconds()
    try:
        if timedelta(seconds=number) == value:
            return number
    except OverflowError:
        pass
    raise UnstorableValueError(f"timedelta {value} cannot be stored: a number of seconds cannot hold it", value=value)


def _locate(value: Any, path: str = "", models: Any = ()) -> str | None:
    """The path of the first value in ``value`` that has no stored form, like ``lines[2].price``.

    It walks in the order the encoder does, so it reaches that value before anything the
    encoder had not yet accepted."""
    if isinstance(value, dict):
        for key, item in value.items():
            found = _locate(item, f"{path}.{key}" if path else str(key), models)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _locate(item, f"{path}[{index}]", models)
            if found is not None:
                return found
        return None
    if isinstance(value, (str, int, float, type(None), models)):  # a row of its own is a reference
        return None
    try:
        plain = _plain(value)
    except UnstorableValueError:
        return path
    return path if plain is value else _locate(plain, path, models)


def _refuse_binary(value: Any) -> None:
    raise UnstorableValueError(
        "bytes cannot be stored in a JSON document: promote the field to a BLOB column, or store text",
        value=value,
    )


def _default(value: Any) -> Any:
    plain = _plain(value)
    if plain is value:
        if isinstance(value, _BINARY):
            _refuse_binary(value)
        raise UnstorableValueError(
            f"Object of type {type(value).__name__} is not JSON serializable", value=value
        )
    return plain


_ENCODER = json.JSONEncoder(default=_default)


def _reference(model: Any) -> dict:
    if model._id is None:
        kind = "async model" if inspect.iscoroutinefunction(model.save) else "model"
        raise ValueError(f"Related {kind} must be saved before saving parent")
    from sqler.models.ref import as_ref  # the models import this module

    return as_ref(model)
