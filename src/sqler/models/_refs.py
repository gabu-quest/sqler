"""References between documents: the one walk every resolver uses.

A stored reference is a dict holding ``_table`` and ``_id``. Two resolvers follow them:

- the batch resolver behind ``query().all()``, ``first()`` and ``from_ids`` puts the
  stored documents in place of the references, read from the queryset's own database,
  level by level to a fixed depth; at a typed position (:func:`typed_fields`) it puts a
  model built from the row (:func:`from_row`) instead;
- the per-row resolver behind ``from_id`` and ``refresh`` asks the class that owns each
  table and puts model instances in place.

Both collect every reference of what they are given first, so that the rows of one
table are read with one statement, then put what they loaded in place.
"""

from __future__ import annotations

import builtins
import collections.abc
import inspect
import re
import sys
import types
import typing
from typing import Any, Callable, Generator, Optional

from sqler import registry
from sqler.db.async_db import _aselect_rows
from sqler.db.sqler_db import _in_order, _row_columns, _row_documents, _select_rows
from sqler.exceptions import InvalidTableNameError
from sqler.models import _dirty
from sqler.models._table_names import _aload_unbound, _default_table_name, _load_unbound
from sqler.utils import validate_table_name

# A reference that is not replaced (a cycle, or a row its class cannot hydrate): it stays
# the reference it was.
KEEP = object()

# What a row its class cannot hydrate raises, for every loader, sync and async. An error of
# the database is none of these: it reaches the caller, and the reference is not kept.
UNLOADABLE = (LookupError, RuntimeError, ValueError)

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def ref_key(value: dict) -> Optional[tuple[str, int]]:
    """``(table, id)`` of a dict holding ``_table`` and ``_id``, or None if it is not usable.

    Only the shape is decided here: ``_table`` a string, ``_id`` an integer (not a bool,
    float, string or list) that fits SQLite's 64-bit INTEGER. A dict that is not usable is
    left as it is, and not walked into. The name is not checked: it is data we did not
    write, and :func:`batch` checks it once per table where it puts it into SQL.
    """
    table, rid = value["_table"], value["_id"]
    if not isinstance(table, str) or isinstance(rid, bool) or not isinstance(rid, int):
        return None
    if not _SQLITE_INT_MIN <= rid <= _SQLITE_INT_MAX:
        return None  # sqlite3 raises OverflowError when binding it
    return table, rid


def _is_table_name(table: str) -> bool:
    try:
        validate_table_name(table)
    except InvalidTableNameError:
        return False
    return True


def _admits(table: str, admits: tuple) -> bool:
    """True when the class that owns ``table`` is one of ``admits``, or a subclass."""
    mdl = registry.resolve(table)
    return mdl is not None and issubclass(mdl, admits)


class Position(typing.NamedTuple):
    """What a field's annotation puts at a place in its value: ``admits``, the model classes
    a reference there may be a row of; ``item``, the position of each entry of a list
    there; ``value``, the position of each value of a dict there; ``fixed``, True when the
    list there has one entry per place (``tuple[Child, Child]``), so an entry is never left
    out. ``Optional[Child]`` is ``Position((Child,), None, None)``, ``list[Child]`` is
    ``Position((), <that>, None)``."""

    admits: tuple
    item: Optional["Position"]
    value: Optional["Position"]
    fixed: bool = False


def collect(doc: dict, typed: dict, out: dict[tuple[str, int], None]) -> bool:
    """Add every reference at a typed position of ``doc`` (in order, once each) to ``out``.

    ``typed`` is :func:`typed_fields` of the class of ``doc``. A reference is taken only
    where the annotation of its field puts a model (:class:`Position`), and only into a
    table of a class admitted there; a reference anywhere else is data, and stays the
    dict it is. True when ``doc`` holds a reference where a model is admitted, taken or
    not: :func:`replace` has something to put there.
    """
    held = False
    for name, at in typed.items():
        if name in doc:
            held |= _collect(doc[name], at, out)
    return held


def _collect(value: Any, at: Position, out: dict[tuple[str, int], None]) -> bool:
    admits, item, inner, _ = at
    held = False
    if isinstance(value, dict):
        if "_table" in value and "_id" in value:
            if not admits:
                return False
            key = ref_key(value)
            if key is not None and _admits(key[0], admits):
                out[key] = None
            return True
        if inner is not None:
            for v in value.values():
                held |= _collect(v, inner, out)
    elif isinstance(value, list) and item is not None:
        for v in value:
            held |= _collect(v, item, out)
    return held


