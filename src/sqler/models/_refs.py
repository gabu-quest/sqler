"""References between documents: the one loader every way of loading uses.

A stored reference is a dict holding ``_table`` and ``_id``. Where the annotation of a field
puts a model class (:func:`typed_fields`), a reference into a table of a class admitted there
is the model of its row; anywhere else it is data, and stays the dict it is.

:func:`load` is the one loader. ``from_id``, ``refresh``, ``from_ids``, ``query().all()`` and
``first()``, sync and async, on every model base, give it the rows they read. It reads the
rows their references lead to level by level, one statement per table per level, to the end
of every chain, then builds one model per row: within one load a row reached twice is one
object, and a cycle closes on its objects. Nothing in it recurses once per level of data. It
yields its reads, so one generator serves a sync reader (:func:`run`) and an async one
(:func:`arun`).

:func:`validate` turns a document into a model for every way in, the loader's included. A
reference the document still holds at a typed position (a load without resolution) stays
the stored reference on every base.
"""

from __future__ import annotations

import builtins
import collections.abc
import dataclasses
import inspect
import re
import sys
import types
import typing
import weakref
from typing import Any, Callable, Generator, Optional

from sqler import registry
from sqler.db._rows import _in_order, _row_columns, _row_documents, _select_rows
from sqler.db.async_db import _aselect_rows
from sqler.exceptions import InvalidTableNameError
from sqler.models import _dirty
from sqler.models._table_names import _Home, _set_home, _unchecked_table_name
from sqler.utils import is_ref_dict, validate_table_name

# What a row its class refuses raises, on every base (pydantic's ``ValidationError`` is a
# ``ValueError``; a lite class missing a field raises ``TypeError``; msgspec's
# ``ValidationError`` is neither): the loader adds a note naming the row and where it was
# referenced. An error of the database is none of these, and reaches the caller as it is.
UNLOADABLE: tuple = (LookupError, RuntimeError, TypeError, ValueError)
try:
    from msgspec import ValidationError as _MsgspecRefused
except ImportError:  # msgspec is an optional extra
    pass
else:
    UNLOADABLE += (_MsgspecRefused,)

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1

Key = tuple[str, int]


def ref_key(value: dict) -> Optional[Key]:
    """``(table, id)`` of a reference (:func:`sqler.utils.is_ref_dict`), or None if it is not
    usable: its ``_id`` does not fit SQLite's 64-bit INTEGER. A reference that is not usable
    is left as it is, and not walked into. The name is not checked: it is data we did not
    write, and :func:`load` checks it once per table where it puts it into SQL.
    """
    table, rid = value["_table"], value["_id"]
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


def collect(doc: dict, typed: dict, out: dict[Key, None]) -> bool:
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


def _collect(value: Any, at: Position, out: dict[Key, None]) -> bool:
    admits, item, inner, _ = at
    held = False
    if isinstance(value, dict):
        if is_ref_dict(value):
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


def replace(doc: dict, typed: dict, get: Callable[[Key], Any]) -> dict:
    """A copy of ``doc`` with ``get(key)``, the model of the row, in place of each reference
    :func:`collect` takes from it. ``get`` gives None for a row that is not there (gone, or
    its table missing). Such a reference, and every other one at a place where a model is
    admitted (not usable, or into another class's table), leads to no row: it reads as
    ``None`` where it is the field's value or an entry of a list with one entry per place
    (``tuple[Child, Child]``), and is left out where it is an entry of any other list or a
    value of a dict."""
    out = {}
    for k, v in doc.items():
        at = typed.get(k)
        if at is not None:
            v = _replace(v, at, get)
            if v is _NO_ROW:
                v = None
        out[k] = v
    return out


def _replace(value: Any, at: Position, get: Callable[[Key], Any]) -> Any:
    admits, item, inner, fixed = at
    if isinstance(value, dict):
        if is_ref_dict(value):
            if not admits:
                return value  # data where the annotation puts no model: its validation decides
            key = ref_key(value)
            if key is None or not _admits(key[0], admits):
                return _NO_ROW
            got = get(key)
            return _NO_ROW if got is None else got
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


# ---- the loader -----------------------------------------------------------------------

# A read the loader asks for: ``(db, table, ids, versioned)``; ``db`` None reads through the
# caller's adapter. It is sent what :func:`read_rows` gives: ``(documents, versions)``, or
# None when the table does not exist.
Read = tuple[Any, str, list, bool]


