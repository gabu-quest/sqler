"""What happens besides the row itself when a row of a model class is written.

Every way of writing an instance (``save()``, ``save_partial()``, ``save_many()``,
``delete()``, ``delete_with_policy()``, on every base, sync and async) makes one call into
this module, and only this module and the mixins know which rules exist. A mixin with such
a rule derives from :class:`WriteRules` and adds its part cooperatively:

* ``_stamp()``: what to set on an instance just before it is written (the timestamp and
  audit-field mixins); ``_update_stamps()``: what a set-based update of the class's rows
  sets on each, in the same statement.
* ``_table_written()``: told, right after a write, that a table of the class was written
  (the cache mixin); it needs no row and runs no statement.
* ``_rows_written()`` / ``_arows_written()``, for a class whose ``_row_rules`` is true:
  told, inside the write's transaction, of each row written as
  ``(action, _id, before, after)``: ``action`` is ``"create"``, ``"update"`` or
  ``"delete"``, ``before`` is the stored document before (``None`` for a create) and
  ``after`` the one written (``None`` for a delete).
* ``_row_kept()`` / ``_arow_kept()``, for a class whose ``_keeps_row()`` is true (the search
  mixin with an index kept by hand): told, inside the write's transaction, of the one row a
  ``save()`` wrote or a ``delete()`` deleted, by id; it needs no document before or after.

The set-based ways (a queryset's ``update()``, ``update_one()``, ``update_n()`` and
``delete_all()``) and the rows a delete's policy nulls or deletes come here too: they stamp and
are told of their rows as an instance write is, and they call no hook (no instance exists).

A class with no such mixin pays one ``isinstance`` test per write and no statement.
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar, Optional

from sqler import registry
from sqler._stored import stored
from sqler.exceptions import ReferentialIntegrityError
from sqler.models import _dirty, _refs, async_integrity, integrity
from sqler.models._table_names import _check_given, _Home, _home_of, _model_table_name, _set_home

Row = tuple[str, Any, Optional[dict], Optional[dict]]


class WriteRules:
    """Base of every mixin with a rule about the rows its class writes; each part defaults
    to nothing, and a mixin that overrides one calls on with ``super()``."""

    _row_rules: ClassVar[bool] = False

    def _stamp(self) -> None:
        """Set what this class keeps on an instance just before it is written."""

    @classmethod
    def _update_stamps(cls) -> dict[str, Any]:
        """The fields a set-based update of this class's rows sets on each, with their values."""
        return {}

    @classmethod
    def _table_written(cls, db, table: str) -> None:
        """Called right after a write of ``table`` through ``db`` (inside the write's
        transaction where it has one); also after a write that raised, which may have
        written some of its rows."""

    @classmethod
    def _rows_written(cls, db, table: str, rows: list[Row]) -> None:
        """Called inside the write's transaction with the rows the write changed."""

    @classmethod
    async def _arows_written(cls, db, table: str, rows: list[Row]) -> None:
        """Async twin of ``_rows_written``."""

    @classmethod
    def _keeps_row(cls) -> bool:
        """Whether ``save()`` and ``delete()`` of an instance tell ``_row_kept``, which makes
        the write and what it writes one unit."""
        return False

    @classmethod
    def _row_kept(cls, db, table: str, _id: Any, deleted: bool) -> None:
        """Called inside the write's transaction with the row ``_id`` of ``table`` that a
        ``save()`` wrote (``deleted`` false) or a ``delete()`` deleted."""

    @classmethod
    async def _arow_kept(cls, db, table: str, _id: Any, deleted: bool) -> None:
        """Async twin of ``_row_kept``."""


# ----- the checks every way in makes before anything runs ------------------------------


def new_only(instances) -> None:
    """``save_many()`` takes new instances only: the ``ValueError``, before any hook runs."""
    for inst in instances:
        if inst._id is not None:
            raise ValueError("save_many() only accepts new instances (_id must be None)")


def deletable(inst, on_delete: str) -> None:
    """A delete needs a saved instance and a known policy: the ``ValueError``, before any
    hook runs."""
    if inst._id is None:
        raise ValueError("Cannot delete unsaved model (missing _id)")
    known_policy(on_delete)


