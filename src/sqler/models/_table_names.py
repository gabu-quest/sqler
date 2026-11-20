"""Shared helpers for deriving default SQLite table names from model class names.

These were previously duplicated byte-for-byte across the three model backends
(Pydantic, Lite, Msgspec). Centralizing here prevents drift — any future change
to pluralization or reserved-word handling lands in one place.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqler.exceptions import NotBoundError


def _pluralize(word: str) -> str:
    """Pluralize a word using common English rules.

    Handles:
    - Words ending in consonant + y → ies (category → categories)
    - Words ending in s, x, z, ch, sh → es (box → boxes)
    - Regular words → s (user → users)

    For irregular plurals (person, child, etc.), use ``__tablename__``.
    """
    w = word.lower()
    if w.endswith("s"):
        return w  # Already plural or ends in s
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"  # category → categories
    if w.endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"  # box → boxes, class → classes
    return w + "s"  # user → users


@lru_cache(maxsize=1024)
def _default_table_name(name: str) -> str:
    """Generate default table name from class name.

    Examples:
        User → users
        Category → categories
        Address → addresses
        As → as_tbl (SQL reserved word)
    """
    lower = name.lower()
    # Check reserved words BEFORE pluralization (for words like "by", "as")
    # Also include words that pluralize to reserved words (a → as)
    reserved = {"a", "as", "by", "and", "or", "not", "null", "index", "table"}
    if lower in reserved:
        return lower + "_tbl"
    return _pluralize(name)


def _model_table_name(cls: type) -> str:
    """Return the table a model class uses: the one rule every reader calls.

    The table bound with ``set_db()`` (``_table``), else ``__tablename__``, else the
    default plural of the class name. The name is checked before it is returned, so
    it can go into SQL as an identifier.

    Raises:
        InvalidTableNameError: The name is not ``[a-zA-Z_][a-zA-Z0-9_]*``.
    """
    from sqler.utils import validate_table_name

    return validate_table_name(_unchecked_table_name(cls))


def _unchecked_table_name(cls: type) -> str:
    """The rule of ``_model_table_name`` without the name check, for comparing only.

    Never put the result into SQL: call ``_model_table_name`` for that.
    """
    return (
        getattr(cls, "_table", None)
        or getattr(cls, "__tablename__", None)
        or _default_table_name(cls.__name__)
    )


def _register_defined_class(cls: type) -> None:
    """Register a newly defined model class under its table, so a stored reference finds it.

    Called when a model class is defined. Classes of sqler itself are skipped, and so
    is a subclass that inherits its table from a base (it would hide the base's entry).
    A class whose ``__tablename__`` is not a valid name is left out here; it is refused
    when it is first used, as before.

    A defined class registers only when no class holds its table or the holder is not
    bound: it never displaces a class bound with ``set_db()`` (``set_db()`` registers
    directly and always wins).

    Every such class, registered or not, is one of the classes the registry gives for the
    table its rows go to (``registry.holders()``): the delete policy reads its fields there.
    """
    from sqler import registry
    from sqler.exceptions import InvalidTableNameError

    if cls.__module__.split(".", 1)[0] == "sqler":
        return
    registry.define(cls)
    if "__tablename__" not in cls.__dict__ and any(
        getattr(base, "__tablename__", None) or getattr(base, "_table", None)
        for base in cls.__mro__[1:]
    ):
        return
    try:
        table = _model_table_name(cls)
    except InvalidTableNameError:
        return
    holder = registry.resolve(table)
    if holder is not None and getattr(holder, "_db", None) is not None:
        # a class bound with set_db() holds its table; a class that is only defined
        # (a later import, a class inside a function) never takes its place
        return
    registry.register(table, cls)


# How every class call names its database without ``set_db()``: the deprecation warning of
# ``set_db()`` and ``bind()`` on every base and the error of an unbound class all say it.
DATABASE_RULE = (
    "Use .using(db) for queries and db= for every other call "
    "(.save(db=db), .delete(db=db), Model.from_id(id, db=db), Model.add_index(field, db=db))."
)


def _require_binding(cls) -> tuple[Any, str]:
    """The database and table bound to ``cls`` with ``set_db()``.

    Raises:
        NotBoundError: ``set_db()`` was not called.
    """
    if cls._db is None or cls._table is None:
        raise NotBoundError(
            f"Model {cls.__name__} is not bound. {DATABASE_RULE}",
            details={"model": cls.__name__},
        )
    return cls._db, cls._table


def _check_database(cls, db) -> None:
    """Refuse ``db`` unless it is a database of the kind ``cls`` takes.

    A sync model takes a ``SQLerDB``, an async one an ``AsyncSQLerDB`` (a subclass of either
    is taken as its parent is): each base names its kind in ``_takes_db``. Every way a
    database reaches a model makes this check before anything else runs: the resolvers
    below, ``using()``, and ``set_db()`` through :func:`_check_given`. A database the class
    is bound to, or an instance's home, was checked when it came in, so a call that uses
    one checks nothing.

    Raises:
        TypeError: ``db`` is not a ``cls._takes_db``.
    """
    takes = cls._takes_db
    if isinstance(db, takes):
        return
    article = "an" if takes.__name__[0] in "AEIOU" else "a"
    raise TypeError(
        f"{cls.__name__} takes {article} {takes.__name__} as its database, not an object of type "
        f"{type(db).__name__}: a sync model reads and writes through a SQLerDB, an async "
        "model through an AsyncSQLerDB."
    )


def _check_given(cls, db) -> None:
    """:func:`_check_database` of ``db`` unless it is ``None``: for a call that returns before
    it resolves a database (``from_ids([])``, ``save_many([])``), and for ``set_db()``, where
    ``None`` leaves the class unbound."""
    if db is not None:
        _check_database(cls, db)


def _resolve_binding(cls, db=None) -> tuple[Any, str]:
    """``(db, the class's table)`` when ``db`` is given, else the class binding.

    The one resolver of every class call that takes ``db=`` and is not a query:
    ``from_id``, ``from_ids``, ``save_many``, ``add_index``, ``ensure_index``,
    ``validate_references`` and the batched CSV import (the other imports pass ``db`` to
    ``from_id`` and ``save``). A query goes through ``using(db)``; the search calls give
    ``db`` to their ``FTSIndex``. A given ``db`` is the only database the call touches, with
    the class's table (``_model_table_name``); a database bound with ``set_db()`` is not
    used. A given ``db`` of the wrong kind raises ``TypeError`` (:func:`_check_database`)."""
    if db is not None:
        _check_database(cls, db)
        return db, _model_table_name(cls)
    return _require_binding(cls)


class _Home:
    """The database and table of the row an instance stands for: where it was read from or
    last written to.

    It is kept in the instance's ``__dict__`` as ``_home``, beside the fields, so no dump,
    ``==``, ``hash`` or ``repr`` sees it. A copy, shallow or deep, holds this same object
    (the same database object, never a copy of one); a pickle drops it.
    """

    __slots__ = ("db", "table")

    def __init__(self, db, table: str) -> None:
        self.db = db
        self.table = table

    def __copy__(self) -> "_Home":
        return self

    def __deepcopy__(self, memo) -> "_Home":
        return self

    def __reduce__(self):
        return (type(None), ())


def _home_of(inst: object) -> "_Home | None":
    return vars(inst).get("_home")


def _set_home(inst: object, home: "_Home | None") -> None:
    """Make ``home`` the home of ``inst`` (frozen or not, on every base)."""
    vars(inst)["_home"] = home


def _ref_table(inst: object) -> str:
    """The table a stored reference to ``inst`` names: where the row lives."""
    home = _home_of(inst)
    return home.table if home is not None else _model_table_name(type(inst))


def _instance_binding(inst: object, db=None) -> tuple[Any, str]:
    """Return ``(db, table)`` for writing or re-reading the row of ``inst``.

    The database is ``db`` when given (``TypeError`` when it is of the wrong kind), else the
    instance's home, else the class binding (``NotBoundError`` when there is none). The
    table is the home's: a row is never written to another table than the one it was read
    from.
    """
    home = _home_of(inst)
    if home is None:
        return _resolve_binding(type(inst), db)
    if db is None:
        db = home.db if home.db is not None else _require_binding(type(inst))[0]
    else:
        _check_database(type(inst), db)
    return db, home.table


def _read_document(db, table: str, rid, versioned: bool = False):
    """``db.find_document`` for a read through a model: a missing table is a missing row, ``None``.
    ``versioned``: the row with its ``_version`` (``find_document_with_version``)."""
    if versioned:
        return db.find_document_with_version(table, rid)
    return db.find_document(table, rid)


def _read_documents(db, table: str, ids: list) -> list:
    """``db.find_documents`` for a read through a model: a missing table gives ``[]``."""
    return db.find_documents(table, ids)


async def _aread_document(db, table: str, rid, versioned: bool = False):
    """Async twin of :func:`_read_document`."""
    if versioned:
        return await db.find_document_with_version(table, rid)
    return await db.find_document(table, rid)


async def _aread_documents(db, table: str, ids: list) -> list:
    """Async twin of :func:`_read_documents`."""
    return await db.find_documents(table, ids)