def load(
    cls: Any,
    rows: list[dict],
    db: Any,
    table: str,
    make: Optional[Callable[[dict, dict], Any]] = None,
    own: Optional[dict] = None,
) -> Generator[Read, Any, list]:
    """The models of ``rows``, rows of ``cls`` read from ``table`` of ``db``, in their order.

    Every reference at a typed position (:func:`collect`) is followed to the end of its
    chain. The row a reference leads to is read from the database its class is bound to,
    where its ``save()`` writes it back; a row of a class bound to none is read from the
    database the referring row was read from (``db`` for ``rows``; None: the caller's
    adapter). A row reached twice in one load is one object: a diamond shares
    its child, a cycle closes on its objects, and a row of ``rows`` that another row
    references is its own result. A reference to a row that is not there leads to no row
    (:func:`replace`). A referenced row its class refuses raises what building it raises,
    with a note naming the row and the field that referenced it.

    Every row is built by :func:`from_row`, but a row whose model is an object that exists
    already. Each model's home (:class:`_Home`) is the database and table its row was read
    from. ``own``: ``{(table, id): object}`` of those rows (``refresh()`` passes the
    instance it refreshes): a reference to the row is that object, and
    ``make(document, row)`` reads one of ``rows`` that ``own`` holds onto its object, from
    its document with references in place and the row as read, and returns the object.
    """
    own = dict(own or {})
    keys = [(table, row["_id"]) for row in rows]
    home = _Home(db, table)
    if not typed_fields(cls):
        return [
            _homed(make(row, row), home) if key in own else from_row(cls, row, row, home)  # type: ignore[misc]
            for key, row in zip(keys, rows)
        ]
    built = yield from _walk(cls, rows, keys, db, own, make)
    return [built[k] for k in keys]


def _walk(
    cls: Any,
    rows: list,
    keys: list,
    db: Any,
    shells: dict,
    make: Any = None,
    only_linked: bool = False,
) -> Generator[Read, Any, dict]:
    """Read and build the graph of ``rows`` (see :func:`load`); ``{key: model}`` of every
    row built. ``shells``: ``{key: object}`` of rows whose model is that object (``own``);
    ``make`` reads one of ``rows`` onto it. ``only_linked`` builds none of ``rows``, only
    the rows they lead to."""
    docs: dict[Key, tuple] = {}  # (class, document with ``_version``, row as read, home)
    links: dict[Key, list] = {}  # the rows each document references, in order
    via: dict[Key, tuple] = {}  # where a row was first referenced: (referring key, field)
    tops: set = set()
    roots: list = []
    frontier: list = []  # (key, the database of the referring row)
    homes = {table: _Home(db, table) for table in {key[0] for key in keys}}
    for key, row in zip(keys, rows):
        found = _links(key, row, cls, via)
        frontier.extend((k, db) for k in found)
        if only_linked:
            roots.extend(found)
        else:
            docs[key], links[key] = (cls, row, row, homes[key[0]]), found
            tops.add(key)
            roots.append(key)
    asked: set = set()
    while frontier:
        reads: dict = {}
        for key, parent_db in frontier:
            if key in asked or key in docs or key in shells:
                continue
            asked.add(key)
            table, rid = key
            if not _is_table_name(table):
                continue  # never put into SQL: it leads to no row
            mdl = registry.resolve(table)  # a class the field admits (see ``collect``)
            bound = getattr(mdl, "_db", None)
            rdb = parent_db if bound is None else bound
            reads.setdefault((table, id(rdb)), (mdl, rdb, []))[2].append(rid)
        frontier = []
        for (table, _), (mdl, rdb, ids) in reads.items():
            versioned = bool(getattr(mdl, "_versioned_rows", False))
            got = yield rdb, table, ids, versioned
            if got is None:
                continue  # the table is missing: its rows are not there
            found, versions = got
            home = _Home(rdb, table)
            for row in found:
                rid = row["_id"]
                key = (table, rid)
                # a child's version is its column's, never its JSON's
                doc = {**row, "_version": (versions or {}).get(rid, 0)} if versioned else row
                docs[key], links[key] = (mdl, doc, row, home), _links(key, doc, mdl, via)
                frontier.extend((k, rdb) for k in links[key])

    built: dict = {}
    blanks: dict = {}  # {key: object} of rows a cycle reached before they were built

    def get(key: Key) -> Any:
        got = built.get(key)
        if got is None:
            got = shells.get(key)
            if got is None:
                got = blanks.get(key)
            if got is None and key in docs:
                # a row further up this walk, not built yet: a cycle. The reference is
                # given the object the row is then built on.
                try:
                    got = blanks[key] = _blank(docs[key][0])
                except UNLOADABLE as exc:
                    _note(exc, key, via)
                    raise
        return got

    def build(key: Key) -> None:
        mdl, doc, row, home = docs[key]
        resolved = replace(doc, typed_fields(mdl), get)
        if key in shells:
            built[key] = _homed(
                make(resolved, row), home
            )  # its model exists: the row is read onto it
            return
        try:
            # on the object a cycle holds for the row, when one reached it first
            built[key] = from_row(mdl, resolved, row, home, blanks.pop(key, None))
        except UNLOADABLE as exc:
            if key not in tops:
                _note(exc, key, via)
            raise

    # children before parents, without recursion: a stack of (row, its links not yet seen)
    state: set = set()
    for root in roots:
        if root in state or root not in docs:
            continue
        state.add(root)
        stack = [(root, iter(links[root]))]
        while stack:
            key, pending = stack[-1]
            for child in pending:
                if child in docs and child not in state:
                    state.add(child)
                    stack.append((child, iter(links[child])))
                    break
            else:
                stack.pop()
                build(key)
    return built