def known_policy(on_delete: str) -> None:
    """The ``ValueError`` of a delete policy that is not one of ``_DELETE_POLICIES``."""
    if on_delete not in _DELETE_POLICIES:
        *others, last = _DELETE_POLICIES
        raise ValueError(f"on_delete must be {', '.join(map(repr, others))}, or {last!r}")


# ----- one unit: all of a write or none of it; what a failed write puts back ------------
#
# A write of more than one statement runs in ``db.transaction()``: a transaction of its own,
# or a savepoint inside the caller's. So a caller who catches the error inside their own
# transaction and commits keeps none of the failed write, and the instance's identity is
# put back to match.


def _remember_identity(obj) -> dict:
    """What a failed write must put back: ``_id``, ``_snapshot``, the home and, where the
    model has one, ``_version``."""
    kept = {"_id": obj._id, "_snapshot": obj._snapshot}
    if type(obj)._versioned_rows:  # every base says whether its instances have ``_version``
        kept["_version"] = obj._version
    kept["_home"] = _home_of(obj)
    return kept


def _restore_identity(obj, kept: dict) -> None:
    for name, value in kept.items():
        if name == "_home":
            _set_home(obj, value)
        else:
            setattr(obj, name, value)


def _written_to(instances, db, table: str) -> None:
    """Each of ``instances``, just written to ``table`` of ``db``, has its home there."""
    home = _Home(db, table)
    for inst in instances:
        _set_home(inst, home)


def _written(db, table: str, inst, partial: bool) -> dict:
    """The stored document a save wrote: a partial save leaves the row's other fields as
    they were, so it is the row."""
    if partial:
        return _dirty.snapshot(db.find_document(table, inst._id))
    return inst._snapshot


async def _awritten(db, table: str, inst, partial: bool) -> dict:
    if partial:
        return _dirty.snapshot(await db.find_document(table, inst._id))
    return inst._snapshot


# ----- the ways of writing ---------------------------------------------------------------


def save(inst, db, table: str, partial: bool, write: Callable[[], Any]):
    """``save()``: stamp, then ``write()`` the row; with row rules or a row kept
    (``_keeps_row``), in one transaction with them, and a failure puts the instance's identity
    back."""
    rules = isinstance(inst, WriteRules)
    if rules:
        inst._stamp()
    if not (rules and (inst._row_rules or inst._keeps_row())):
        try:
            result = write()
        finally:
            if rules:
                inst._table_written(db, table)
        _written_to([inst], db, table)
        return result
    told = inst._row_rules
    kept = _remember_identity(inst)
    try:
        with db.transaction():
            row_id = kept["_id"]
            before = None if row_id is None or not told else db.find_document(table, row_id)
            result = write()
            inst._table_written(db, table)
            _written_to([inst], db, table)
            written_id = inst._id
            if told:
                action = "create" if row_id is None else "update"
                after = _written(db, table, inst, partial)
                inst._rows_written(db, table, [(action, written_id, before, after)])
            inst._row_kept(db, table, written_id, False)
            return result
    except BaseException:
        _restore_identity(inst, kept)
        raise


async def asave(inst, db, table: str, partial: bool, write):
    """Async twin of :func:`save`; ``write()`` returns an awaitable."""
    rules = isinstance(inst, WriteRules)
    if rules:
        inst._stamp()
    if not (rules and (inst._row_rules or inst._keeps_row())):
        try:
            result = await write()
        finally:
            if rules:
                inst._table_written(db, table)
        _written_to([inst], db, table)
        return result
    told = inst._row_rules
    kept = _remember_identity(inst)
    try:
        async with db.transaction():
            row_id = kept["_id"]
            before = None if row_id is None or not told else await db.find_document(table, row_id)
            result = await write()
            inst._table_written(db, table)
            _written_to([inst], db, table)
            written_id = inst._id
            if told:
                action = "create" if row_id is None else "update"
                after = await _awritten(db, table, inst, partial)
                await inst._arows_written(db, table, [(action, written_id, before, after)])
            await inst._arow_kept(db, table, written_id, False)
            return result
    except BaseException:
        _restore_identity(inst, kept)
        raise