# what stands at a typed position whose reference leads to no row of a class admitted there
_NO_ROW = object()


def replace(doc: dict, typed: dict, get: Callable[[tuple[str, int], Any], Any]) -> dict:
    """A copy of ``doc`` with ``get(key, KEEP)`` in place of each reference :func:`collect`
    takes from it. ``KEEP`` leaves the reference as it is (a row being replaced further
    up). Every other reference at a place where a model is admitted leads to no row of
    the field's class: ``get`` gives None for it (the row or its table is gone), or it is
    not usable or into another class's table; it reads as ``None`` where it is the field's
    value or an entry of a list with one entry per place (``tuple[Child, Child]``), and is
    left out where it is an entry of any other list or a value of a dict."""
    out = {}
    for k, v in doc.items():
        at = typed.get(k)
        if at is not None:
            v = _replace(v, at, get)
            if v is _NO_ROW:
                v = None
        out[k] = v
    return out


def _replace(value: Any, at: Position, get: Callable[[tuple[str, int], Any], Any]) -> Any:
    admits, item, inner, fixed = at
    if isinstance(value, dict):
        if "_table" in value and "_id" in value:
            if not admits:
                return value  # data where the annotation puts no model: its validation decides
            key = ref_key(value)
            if key is None or not _admits(key[0], admits):
                return _NO_ROW
            got = get(key, KEEP)
            return value if got is KEEP else _NO_ROW if got is None else got
        if inner is None:
            return value
        out = {}
        for k, v in value.items():
            v = _replace(v, inner, get)
            if v is not _NO_ROW:
                out[k] = v
        return out
    if isinstance(value, list) and item is not None:
        entries = []
        for v in value:
            v = _replace(v, item, get)
            if v is not _NO_ROW:
                entries.append(v)
            elif fixed:
                entries.append(None)  # the places after it keep theirs
        return entries
    return value


# ---- the batch resolver -------------------------------------------------------------


def batch(
    docs: list[dict], max_depth: int, cls: Any = None, db: Any = None
) -> Generator[tuple[str, list], Any, list[dict]]:
    """Resolve the references of ``docs`` into stored documents, to ``max_depth`` levels.

    Only a reference at a typed position is followed (:func:`collect`): in a field of
    ``cls`` (the class of ``docs``) that admits the class of its table, and inside a row
    that was followed, in a field of that row's class. Yields ``(table, ids)`` for each
    read and is sent what :func:`read_rows` gives: ``(documents, versions)``, or ``None``
    when the table does not exist; one read per table per level. A table whose name is
    not an identifier is never read.
    Returns the documents: ``docs`` itself when no document holds a reference to
    follow; else new ones in which a reference that is being replaced further up stays a
    reference, and one to a row that is not there (gone, or its table missing) leads to
    no row (:func:`replace`). A followed row is replaced by its class's model of the row
    (:func:`from_row`) when the class reads ``db``: bound to it, or never bound; else by
    the stored document.
    """
    top = typed_fields(cls)
    found: dict[tuple[str, int], None] = {}
    held = False
    if max_depth > 0 and top:
        for d in docs:
            held |= collect(d, top, found)
    if not held:
        return docs
    # the rows asked for: a document, or None while it is not read (or is not there)
    resolved: dict[tuple[str, int], Optional[dict]] = {}
    versions: dict[tuple[str, int], Any] = {}
    for _ in range(max_depth):
        wanted: dict[str, list[int]] = {}
        refused: set[str] = set()
        for table, rid in found:
            if (table, rid) in resolved or table in refused:
                continue
            ids = wanted.get(table)
            if ids is None:
                if not _is_table_name(table):
                    refused.add(table)
                    continue
                ids = wanted[table] = []
            ids.append(rid)
            resolved[(table, rid)] = None
        if not wanted:
            break
        found = {}
        for table, ids in wanted.items():
            got = yield table, ids
            if got is None:
                continue
            got, row_versions = got
            typed = typed_fields(registry.resolve(table))
            for obj in got:
                resolved[(table, int(obj["_id"]))] = obj
                collect(obj, typed, found)
            if row_versions:
                versions.update(((table, rid), v) for rid, v in row_versions.items())

    processing: set[tuple[str, int]] = set()

    def expand(key: tuple[str, int], keep: Any) -> Any:
        if key in processing:
            return keep  # a cycle: the row is being replaced further up
        if key not in resolved:
            return keep  # deeper than ``max_depth``: never read
        doc = resolved[key]
        if doc is None:
            return None  # the row, or its table, is not there
        mdl = registry.resolve(key[0])  # a class the field admits (see ``collect``)
        processing.add(key)
        out = replace(doc, typed_fields(mdl), expand)
        processing.discard(key)
        if db is None or not _reads(mdl, db) or not hasattr(mdl, "_versioned_rows"):
            return out  # no database to build from, bound to another one, or msgspec
        row = {**out, "_version": versions.get(key, 0)} if mdl._versioned_rows else out
        try:
            return from_row(mdl, row, doc)
        except (TypeError, ValueError):
            return out  # refused by its class: the parent's validation says so, as it did

    return [replace(d, top, expand) for d in docs]