def _note(exc: BaseException, key: Key, via: dict) -> None:
    """Name on ``exc`` the row ``key`` and the field of the row that referenced it."""
    (ptable, pid), name = via[key]
    exc.add_note(
        f"loading row {key[1]} of {key[0]!r}, referenced by field {name!r} of row {pid} of {ptable!r}"
    )


def _links(key: Any, doc: dict, mdl: Any, via: dict) -> list:
    """The rows ``doc`` (row ``key`` of ``mdl``) references at its typed positions, in
    order, once each; each recorded in ``via`` with this row and the field, if it is new."""
    out: dict = {}
    for name, at in typed_fields(mdl).items():
        if name in doc:
            found: dict = {}
            _collect(doc[name], at, found)
            for k in found:
                out[k] = None
                via.setdefault(k, (key, name))
    return list(out)


def resolve(data: dict, db: Any, own: Any, cls: Any) -> dict:
    """``data``, a row of ``cls``, with the model of its row in place of each reference at a
    typed position, loaded through ``db`` as :func:`load` loads it. ``own``: ``{(table, id):
    object}`` of rows whose model exists already (see :func:`load`). The row of ``data``
    itself is not built: a reference back to it is read and built as any other row."""
    return run(_resolved(data, db, own, cls))


async def aresolve(data: dict, db: Any, own: Any, cls: Any) -> dict:
    """Async twin of :func:`resolve`."""
    return await arun(_resolved(data, db, own, cls))


def _resolved(data: dict, db: Any, own: Any, cls: Any) -> Generator[Read, Any, dict]:
    typed = typed_fields(cls)
    if not typed:
        return data
    own = dict(own or {})
    key = (_unchecked_table_name(cls), data.get("_id"))
    built = yield from _walk(cls, [data], [key], db, dict(own), only_linked=True)
    return replace(data, typed, lambda k: built[k] if k in built else own.get(k))


def models(cls: Any, rows: list[dict], db: Any, table: str) -> list:
    """The models of ``rows`` as :func:`load` gives them, its reads made with sync reads.
    A class with no typed field has no reference to follow, so its rows are built here,
    with no walk to start."""
    if typed_fields(cls):
        return run(load(cls, rows, db, table))
    home = _Home(db, table)
    return [from_row(cls, row, row, home) for row in rows]


async def amodels(cls: Any, rows: list[dict], db: Any, table: str) -> list:
    """Async twin of :func:`models`."""
    if typed_fields(cls):
        return await arun(load(cls, rows, db, table))
    home = _Home(db, table)
    return [from_row(cls, row, row, home) for row in rows]


def run(walk: Generator[Read, Any, Any], adapter: Any = None) -> Any:
    """What the loader ``walk`` returns, its reads made with sync reads (:func:`read_rows`);
    ``adapter`` reads a row whose database is not known."""
    try:
        want = next(walk)
        while True:
            want = walk.send(read_rows(adapter, *want))
    except StopIteration as done:
        return done.value


async def arun(walk: Generator[Read, Any, Any], adapter: Any = None) -> Any:
    """Async twin of :func:`run`."""
    try:
        want = next(walk)
        while True:
            want = walk.send(await aread_rows(adapter, *want))
    except StopIteration as done:
        return done.value


# ---- building a model ---------------------------------------------------------------