def save_many(cls, instances: list, db, insert: Callable[[Any, str, list], None]) -> list:
    """``save_many()``: stamp each instance, then ``insert(db, table, instances)`` them in
    one batch; with row rules, in one transaction with them, and a failure puts every
    instance's identity back."""
    if not instances:
        _check_given(cls, db)
        return instances
    new_only(instances)
    db, table = cls._resolve_binding(db)
    rules = issubclass(cls, WriteRules)
    if rules:
        for inst in instances:
            inst._stamp()
    if not (rules and cls._row_rules):
        try:
            insert(db, table, instances)
        finally:
            if rules:
                cls._table_written(db, table)
        _written_to(instances, db, table)
        return instances
    kept = [_remember_identity(inst) for inst in instances]
    try:
        with db.transaction():
            insert(db, table, instances)
            cls._table_written(db, table)
            _written_to(instances, db, table)
            cls._rows_written(
                db, table, [("create", inst._id, None, inst._snapshot) for inst in instances]
            )
    except BaseException:
        for inst, k in zip(instances, kept):
            _restore_identity(inst, k)
        raise
    return instances


async def asave_many(cls, instances: list, db, insert) -> list:
    """Async twin of :func:`save_many`; ``insert(db, table, instances)`` returns an awaitable."""
    if not instances:
        _check_given(cls, db)
        return instances
    new_only(instances)
    db, table = cls._resolve_binding(db)
    rules = issubclass(cls, WriteRules)
    if rules:
        for inst in instances:
            inst._stamp()
    if not (rules and cls._row_rules):
        try:
            await insert(db, table, instances)
        finally:
            if rules:
                cls._table_written(db, table)
        _written_to(instances, db, table)
        return instances
    kept = [_remember_identity(inst) for inst in instances]
    try:
        async with db.transaction():
            await insert(db, table, instances)
            cls._table_written(db, table)
            _written_to(instances, db, table)
            await cls._arows_written(
                db, table, [("create", inst._id, None, inst._snapshot) for inst in instances]
            )
    except BaseException:
        for inst, k in zip(instances, kept):
            _restore_identity(inst, k)
        raise
    return instances


# ----- the database calls of a row write: decided here, for every base -------------------
#
# A save is the hottest call of the package: the body of each base tests ``if promoted:``
# itself (a class with no promoted columns makes no call for them), and :func:`row_write`
# makes the database call and returns what it returned, which a sync base uses and an async
# one awaits. The ones that return a call instead (``fn(*args)``, awaited on an async
# database) serve a batch or the steps of a safe save.

#: One database call: ``fn(*args)`` (awaited on an async database).
Call = tuple[Callable[..., Any], tuple[Any, ...]]


def ensure_columns(cls, db, table: str, promoted: Any) -> Any:
    """Ensure the promoted columns ``promoted`` (``cls.__promoted__``, not empty) in
    ``table`` before a write; what ``db._ensure_promoted`` returns (an awaitable on an async
    database)."""
    return db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)


def row_write(db, table: str, inst: Any, payload: dict, partial: bool, promoted: Any) -> Any:
    """Write the row ``payload`` of ``inst``, whose class's ``__promoted__`` is ``promoted``,
    as ``inst._id``, and return what the database call returned (an awaitable on an async
    database): with ``partial``, only the fields changed since ``inst._snapshot``
    (``db._update_fields``, whose result is not the id: the row keeps ``_id``); else the
    whole row, with its promoted columns where the class has any
    (``db.upsert_document_promoted``) or without (``db.upsert_document``)."""
    if partial:
        changed = _dirty.changed_fields(inst._snapshot or {}, payload)
        return db._update_fields(table, inst._id, payload, changed, promoted or ())
    if promoted:
        return db.upsert_document_promoted(table, inst._id, payload, list(promoted.keys()))
    return db.upsert_document(table, inst._id, payload)


def versioned_write(db, table: str, _id: Any, payload: dict, base: Any, partial: bool, promoted: Any, version: Any) -> Call:
    """The call that writes the row ``payload`` of a safe model as ``_id`` only at row
    version ``version``, as :func:`row_write` decides it (``base`` the snapshot a partial
    write compares with); the whole-row ones return ``(id, new version)``."""
    if partial:
        changed = _dirty.changed_fields(base or {}, payload)
        return db._update_fields, (table, _id, payload, changed, promoted or (), version)
    if promoted:
        return db.upsert_with_version_promoted, (table, _id, payload, version, list(promoted.keys()))
    return db.upsert_with_version, (table, _id, payload, version)
