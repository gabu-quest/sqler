"""Shared helpers for deriving default SQLite table names from model class names.

These were previously duplicated byte-for-byte across the three model backends
(Pydantic, Lite, Msgspec). Centralizing here prevents drift — any future change
to pluralization or reserved-word handling lands in one place.
"""

from __future__ import annotations

from functools import lru_cache

from sqler.db._missing_table import is_missing_table


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
    """
    from sqler import registry
    from sqler.exceptions import InvalidTableNameError

    if cls.__module__.split(".", 1)[0] == "sqler":
        return
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


def _source_table_of(inst: object) -> "str | None":
    """The table a row was read from when it is not its class's table, else None."""
    return getattr(inst, "_source_table", None)


def _ref_table(inst: object) -> str:
    """The table a stored reference to ``inst`` names: where the row lives."""
    return _source_table_of(inst) or _model_table_name(type(inst))


def _instance_binding(inst: object, db=None):
    """Return ``(db, table)`` for writing or re-reading one row.

    The database is ``db`` or the class binding, as ``_resolve_binding`` gives it. The
    table is the one the row was read from when it came through ``using(db, table)``,
    so a row is never written to a different table than it was read from.
    """
    db, table = type(inst)._resolve_binding(db)  # type: ignore[attr-defined]
    return db, (_source_table_of(inst) or table)


# the one check, in the database layer; the old name stays for its callers here
_is_missing_table = is_missing_table


def _read_document(db, table: str, rid):
    """``db.find_document`` for a read through a model: a missing table is a missing row, ``None``."""
    return db.find_document(table, rid)


def _read_documents(db, table: str, ids: list) -> list:
    """``db.find_documents`` for a read through a model: a missing table gives ``[]``."""
    return db.find_documents(table, ids)


async def _aread_document(db, table: str, rid):
    """Async twin of :func:`_read_document`."""
    return await db.find_document(table, rid)


async def _aread_documents(db, table: str, ids: list) -> list:
    """Async twin of :func:`_read_documents`."""
    return await db.find_documents(table, ids)


def _set_row_id(inst: object, rid) -> None:
    try:
        inst._id = rid  # type: ignore[attr-defined]
    except AttributeError:  # a frozen dataclass
        object.__setattr__(inst, "_id", rid)


def _load_unbound(mdl: type, db, table: str, rid):
    """Hydrate a referenced row of a never-bound class from ``db`` (the parent's database).

    A row that is gone, or whose table is missing, is returned as the reference it
    was; any other error reaches the caller.
    """
    doc = _read_document(db, table, rid)
    if doc is None:
        return {"_table": table, "_id": rid}
    resolver = getattr(mdl, "_resolve_relations", None)
    if resolver is not None:
        doc = resolver(doc, db=db)
    inst = mdl.model_validate(doc)  # type: ignore[attr-defined]
    _set_row_id(inst, doc.get("_id"))
    return inst


async def _aload_unbound(mdl: type, db, table: str, rid):
    """Async twin of :func:`_load_unbound`."""
    doc = await _aread_document(db, table, rid)
    if doc is None:
        return {"_table": table, "_id": rid}
    resolver = getattr(mdl, "_aresolve_relations", None)
    if resolver is not None:
        doc = await resolver(doc, db=db)
    inst = mdl.model_validate(doc)  # type: ignore[attr-defined]
    _set_row_id(inst, doc.get("_id"))
    return inst