class _Kind:
    """What a class is, found once per class (:func:`_kind`): a pydantic class takes longer
    to say it lacks an attribute than a row takes to build, so no row asks the class.

    ``model``: a class sqler stores the rows of; ``struct``: a msgspec class; ``lite``: a
    dataclass; ``pydantic``: a class with a pydantic validator; ``forbids``: a pydantic class
    set to refuse a key it has no field for (``extra="forbid"``); ``set``: how an attribute
    of one of its objects is set, frozen or not (:func:`_set`); ``gone``: a weak reference
    to the class that takes the answer out of :data:`_KINDS` as the class goes, and that
    :func:`_kind` checks an answer against (an id is used again once its class is gone).
    """

    __slots__ = ("model", "struct", "lite", "pydantic", "forbids", "set", "gone")

    set: "typing.Callable[[typing.Any, str, typing.Any], None]"  # sets an attribute of an instance

    def __init__(self, cls: type) -> None:
        key = id(cls)
        self.gone = weakref.ref(cls, lambda _: _KINDS.pop(key, None))
        self.struct = hasattr(cls, "__struct_fields__")
        self.lite = dataclasses.is_dataclass(cls)
        self.pydantic = hasattr(cls, "__pydantic_validator__")
        # a pydantic class has ``model_config``; ``type`` does not declare it
        self.forbids = self.pydantic and getattr(cls, "model_config").get("extra") == "forbid"
        # every pydantic and lite base has ``_versioned_rows``; a msgspec class stores its rows too
        self.model = hasattr(cls, "_versioned_rows") or (self.struct and hasattr(cls, "from_id"))
        if self.struct:
            from msgspec.structs import force_setattr  # only a msgspec class gets here

            self.set = force_setattr
        elif getattr(getattr(cls, "__dataclass_params__", None), "frozen", False):
            # not ``setattr`` and its refusal: with ``slots=True`` a frozen dataclass's
            # ``__setattr__`` raises ``TypeError`` for a name that is not a field
            self.set = object.__setattr__
        else:
            self.set = setattr


# {id(class): its :class:`_Kind`}: by id, not by the class, so a class nothing else holds
# leaves it (``_Kind.gone``), and an ask costs a third of a ``WeakKeyDictionary``'s
_KINDS: dict[int, _Kind] = {}


def _kind(cls: type) -> _Kind:
    """The :class:`_Kind` of ``cls``; the first ask finds it, and every ask after reads it."""
    kind = _KINDS.get(id(cls))
    if kind is None or kind.gone() is not cls:  # an id is used again once its class is gone
        kind = _KINDS[id(cls)] = _Kind(cls)
    return kind


def from_row(cls: Any, doc: dict, row: dict, home: Optional[_Home], into: Any = None) -> Any:
    """A model of the row ``doc`` that knows its row: ``_id``, ``_version`` for a class
    whose rows are versioned, its snapshot (:func:`_dirty.snapshot`) and ``home``, the
    database and table it was read from.

    ``doc`` is the document the model is validated from: the row with its references
    resolved. ``row`` is the row as it was read, before that: the snapshot is taken from it,
    so it is what the database held. A site that resolves nothing passes one dict for both.
    ``into``: the object (:func:`_blank`) the model is built on (see :func:`validate`).

    The one step every way in uses to turn a row into a model, for every model base.
    """
    inst = validate(cls, doc, into)
    kind = _kind(cls)
    put = (kind if type(inst) is cls else _kind(type(inst))).set  # as :func:`_set` sets
    put(inst, "_id", doc["_id"])
    if getattr(cls, "_versioned_rows", False):
        put(inst, "_version", doc.get("_version") or 0)
    put(inst, "_snapshot", _dirty.snapshot(row))
    if kind.lite:
        # not vars(): that makes a dict for the instance, a tenth of a lite model's read time
        object.__setattr__(inst, "_home", home)
    else:
        vars(inst)["_home"] = home  # _set_home, inline: once per row read
    return inst


def _homed(inst: Any, home: Optional[_Home]) -> Any:
    _set_home(inst, home)
    return inst


def validate(cls: Any, doc: dict, into: Any = None) -> Any:
    """``cls.model_validate(doc)``, where a stored reference ``doc`` holds at a typed position
    stays that stored reference, on every base: the field holds the dict, never a model built
    from it. A lite class takes it as it is; a pydantic or msgspec class is given an object
    of the field's class in its place (:func:`_blank`), and the reference is put back once it
    is validated.

    ``into``: an object of ``cls`` from :func:`_blank` that the model is built on, so that a
    reference already given it holds the row's model (a cycle); the model is ``into``.
    """
    typed = typed_fields(cls)
    kind = _kind(cls)
    if not typed:
        return cls.model_validate(_fields_of(doc) if kind.forbids else doc)
    held = {}
    kept: dict = {}
    if not kind.lite:
        for name, at in typed.items():
            if name in doc:
                try:
                    value = _stand_in(doc[name], at, kept)
                except UNLOADABLE as exc:
                    exc.add_note(
                        f"making a stand-in for a reference in field {name!r} of row"
                        f" {doc.get('_id')} of {cls.__name__}"
                    )
                    raise
                if value is not doc[name]:
                    held[name] = value
    data = {**doc, **held} if held else doc
    if kind.forbids:
        data = _fields_of(data)
    inst = cls.model_validate(data) if into is None else _build_on(into, cls, data)
    for name in held:
        _put(inst, name, _put_back(getattr(inst, name), kept))
    return inst