def rows_insert(db, table: str, payloads: list, promoted: Any, versioned: bool = False) -> Call:
    """The call that inserts the new rows ``payloads`` of a class whose ``__promoted__`` is
    ``promoted`` in one batch, with the promoted columns where it has any; it returns their
    ids, or with ``versioned`` (a safe model) ``(id, version)`` pairs."""
    if promoted:
        fn = db.insert_many_with_version_promoted if versioned else db.insert_many_promoted
        return fn, (table, payloads, list(promoted.keys()))
    return (db.insert_many_with_version if versioned else db.insert_many), (table, payloads)


def found_after_race(existing: Any, error: BaseException) -> tuple[Any, bool]:
    """``first_or_create()`` whose insert raised ``error`` (a unique index refused it), with
    ``existing`` the second read of the lookup: another caller inserted the same lookup
    between the read and the insert, and its row is returned as found; ``None`` means a
    unique index on a field outside the lookup refused it, and ``error`` is raised."""
    if existing is None:
        raise error
    return existing, False


def refresh_row(doc: Optional[dict], _id: Any) -> dict:
    """The row ``doc`` that ``refresh()`` read for ``_id``.

    Raises:
        LookupError: The row no longer exists (``doc`` is ``None``).
    """
    if doc is None:
        raise LookupError(f"Row {_id} not found for refresh")
    return doc


def delete(inst, db, table: str, on_delete: str) -> None:
    """``delete_with_policy()``: delete the row and do what the policy does about the rows
    that refer to it (:func:`deleted`), with the class's row rules and its row kept, as one unit
    (``db.transaction()``); a failure leaves every row as it was. Then the instance is
    unsaved (see :func:`_deleted`)."""
    deletable(inst, on_delete)
    row_id = int(inst._id)
    told = isinstance(inst, WriteRules)
    rules = told and inst._row_rules
    with db.transaction():
        before = db.find_document(table, row_id) if rules else None
        db.delete_document(table, row_id)
        if told:
            inst._table_written(db, table)
        deleted(db, table, [row_id], on_delete)
        if before is not None:
            inst._rows_written(db, table, [("delete", row_id, before, None)])
        if told:
            inst._row_kept(db, table, row_id, True)
    _deleted(inst)


async def adelete(inst, db, table: str, on_delete: str) -> None:
    """Async twin of :func:`delete`."""
    deletable(inst, on_delete)
    row_id = int(inst._id)
    told = isinstance(inst, WriteRules)
    rules = told and inst._row_rules
    async with db.transaction():
        before = await db.find_document(table, row_id) if rules else None
        await db.delete_document(table, row_id)
        if told:
            inst._table_written(db, table)
        await adeleted(db, table, [row_id], on_delete)
        if before is not None:
            await inst._arows_written(db, table, [("delete", row_id, before, None)])
        if told:
            await inst._arow_kept(db, table, row_id, True)
    _deleted(inst)


def _deleted(inst) -> None:
    """A deleted instance has no row: ``_id`` is unset. Its home stays the database and table
    it was deleted from, so a later ``save()`` writes it back there as a new row."""
    _refs._set(inst, "_id", None)


# ----- what a delete's policy does about the rows that refer to its rows ------------------
#
# A delete deletes its rows first and then looks for the rows that refer to them, in one
# unit: a row inside the set being deleted is gone, so it is no referrer (a row that refers
# to itself, a parent deleted with its children); a refusal ends the unit, which puts the
# rows back.


def deleted(db, table: str, ids: list, on_delete: str) -> None:
    """Do what ``on_delete`` does about the rows that refer to ``ids`` of ``table``, rows
    just deleted inside the caller's unit: ``restrict`` raises ``ReferentialIntegrityError``
    while one refers to them. A table no class can refer to is not looked at."""
    if not ids or not integrity.referable(table):
        return
    referrers = integrity.find_referrers(db, table, ids)
    change = _referrer_change(on_delete, table, ids, referrers)
    if change is not None:
        change[0](db, table, ids, referrers)