def _reads(mdl: Any, db: Any) -> bool:
    """True when the class ``mdl`` reads ``db``: bound to it, or never bound."""
    bound = getattr(mdl, "_db", None)
    return bound is None or bound is db


def from_row(cls: Any, doc: dict, row: dict) -> Any:
    """A model of the row ``doc`` that knows its row: ``_id``, ``_version`` for a class
    whose rows are versioned, and its snapshot (:func:`_dirty.snapshot`).

    ``doc`` is the document the model is validated from: the row with its references
    resolved. ``row`` is the row as it was read, before that: the snapshot is taken from it,
    so it is what the database held. A site that resolves nothing passes one dict for both.

    The one step every way in uses to turn a row into a model, for every model base.
    """
    inst = cls.model_validate(doc)
    _set(inst, "_id", doc["_id"])
    if cls._versioned_rows:
        _set(inst, "_version", doc.get("_version") or 0)
    _set(inst, "_snapshot", _dirty.snapshot(row))
    return inst


def _set(inst: Any, name: str, value: Any) -> None:
    try:
        setattr(inst, name, value)
    except AttributeError:  # a frozen dataclass
        object.__setattr__(inst, name, value)


# ---- typed references -----------------------------------------------------------------

_TYPED: dict[type, dict[str, Position]] = {}


# the annotation of a field of a pydantic or msgspec class whose annotations are not resolved yet
_NOT_YET = object()

# ``Literal[...]`` in a string annotation: what it holds are values, never names
_LITERALS = re.compile(r"Literal\[[^\]]*\]")
# the parts of a string annotation: names (``typing.Optional``), ``...``, brackets, commas
# and ``|``
_TOKENS = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*|\.\.\.|[\[\],|]")
# names in a string annotation, by what they put at a position: their arguments there, in
# each entry of a list, in each value of a dict; or nothing (never a model class)
_UNION_NAMES = frozenset({"Optional", "Union", "Annotated"})
_ITEM_NAMES = frozenset(
    {"list", "List", "tuple", "Tuple", "set", "Set", "frozenset", "FrozenSet", "Sequence"}
    | {"MutableSequence", "Iterable", "Collection", "AbstractSet", "MutableSet"}
)
_VALUE_NAMES = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_NOT_A_MODEL = frozenset({"None", "Any", "ClassVar", "typing", "Literal", "..."})
_ANY_LENGTH = [("...", [])]  # the ``...`` of ``tuple[Child, ...]``, as :func:`_parse` gives it


def typed_fields(cls: Any) -> dict[str, Position]:
    """``{field: Position}`` for the fields of ``cls`` whose annotation puts a model class
    somewhere in its value (``Child``, ``Optional[Child]``, ``list[Child]``,
    ``dict[str, list[Child]]``), for the pydantic, the lite and the msgspec bases alike.

    The :class:`Position` follows the annotation's shape, and no deeper: a ``Union`` puts
    its arguments at the same place, a list type its argument in each entry, a dict type
    its value type in each value. Found once per class; an answer is not kept while a
    name in an annotation cannot be found yet (a class defined before the class it
    refers to).
    """
    if not _is_model_class(cls):
        return {}
    typed = _TYPED.get(cls)
    if typed is None:
        typed, complete = {}, True
        for name, ann, owner in _annotations(cls):
            at: dict = {}
            complete &= _shape(ann, owner, at)
            position = _position(at)
            if position is not None:
                typed[name] = position
        if complete:
            _TYPED[cls] = typed
    return typed