def _fields_of(doc: dict) -> dict:
    """The row ``doc`` without the keys sqler keeps beside its fields. A class that forbids
    extra keys is given this: ``_id`` and ``_version`` are set on the model once it is built
    (:func:`from_row`), and every other key it has no field for it still refuses."""
    return {k: v for k, v in doc.items() if k not in ("_id", "_version")}


def _build_on(into: Any, cls: Any, data: dict) -> Any:
    """``into`` (:func:`_blank`) made the model ``cls.model_validate(data)`` gives.

    The class builds on it where its library can: pydantic validates onto an instance and a
    lite class's ``__init__`` runs on it, so a hook that keeps ``self`` keeps ``into``. Any
    other class builds its model as it always does, and the model's state is moved onto
    ``into`` (:func:`_become`): msgspec, which builds a new object only; a class with its own
    ``model_validate``; a pydantic validator that returns another object than it was given.
    """
    obj = None
    kind = _kind(cls)
    if not _own_model_validate(cls):  # asked each time: a class can be given one later
        if kind.pydantic:
            obj = cls.__pydantic_validator__.validate_python(data, self_instance=into)
        elif kind.lite:
            into.__init__(**cls._init_fields(data))
            if "_id" in data:
                object.__setattr__(into, "_id", data["_id"])
            return into
    if obj is None:
        obj = cls.model_validate(data)
    if obj is not into:
        _become(into, obj)
    return into


def _own_model_validate(cls: Any) -> bool:
    """True when ``cls.model_validate`` is one a class of the user's defines, in place of
    the one its base (sqler's, or pydantic's) gives."""
    for base in cls.__mro__:
        if "model_validate" in vars(base):
            return base.__module__.split(".", 1)[0] not in ("sqler", "pydantic")
    return True


def _stand_in(value: Any, at: Position, kept: dict) -> Any:
    """``value`` with an object of the admitted class in place of each stored reference at a
    place where a model is admitted (``kept``: ``{id(object): (object, reference)}``);
    ``value`` itself when it holds none."""
    admits, item, inner, _ = at
    before = len(kept)
    if isinstance(value, dict):
        if is_ref_dict(value):
            if not admits:
                return value
            blank = _blank(admits[0])
            kept[id(blank)] = (blank, value)
            return blank
        if inner is None:
            return value
        out: Any = {k: _stand_in(v, inner, kept) for k, v in value.items()}
    elif isinstance(value, list) and item is not None:
        out = [_stand_in(v, item, kept) for v in value]
    else:
        return value
    return out if len(kept) > before else value


def _put_back(value: Any, kept: dict) -> Any:
    """``value`` as validated, with each stand-in of ``kept`` replaced by its reference."""
    hit = kept.get(id(value))
    if hit is not None and hit[0] is value:
        return hit[1]
    if type(value) is dict:
        return {k: _put_back(v, kept) for k, v in value.items()}
    if type(value) in (list, tuple):
        return type(value)(_put_back(v, kept) for v in value)
    return value


def _put(inst: Any, name: str, value: Any) -> None:
    """Set field ``name`` of ``inst`` to ``value`` as it is: no validation, frozen or not."""
    kind = _kind(type(inst))
    if kind.struct:
        kind.set(inst, name, value)  # msgspec's ``force_setattr``
    else:
        inst.__dict__[name] = value


# what a pydantic model holds besides its fields, as a model not yet validated holds it
_PYDANTIC_BLANK = (
    ("__pydantic_fields_set__", set),
    ("__pydantic_extra__", lambda: None),
    ("__pydantic_private__", lambda: None),  # its validation fills it, defaults first
)