async def adeleted(db, table: str, ids: list, on_delete: str) -> None:
    """Async twin of :func:`deleted`."""
    if not ids or not integrity.referable(table):
        return
    referrers = await async_integrity.async_find_referrers(db, table, ids)
    change = _referrer_change(on_delete, table, ids, referrers)
    if change is not None:
        await change[1](db, table, ids, referrers)


def _cascade(db, table: str, ids, referrers) -> None:
    integrity.cascade_delete(db, referrers, {(table, i) for i in ids})


async def _acascade(db, table: str, ids, referrers) -> None:
    await async_integrity.async_cascade_delete(db, referrers, {(table, i) for i in ids})


# The policies that change the referring rows, each as its sync and its async way.
_CHANGES = {
    "set_null": (integrity.set_null_referrers, async_integrity.async_set_null_referrers),
    "cascade": (_cascade, _acascade),
}
_DELETE_POLICIES = ("restrict", *_CHANGES)


def _referrer_change(on_delete: str, table: str, ids: list, referrers: list):
    """What ``on_delete`` does about ``referrers``, the rows that refer to ``ids`` of
    ``table``: ``restrict`` refuses the delete; the others are their ``(sync, async)``
    change of the rows. ``None`` where no row refers to them."""
    if not referrers:
        return None
    if on_delete == "restrict":
        rows = f"{table}:{ids[0]}" if len(ids) == 1 else f"{len(ids)} rows of {table}"
        raise ReferentialIntegrityError(f"Cannot delete {rows}; referenced by {len(referrers)} row(s)")
    return _CHANGES[on_delete]


# ----- the set-based ways: a queryset's update, update_one, update_n and delete_all ------


def stamped(cls, fields: dict) -> dict:
    """``fields`` and the class's update stamps; a field the caller names is the caller's
    (no fields: none, which the update refuses)."""
    if not fields or not issubclass(cls, WriteRules):
        return fields
    stamps = cls._update_stamps()
    return {**fields, **{k: v for k, v in stamps.items() if k not in fields}}


def row_rules(cls) -> bool:
    return issubclass(cls, WriteRules) and cls._row_rules


def _set_rows(pairs) -> list[Row]:
    return [
        ("delete", before["_id"], before, None)
        if after is None
        else ("update", before["_id"], before, _dirty.snapshot(after))
        for before, after in pairs
    ]


def _unit_db(cls, db):
    if db is None:
        raise RuntimeError(
            f"{cls.__name__} has row rules or rows that may be referred to: a set-based write "
            "needs the queryset's database"
        )
    return db


def set_delete(cls, table: str, on_delete: str) -> bool:
    """Whether a ``delete_all()`` of ``cls``'s rows in ``table`` is a unit (row rules, or a
    class that can refer to the table) rather than its one statement; an unknown policy is
    refused first."""
    known_policy(on_delete)
    return row_rules(cls) or integrity.referable(table)


def _ids(pairs) -> list:
    return [before["_id"] for before, _ in pairs]


def told(cls, db, table: str, result):
    """``result`` of a set-based write of ``table`` that needed no row of it (a class without
    row rules), once the class has been told that its table was written."""
    if issubclass(cls, WriteRules):
        cls._table_written(db, table)
    return result


def refuse_self_copy(cls, source: str) -> None:
    """Refuse, before any statement, a ``copy_to()`` of rows of ``cls`` from ``source`` when a
    field of ``cls`` can refer to rows of ``source``: a class admitted at any position of the
    field (:func:`sqler.models._refs.typed_fields`: the field, an ``Optional``, each entry of
    a list, each value of a dict) is stored in ``source``, or the class that owns ``source``
    is one of them or a subclass. A copied row keeps its references, so its copies would
    still refer to the rows of ``source``, and a delete of those rows would count the copies
    as referrers.

    Raises:
        ValueError: Such a field, the first one found.
    """
    for field, at in _refs.typed_fields(cls).items():
        admits = _admitted(at)
        if admits and (
            any(_model_table_name(c) == source for c in admits) or _refs._admits(source, admits)
        ):
            raise ValueError(
                f"copy_to: {cls.__name__}.{field} can refer to rows of {source}: its copies would "
                f"still refer to the rows of {source}, and a delete of those rows would count the "
                "copies as referrers"
            )


def _admitted(at: Any) -> tuple:
    """Every class a :class:`sqler.models._refs.Position` admits, at it or anywhere in it."""
    found = tuple(at.admits)
    for inner in (at.item, at.value):
        if inner is not None:
            found += _admitted(inner)
    return found