def _position(at: dict) -> Optional[Position]:
    """The :class:`Position` of what :func:`_shape` gathered, or None where it puts no model."""
    item = _position(at["item"]) if "item" in at else None
    value = _position(at["value"]) if "value" in at else None
    admits = tuple(dict.fromkeys(at.get("admits", ())))
    if not admits and item is None and value is None:
        return None
    return Position(admits, item, value, at.get("fixed", False))


def _annotations(cls: Any) -> list:
    """``(field, annotation, class that declares it)`` for each field of ``cls``."""
    fields = getattr(cls, "__dataclass_fields__", None)
    if fields is not None:  # a lite class: a string annotation is read where it was written
        return [(n, f.type, _declarer(cls, n)) for n, f in fields.items() if not n.startswith("_")]
    if hasattr(cls, "__struct_fields__"):  # a msgspec class
        names = [n for n in cls.__struct_fields__ if not n.startswith("_")]
        try:
            hints = typing.get_type_hints(cls)
        except NameError:  # it names a class that is not defined yet
            return [(n, _NOT_YET, cls) for n in names]
        return [(n, hints[n], cls) for n in names]
    if not getattr(cls, "__pydantic_complete__", True):
        return [(n, _NOT_YET, cls) for n in cls.model_fields]
    return [(n, f.annotation, cls) for n, f in cls.model_fields.items()]


def _declarer(cls: Any, name: str) -> Any:
    for klass in cls.__mro__:
        if name in vars(klass).get("__annotations__", {}):
            return klass
    return cls


def _is_model_class(x: Any) -> bool:
    # every pydantic and lite base has ``_versioned_rows``; a msgspec class stores its rows too
    return isinstance(x, type) and (
        hasattr(x, "_versioned_rows") or (hasattr(x, "__struct_fields__") and hasattr(x, "from_id"))
    )


def _shape(ann: Any, owner: Any, at: dict, within: tuple = ()) -> bool:
    """Gather in ``at`` what ``ann`` puts at a place: the model classes admitted there
    (``admits``), and what it puts in each entry of a list there (``item``) and in each
    value of a dict there (``value``). False when a name is not found.

    ``within``: the names a module gives to an annotation that are being read through; one
    that names itself (``type Tree = dict[str, Tree] | Child``) puts nothing more there.
    """
    if ann is _NOT_YET:
        return False
    if isinstance(ann, typing.TypeAliasType):  # ``type Children = list[Child]``
        if ann in within:
            return True
        within += (ann,)
        try:
            ann = ann.__value__
        except NameError:  # it names a class that is not defined yet
            return False
    if isinstance(ann, typing.ForwardRef):  # ``Optional["Category"]`` in a lite class
        ann = ann.__forward_arg__
    if isinstance(ann, str):
        terms, _ = _parse(_TOKENS.findall(_LITERALS.sub("", ann)), 0)
        return _shape_terms(terms, owner, at, within)
    if _is_model_class(ann):
        at.setdefault("admits", []).append(ann)
        return True
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if origin is typing.Literal or not args:
        return True  # a ``Literal`` holds values, not annotations
    if origin in (typing.Union, types.UnionType):
        return all([_shape(arg, owner, at, within) for arg in args])
    if origin is typing.Annotated:
        return _shape(args[0], owner, at, within)
    if isinstance(origin, type) and issubclass(origin, collections.abc.Mapping):
        return len(args) != 2 or _shape(args[1], owner, at.setdefault("value", {}), within)
    if isinstance(origin, type) and issubclass(origin, collections.abc.Collection):
        item = at.setdefault("item", {})
        if origin is tuple and Ellipsis not in args:
            at["fixed"] = True  # ``tuple[Child, Child]``: one entry per place
        return all([_shape(arg, owner, item, within) for arg in args if arg is not Ellipsis])
    return True  # ``type[Child]``, a ``Callable``: not a place a stored model is