def _blank(mdl: Any) -> Any:
    """An object of ``mdl`` that holds no row yet: a stand-in, or the object a row in a cycle
    is built on (:func:`_build_on`). No hook of the class runs on it and nothing is
    validated. On pydantic its fields with a default hold it; on lite no field is set; on
    msgspec every field holds ``None``.
    """
    kind = _kind(mdl)
    if kind.struct:
        from msgspec import Struct  # only a msgspec model gets here

        empty = _EMPTY.get(mdl)
        if empty is None:
            empty = _EMPTY[mdl] = _empty(mdl)
        return Struct.__copy__(empty)  # type: ignore[attr-defined]  # msgspec's stubs leave out Struct.__copy__, which it has; a Struct's copy runs no ``__post_init__``
    obj = object.__new__(mdl)
    if kind.pydantic:
        # each field that has a default holds it, as ``model_construct()`` gives (which
        # also runs ``model_post_init``)
        defaults = {
            name: f.get_default(call_default_factory=True)
            for name, f in mdl.model_fields.items()
            if not f.is_required()
        }
        object.__setattr__(obj, "__dict__", defaults)
        for name, empty in _PYDANTIC_BLANK:
            object.__setattr__(obj, name, empty())
    return obj


_EMPTY: dict[type, Any] = {}  # {msgspec class: its :func:`_empty` object}; a blank is a copy


class _Unhooked:
    """Put before a msgspec class, makes a subclass of the same layout on which neither the
    class's ``__post_init__`` nor, when the subclass is made, its ``__init_subclass__`` runs."""

    __slots__ = ()

    def __post_init__(self) -> None:
        pass

    @classmethod
    def __init_subclass__(cls, **kwargs: Any) -> None:
        pass


def _empty(mdl: Any) -> Any:
    """An object of the msgspec class ``mdl`` with ``None`` in every field, on which no
    ``__post_init__`` ran. msgspec makes an object only by calling its class, which runs the
    hook (a hook that reads a field would read ``None``), and takes no object of another
    class where ``mdl`` is expected. So for a class with a hook the object is made by a
    subclass whose hook does nothing (:class:`_Unhooked`) and is then given ``mdl`` as its
    class. The subclass is dropped; nothing of the user's sees it.
    """
    nothing = dict.fromkeys(mdl.__struct_fields__)
    if getattr(mdl, "__post_init__", None) is None:
        return mdl(**nothing)  # a Struct's constructor checks no types
    from msgspec.structs import force_setattr  # only a msgspec model gets here

    try:
        obj = type(mdl)(mdl.__name__, (_Unhooked, mdl), {})(**nothing)
        force_setattr(obj, "__class__", mdl)  # frozen or not
    except TypeError as exc:
        exc.add_note(f"making an object of {mdl.__name__} without running its __post_init__")
        raise
    return obj


def _become(shell: Any, obj: Any) -> None:
    """Make ``shell`` (:func:`_blank`) the model ``obj``, an object of its class built apart
    from it (see :func:`_build_on`): ``shell`` holds the state a copy of ``obj`` would."""
    if type(shell) is not type(obj):
        raise TypeError(
            f"{type(shell).__name__}: its row was built as {type(obj).__name__}, which cannot"
            " take the place of the object its cycle holds"
        )
    kind = _kind(type(obj))
    if kind.struct:
        for name in type(obj).__struct_fields__:
            kind.set(shell, name, getattr(obj, name))  # msgspec's ``force_setattr``
        state = getattr(obj, "__dict__", None)
        if state is not None:
            shell.__dict__.clear()
            shell.__dict__.update(state)
        return
    # as ``copy.copy`` moves state: what ``__reduce_ex__`` gives, set by ``__setstate__``
    # where the class has one (pydantic; a frozen dataclass with slots)
    state = obj.__reduce_ex__(4)[2]
    if hasattr(shell, "__setstate__"):
        shell.__setstate__(state)
        return
    slots = None
    if isinstance(state, tuple) and len(state) == 2:
        state, slots = state
    if state:
        shell.__dict__.update(state)
    for name, value in (slots or {}).items():
        object.__setattr__(shell, name, value)


def _set(inst: Any, name: str, value: Any) -> None:
    """Set attribute ``name`` of the model ``inst``, frozen or not."""
    cls = type(inst)
    _kind(cls).set(inst, name, value)


# ---- typed references -----------------------------------------------------------------

# {model class: (its typed fields, the ``registry.state()`` they hold for)}; the state is None
# unless a name came from the registry, where a class defined later can be meant too. The
# class is held weakly, so one nothing else holds leaves it (a class with a typed field that
# refers to itself is held by that field's position)
_TYPED: "weakref.WeakKeyDictionary[type, tuple[dict[str, Position], Optional[int]]]" = (
    weakref.WeakKeyDictionary()
)