def copied(cls, db, table: str, count: int) -> int:
    """``count``, the rows of ``cls`` a queryset's ``copy_to()`` copied into ``table``, once
    the class and the class registered for ``table`` have been told that it was written (a
    cache drops its entries). It writes no audit row and calls no hook: the copy is one
    statement inside the database, and no row passes through Python. Rows of ``cls`` are
    now kept in ``table`` (``registry.note``)."""
    told(cls, db, table, None)
    other = _class_of(table)
    if other is not None and other is not cls:
        other._table_written(db, table)
    registry.note(table, cls)
    return count


def set_write(cls, db, table: str, write: Callable[[], list], on_delete: Optional[str] = None) -> list:
    """A set-based write: ``write()`` writes the rows and returns each as
    ``(before, after)`` (``after`` ``None`` for a delete); it, what ``on_delete`` does about
    the rows that refer to the deleted ones (:func:`deleted`) and the class's row rules are
    one unit."""
    with _unit_db(cls, db).transaction():
        pairs = write()
        if issubclass(cls, WriteRules):
            cls._table_written(db, table)
        if on_delete is not None:
            deleted(db, table, _ids(pairs), on_delete)
        if row_rules(cls):
            cls._rows_written(db, table, _set_rows(pairs))
    return pairs


async def aset_write(cls, db, table: str, write, on_delete: Optional[str] = None) -> list:
    """Async twin of :func:`set_write`; ``write()`` returns an awaitable."""
    async with _unit_db(cls, db).transaction():
        pairs = await write()
        if issubclass(cls, WriteRules):
            cls._table_written(db, table)
        if on_delete is not None:
            await adeleted(db, table, _ids(pairs), on_delete)
        if row_rules(cls):
            await cls._arows_written(db, table, _set_rows(pairs))
    return pairs


# ----- the rows a delete's policy changes: each meets the rules of its own class ---------


def _class_of(table: str):
    cls = registry.resolve(table)
    return cls if isinstance(cls, type) and issubclass(cls, WriteRules) else None


def _nulled(cls, doc: dict) -> dict:
    """``doc``, a referring row's document with its reference nulled, with the update stamps
    of its class; the database's by-id writer puts each key that names a column of the table
    into its column."""
    stamps = cls._update_stamps() if cls is not None else {}
    return {**doc, **{name: stored(value) for name, value in stamps.items()}}


def null_referrer(db, table: str, row_id, doc: dict) -> None:
    """``set_null``: write ``doc`` into the referring row ``row_id`` with the by-id writer a
    save uses. Where the table has ``_version`` the row's version moves by one, as with every
    other write of it: a holder who loaded the row before cannot save the reference back, one
    who loads it after can save it."""
    cls = _class_of(table)
    before = db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    db._upsert(table, row_id, _nulled(cls, doc))
    if cls is not None:
        cls._table_written(db, table)
    if before is not None:
        after = _dirty.snapshot(db.find_document(table, row_id))
        cls._rows_written(db, table, [("update", row_id, before, after)])


async def anull_referrer(db, table: str, row_id, doc: dict) -> None:
    """Async twin of :func:`null_referrer`."""
    cls = _class_of(table)
    before = await db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    await db._upsert(table, row_id, _nulled(cls, doc))
    if cls is not None:
        cls._table_written(db, table)
    if before is not None:
        after = _dirty.snapshot(await db.find_document(table, row_id))
        await cls._arows_written(db, table, [("update", row_id, before, after)])


def delete_referrer(db, table: str, row_id) -> None:
    """``cascade``: delete the referring row ``row_id``."""
    cls = _class_of(table)
    before = db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    db.delete_document(table, row_id)
    if cls is not None:
        cls._table_written(db, table)
    if before is not None:
        cls._rows_written(db, table, [("delete", row_id, before, None)])


async def adelete_referrer(db, table: str, row_id) -> None:
    """Async twin of :func:`delete_referrer`."""
    cls = _class_of(table)
    before = await db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    await db.delete_document(table, row_id)
    if cls is not None:
        cls._table_written(db, table)
    if before is not None:
        await cls._arows_written(db, table, [("delete", row_id, before, None)])