def _parse(tokens: list, i: int) -> tuple[list, int]:
    """The terms of a string annotation from ``tokens[i]`` up to the ``,`` or ``]`` that
    ends them: ``[(name, args)]``, each of ``args`` such a list; and where they end."""
    terms: list = []
    while i < len(tokens) and tokens[i] not in ",]":
        name, i = tokens[i], i + 1
        if name == "|":
            continue
        args: list = []
        if name == "[" or (i < len(tokens) and tokens[i] == "["):
            i += name != "["
            while i < len(tokens) and tokens[i] != "]":
                arg, i = _parse(tokens, i)
                args.append(arg)
                i += i < len(tokens) and tokens[i] == ","
            i += 1
        terms.append((name if name == "..." else name.rsplit(".", 1)[-1], args))
    return terms, i


def _shape_terms(terms: list, owner: Any, at: dict, within: tuple = ()) -> bool:
    """:func:`_shape` of the terms of a string annotation (:func:`_parse`)."""
    complete = True
    for name, args in terms:
        if name in _UNION_NAMES:
            for arg in args[:1] if name == "Annotated" else args:
                complete &= _shape_terms(arg, owner, at, within)
        elif name in _ITEM_NAMES:
            if name in ("tuple", "Tuple") and args and _ANY_LENGTH not in args:
                at["fixed"] = True
            for arg in args:
                complete &= _shape_terms(arg, owner, at.setdefault("item", {}), within)
        elif name in _VALUE_NAMES:
            if len(args) == 2:
                complete &= _shape_terms(args[1], owner, at.setdefault("value", {}), within)
        elif name and name not in _NOT_A_MODEL:  # "" is what is left of "typing.Literal[...]"
            found = _named(name, owner)
            if found is None:
                complete = False
            elif _is_model_class(found):
                at.setdefault("admits", []).append(found)
            elif name not in within:  # a name the module gives to an annotation
                complete &= _shape(found, owner, at, within + (name,))
    return complete


def _named(name: str, owner: Any) -> Any:
    """What ``name`` in a string annotation of ``owner`` is, or None when it is not found."""
    space = vars(sys.modules.get(owner.__module__, builtins))
    found = space.get(name, getattr(builtins, name, None))
    if found is None:
        found = registry.resolve(_default_table_name(name)) or registry.resolve_class_name(name)
        if found is None or found.__name__ != name:
            return None
    return found


def read_rows(adapter: Any, table: str, ids: list, db: Any = None) -> Optional[tuple]:
    """``(documents, versions)`` of the rows ``ids``, or ``None`` when ``table`` does not exist.

    Read through ``db``'s by-id reader, as ``db.find_documents`` reads them; for the table of
    a versioned class bound to ``db`` the same statement reads the ``_version`` column, given
    as ``{id: version}`` and left out of the documents (0 where the table has no such column:
    the read changes no schema). Without a database (a queryset built by hand) ``adapter``
    reads ``_id`` and ``data`` only.
    """
    reader = getattr(db, "_read_rows", None)
    if reader is None:
        columns = _row_columns()
        rows = _select_rows(adapter, table, columns, ids)
        return None if rows is None else (_row_documents(rows, columns), None)
    versioned = _versioned_here(table, db)
    rows, columns = reader(table, ids, versioned=versioned, alter=False)
    return _documents(rows, columns, versioned)


async def aread_rows(adapter: Any, table: str, ids: list, db: Any = None) -> Optional[tuple]:
    """Async twin of :func:`read_rows`."""
    if db is None:
        columns = _row_columns()
        rows = await _aselect_rows(adapter, table, columns, ids)
        return None if rows is None else (_row_documents(rows, columns), None)
    versioned = _versioned_here(table, db)
    rows, columns = await db._read_rows(table, ids, versioned=versioned, alter=False)
    return _documents(rows, columns, versioned)


def _versioned_here(table: str, db: Any) -> bool:
    mdl = registry.resolve(table)
    # the classes the batch builds (see ``_reads``); another family (msgspec) has no versions
    return mdl is not None and _reads(mdl, db) and getattr(mdl, "_versioned_rows", False)


def _documents(rows: Any, columns: Any, versioned: bool) -> Optional[tuple]:
    if rows is None:
        return None
    if not versioned:
        return _row_documents(rows, columns), None
    # the version column (third) is kept apart, so a document is what an unversioned read gives
    docs = _row_documents([(r[0], r[1], *r[3:]) for r in rows], [*columns[:2], *columns[3:]])
    return docs, {r[0]: r[2] for r in rows}


# ---- the per-row resolver -----------------------------------------------------------