# the annotation of a field of a pydantic or msgspec class whose annotations are not resolved yet
_NOT_YET = object()
# {pydantic class whose annotations did not resolve: ``registry.state()`` when it was last asked}
_UNRESOLVED: "weakref.WeakKeyDictionary[type, int]" = weakref.WeakKeyDictionary()

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
_ANY_LENGTH: list[tuple[str, list]] = [
    ("...", [])
]  # the ``...`` of ``tuple[Child, ...]``, as :func:`_parse` gives it


def typed_fields(cls: Any) -> dict[str, Position]:
    """``{field: Position}`` for the fields of ``cls`` whose annotation puts a model class
    somewhere in its value (``Child``, ``Optional[Child]``, ``list[Child]``,
    ``dict[str, list[Child]]``), for the pydantic, the lite and the msgspec bases alike.

    The :class:`Position` follows the annotation's shape, and no deeper: a ``Union`` puts
    its arguments at the same place, a list type its argument in each entry, a dict type
    its value type in each value. Found once per class; an answer is not kept while a
    name in an annotation cannot be found yet (a class defined before the class it
    refers to).

    A name in a string annotation is what the module of the class calls that. A name the
    module does not have (a class imported only under ``TYPE_CHECKING``) means every model
    class of that name, those defined later too.
    """
    return _typed_fields(cls)[0]


def known_typed_fields(cls: Any) -> Optional[dict[str, Position]]:
    """:func:`typed_fields` of ``cls``, or None while a name in one of its annotations cannot
    be found yet (any field may then put a model anywhere)."""
    typed, complete = _typed_fields(cls)
    return typed if complete else None


def _typed_fields(cls: Any) -> tuple[dict[str, Position], bool]:
    kept = _TYPED.get(cls)  # it holds model classes only
    if kept is not None and (kept[1] is None or kept[1] == registry.state()):
        return kept[0], True
    if not _is_model_class(cls):
        return {}, True
    typed: dict[str, Position] = {}
    complete, state, now = True, None, registry.state()
    for name, ann, owner in _annotations(cls):
        at: dict = {}
        names = _Names(owner)
        complete &= _shape(ann, names, at)
        if names.registered:
            state = now
        position = _position(at)
        if position is not None:
            typed[name] = position
    if complete:
        _TYPED[cls] = (typed, state)
    return typed, complete


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
    kind = _kind(cls)
    if kind.lite:  # a string annotation is read where it was written
        fields = cls.__dataclass_fields__
        return [(n, f.type, _declarer(cls, n)) for n, f in fields.items() if not n.startswith("_")]
    if kind.struct:  # a msgspec class
        names = [n for n in cls.__struct_fields__ if not n.startswith("_")]
        try:
            hints = typing.get_type_hints(cls)
        except NameError:  # it names a class that is not defined yet
            return [(n, _NOT_YET, cls) for n in names]
        return [(n, hints[n], cls) for n in names]
    if not kind.pydantic:  # told as every pydantic 2 tells it (``__pydantic_fields__`` is 2.10's)
        return []  # a lite class left without ``@dataclass``: no field to read
    if not getattr(cls, "__pydantic_complete__", True) and not _resolves(cls):
        return [(n, _NOT_YET, cls) for n in cls.model_fields]
    return [(n, f.annotation, cls) for n, f in cls.model_fields.items()]


def _resolves(cls: Any) -> bool:
    """Whether the annotations of the pydantic class ``cls``, which named a class that was
    not defined yet when it was, resolve now.

    pydantic resolves them when the class is first used. A class nothing has used yet is
    asked here, as pydantic asks it then, with the names of its own module and of the place
    it was defined and no others: until it resolves, any field of it may hold a model, and
    every delete reads its whole table. Asked again only after a model class was defined
    (a name can also turn up in another way; the class's first use finds it then).
    """
    state = registry.state()
    if _UNRESOLVED.get(cls) == state:
        return False
    cls.model_rebuild(raise_errors=False, _parent_namespace_depth=0)
    if cls.__pydantic_complete__:
        _UNRESOLVED.pop(cls, None)
        return True
    _UNRESOLVED[cls] = state
    return False


def _declarer(cls: Any, name: str) -> Any:
    for klass in cls.__mro__:
        if name in vars(klass).get("__annotations__", {}):
            return klass
    return cls


def _is_model_class(x: Any) -> bool:
    return isinstance(x, type) and _kind(x).model


