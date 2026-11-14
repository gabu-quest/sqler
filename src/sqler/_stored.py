"""The stored form of a value: what a row holds for it, and what a filter binds for it.

Every writer (``save()`` of each model base, ``update``, the document writes of the database)
and every filter gets it here, so a value is found by the form it was written in:

- a ``datetime``, ``date`` or ``time`` is its ``isoformat()``;
- a ``UUID`` or a path is its ``str``;
- an ``Enum`` is its value, in stored form;
- a set is a list, sorted when its elements sort, else ordered by type name and ``repr``;
- a tuple is a list;
- a pydantic model is its fields (``model_dump()``), each in stored form;
- a model that is a row of its own is the reference to that row, where the caller names
  the model classes it stores that way;

the same at every depth of a list or dict. ``str``, ``int``, ``float``, ``bool`` and ``None``
are stored as they are. Any other value has no stored form and is passed on as it is: a
document that holds one is refused by ``json``.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import date, time
from enum import Enum
from pathlib import PurePath
from typing import Any
from uuid import UUID

_AS_IS = frozenset({str, int, float, bool, type(None)})


def stored(value: Any, models: Any = ()) -> Any:
    """The stored form of ``value``. An instance of ``models`` (a class or a tuple of them)
    is stored as the reference to its row."""
    if type(value) in _AS_IS:
        return value
    if isinstance(value, dict):
        if "_table" in value and "_id" in value:  # a stored reference holds nothing else
            return {"_table": value["_table"], "_id": value["_id"]}
        return {k: stored(v, models) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [stored(v, models) for v in value]
    if isinstance(value, models):
        return _reference(value)
    plain = _plain(value)
    return value if plain is value else stored(plain, models)


def dumps(doc: Any) -> str:
    """The JSON text of ``doc`` with every value in stored form. ``json`` asks only for a
    value it does not know, so a document already in stored form is not walked again."""
    return _ENCODER.encode(doc)


def json_arg(value: Any) -> tuple[str, Any]:
    """``(placeholder, parameter)`` that hand the stored form of ``value`` to SQLite's JSON
    functions: a list, dict or bool as JSON text through ``json(?)`` (bound as it is, a bool
    would be stored as the integer 1 or 0), anything else bound as it is."""
    value = stored(value)
    if isinstance(value, (dict, list, bool)):
        return "json(?)", json.dumps(value)
    return "?", value


def column_arg(value: Any) -> Any:
    """The parameter that puts the stored form of ``value`` into a column of its own: a list
    or dict as JSON text, anything else bound as it is."""
    value = stored(value)
    return json.dumps(value) if isinstance(value, (dict, list)) else value


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
    pydantic = sys.modules.get("pydantic")  # never imported here: the lite bases run without it
    if pydantic is not None and isinstance(value, pydantic.BaseModel):
        return value.model_dump()
    return value


def _default(value: Any) -> Any:
    plain = _plain(value)
    if plain is value:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
    return plain


_ENCODER = json.JSONEncoder(default=_default)


def _reference(model: Any) -> dict:
    if model._id is None:
        kind = "async model" if inspect.iscoroutinefunction(model.save) else "model"
        raise ValueError(f"Related {kind} must be saved before saving parent")
    from sqler.models.ref import as_ref  # the models import this module

    return as_ref(model)