def plan(data: dict, typed: dict, db: Any, seen: frozenset) -> Optional[tuple[dict, list]]:
    """Sort the references at the typed positions of ``data`` (:func:`collect`) into what
    to load.

    Returns ``(groups, unbound)``: ``groups`` maps a table to ``(model, ids)`` for classes
    that are bound; ``unbound`` lists ``(model, table, id)`` for classes never bound,
    which are read from the parent's database. A reference already being loaded further
    up (``seen``) is left out and stays a reference. None when ``data`` holds no
    reference at a typed position.
    """
    found: dict[tuple[str, int], None] = {}
    if not collect(data, typed, found):
        return None
    groups: dict[str, tuple[Any, list[int]]] = {}
    unbound: list[tuple[Any, str, int]] = []
    for table, rid in found:
        if (table, rid) in seen:
            continue
        mdl = registry.resolve(table)
        if getattr(mdl, "_db", None) is None and db is not None:
            unbound.append((mdl, table, rid))
        elif hasattr(mdl, "from_id"):
            groups.setdefault(table, (mdl, []))[1].append(rid)
    return groups, unbound


def resolve(data: dict, db: Any, seen: frozenset, cls: Any) -> dict:
    """Replace the references at the typed positions of ``data``, a row of ``cls``
    (:func:`collect`), by model instances of the classes that own them.

    The rows of one table are read with one statement, by that class's ``_load_refs``,
    which resolves them by the same rule. A reference to a row being loaded further up
    (``seen``) stays a reference, which cuts a cycle, as does one to a row its class
    cannot hydrate (``KEEP``); one to a row that is not there leads to no row
    (:func:`replace`). ``data`` with no reference at a typed position is returned as it is.
    """
    typed = typed_fields(cls)
    planned = plan(data, typed, db, seen)
    if planned is None:
        return data
    groups, unbound = planned
    loaded: dict = {}
    for mdl, table, rid in unbound:
        # a class never bound: read the row from the parent's db; None when the row or
        # its table is not there
        loaded[(table, rid)] = _load_unbound(mdl, db, table, rid)
    for table, (mdl, ids) in groups.items():
        loader = getattr(mdl, "_load_refs", None)
        found: dict = {}
        if loader is not None:
            try:
                found = loader(ids, seen)
            except UNLOADABLE:
                found = dict.fromkeys(ids, KEEP)
        else:
            for rid in ids:
                try:
                    found[rid] = mdl.from_id(rid)
                except UNLOADABLE:
                    found[rid] = KEEP
        for rid in ids:
            loaded[(table, rid)] = found.get(rid)
    return replace(data, typed, loaded.get)


async def aresolve(data: dict, db: Any, seen: frozenset, cls: Any) -> dict:
    """Async twin of :func:`resolve`, through each class's ``_aload_refs``."""
    typed = typed_fields(cls)
    planned = plan(data, typed, db, seen)
    if planned is None:
        return data
    groups, unbound = planned
    loaded: dict = {}
    for mdl, table, rid in unbound:
        loaded[(table, rid)] = await _aload_unbound(mdl, db, table, rid)
    for table, (mdl, ids) in groups.items():
        loader = getattr(mdl, "_aload_refs", None)
        found: dict = {}
        if loader is not None:
            try:
                found = await loader(ids, seen)
            except UNLOADABLE:
                found = dict.fromkeys(ids, KEEP)
        else:
            for rid in ids:
                try:
                    fn = mdl.from_id
                    found[rid] = await fn(rid) if inspect.iscoroutinefunction(fn) else fn(rid)
                except UNLOADABLE:
                    found[rid] = KEEP
        for rid in ids:
            loaded[(table, rid)] = found.get(rid)
    return replace(data, typed, loaded.get)


# ---- versioned rows for the safe bases ----------------------------------------------


def read_versioned(db: Any, table: str, ids: list) -> list:
    """Documents of ``ids`` with ``_version``, in the order of ``ids``, one statement; a
    missing table has no rows."""
    if not ids:
        return []
    rows, columns = db._read_rows(table, ids, versioned=True)
    return _row_documents(_in_order(rows or [], ids), columns)


async def aread_versioned(db: Any, table: str, ids: list) -> list:
    """Async twin of :func:`read_versioned`."""
    if not ids:
        return []
    rows, columns = await db._read_rows(table, ids, versioned=True)
    return _row_documents(_in_order(rows or [], ids), columns)