def _shape(ann: Any, names: "_Names", at: dict, within: tuple = ()) -> bool:
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
        return _shape_terms(terms, names, at, within)
    if _is_model_class(ann):
        at.setdefault("admits", []).append(ann)
        return True
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if origin is typing.Literal or not args:
        return True  # a ``Literal`` holds values, not annotations
    if origin in (typing.Union, types.UnionType):
        return all([_shape(arg, names, at, within) for arg in args])
    if origin is typing.Annotated:
        return _shape(args[0], names, at, within)
    if isinstance(origin, type) and issubclass(origin, collections.abc.Mapping):
        return len(args) != 2 or _shape(args[1], names, at.setdefault("value", {}), within)
    if isinstance(origin, type) and issubclass(origin, collections.abc.Collection):
        item = at.setdefault("item", {})
        if origin is tuple and Ellipsis not in args:
            at["fixed"] = True  # ``tuple[Child, Child]``: one entry per place
        return all([_shape(arg, names, item, within) for arg in args if arg is not Ellipsis])
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


def _shape_terms(terms: list, names: "_Names", at: dict, within: tuple = ()) -> bool:
    """:func:`_shape` of the terms of a string annotation (:func:`_parse`)."""
    complete = True
    for name, args in terms:
        if name in _UNION_NAMES:
            for arg in args[:1] if name == "Annotated" else args:
                complete &= _shape_terms(arg, names, at, within)
        elif name in _ITEM_NAMES:
            if name in ("tuple", "Tuple") and args and _ANY_LENGTH not in args:
                at["fixed"] = True
            for arg in args:
                complete &= _shape_terms(arg, names, at.setdefault("item", {}), within)
        elif name in _VALUE_NAMES:
            if len(args) == 2:
                complete &= _shape_terms(args[1], names, at.setdefault("value", {}), within)
        elif name and name not in _NOT_A_MODEL:  # "" is what is left of "typing.Literal[...]"
            found = names.here(name)
            if found is None:  # not a name of the module: every model class called that
                classes = names.anywhere(name)
                at.setdefault("admits", []).extend(classes)
                complete &= bool(classes)
            elif _is_model_class(found):
                at.setdefault("admits", []).append(found)
            elif name not in within:  # a name the module gives to an annotation
                complete &= _shape(found, names, at, within + (name,))
    return complete


class _Names:
    """What the names in a string annotation of the class ``owner`` are."""

    __slots__ = ("owner", "registered")

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.registered = False  # whether the registry was asked: its answer can grow

    def here(self, name: str) -> Any:
        """What the owner's module, or the builtins, call ``name``; None when neither does."""
        space = vars(sys.modules.get(self.owner.__module__, builtins))
        return space.get(name, getattr(builtins, name, None))

    def anywhere(self, name: str) -> tuple[type, ...]:
        """Every registered model class called ``name``; none when it is not found yet."""
        self.registered = True
        return registry.classes_named(name)


def read_rows(adapter: Any, db: Any, table: str, ids: list, versioned: bool) -> Optional[tuple]:
    """``(documents, versions)`` of the rows ``ids``, or ``None`` when ``table`` does not exist.

    Read through ``db``'s by-id reader, as ``db.find_documents`` reads them; for a
    ``versioned`` read the same statement reads the ``_version`` column, given as
    ``{id: version}`` and left out of the documents (0 where the table has no such column:
    the read changes no schema). Without a database (a queryset built by hand) ``adapter``
    reads ``_id`` and ``data`` only.
    """
    if db is None:
        columns = _row_columns()
        rows = _select_rows(adapter, table, columns, ids)
        return None if rows is None else (_row_documents(rows, columns), None)
    rows, columns = db._read_rows(table, ids, versioned=versioned)
    return _documents(rows, columns, versioned)


async def aread_rows(
    adapter: Any, db: Any, table: str, ids: list, versioned: bool
) -> Optional[tuple]:
    """Async twin of :func:`read_rows`. A class bound to a sync database is read through it."""
    if db is None:
        columns = _row_columns()
        rows = await _aselect_rows(adapter, table, columns, ids)
        return None if rows is None else (_row_documents(rows, columns), None)
    got = db._read_rows(table, ids, versioned=versioned)
    rows, columns = await got if inspect.isawaitable(got) else got
    return _documents(rows, columns, versioned)


def _documents(rows: Any, columns: Any, versioned: bool) -> Optional[tuple]:
    if rows is None:
        return None
    if not versioned:
        return _row_documents(rows, columns), None
    # the version column (third) is kept apart, so a document is what an unversioned read gives
    docs = _row_documents([(r[0], r[1], *r[3:]) for r in rows], [*columns[:2], *columns[3:]])
    return docs, {r[0]: r[2] for r in rows}


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
