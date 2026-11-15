"""What happens besides the row itself when a row of a model class is written.

Every way of writing an instance (``save()``, ``save_partial()``, ``save_many()``,
``delete()``, ``delete_with_policy()``, on every base, sync and async) makes one call into
this module, and only this module and the mixins know which rules exist. A mixin with such
a rule derives from :class:`WriteRules` and adds its part cooperatively:

* ``_stamp()``: what to set on an instance just before it is written (the timestamp and
  audit-field mixins); ``_update_stamps()``: what a set-based update of the class's rows
  sets on each, in the same statement.
* ``_rows_written()`` / ``_arows_written()``, for a class whose ``_row_rules`` is true:
  told, inside the write's transaction, of each row written as
  ``(action, _id, before, after)``: ``action`` is ``"create"``, ``"update"`` or
  ``"delete"``, ``before`` is the stored document before (``None`` for a create) and
  ``after`` the one written (``None`` for a delete).

The set-based ways (a queryset's ``update()``, ``update_one()``, ``update_n()`` and
``delete_all()``) and the rows a delete's policy nulls or deletes come here too: they stamp and
are told of their rows as an instance write is, and they call no hook (no instance exists).

A class with no such mixin pays one ``isinstance`` test per write and no statement.
"""

from __future__ import annotations

import json
from typing import Any, Callable, ClassVar, Optional

from sqler import registry
from sqler._stored import column_arg, stored
from sqler.exceptions import ReferentialIntegrityError
from sqler.models import _dirty, async_integrity, integrity

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
    def _rows_written(cls, db, table: str, rows: list[Row]) -> None:
        """Called inside the write's transaction with the rows the write changed."""

    @classmethod
    async def _arows_written(cls, db, table: str, rows: list[Row]) -> None:
        """Async twin of ``_rows_written``."""


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
    if on_delete not in _DELETE_POLICIES:
        *others, last = _DELETE_POLICIES
        raise ValueError(f"on_delete must be {', '.join(map(repr, others))}, or {last!r}")


# ----- one unit: all of a write or none of it; what a failed write puts back ------------
#
# A write of more than one statement runs in ``db.transaction()``: a transaction of its own,
# or a savepoint inside the caller's. So a caller who catches the error inside their own
# transaction and commits keeps none of the failed write, and the instance's identity is
# put back to match.


_NO_VALUE = object()


def _remember_identity(obj) -> dict:
    """What a failed write must put back: ``_id``, ``_snapshot`` and, where the model has
    one, ``_version``."""
    kept = {"_id": obj._id, "_snapshot": obj._snapshot}
    version = getattr(obj, "_version", _NO_VALUE)
    if version is not _NO_VALUE:
        kept["_version"] = version
    return kept


def _restore_identity(obj, kept: dict) -> None:
    for name, value in kept.items():
        setattr(obj, name, value)


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
    """``save()``: stamp, then ``write()`` the row; with row rules, in one transaction with
    them, and a failure puts the instance's identity back."""
    if not isinstance(inst, WriteRules):
        return write()
    inst._stamp()
    if not inst._row_rules:
        return write()
    kept = _remember_identity(inst)
    try:
        with db.transaction():
            row_id = inst._id
            before = None if row_id is None else db.find_document(table, row_id)
            result = write()
            action = "create" if row_id is None else "update"
            after = _written(db, table, inst, partial)
            inst._rows_written(db, table, [(action, inst._id, before, after)])
            return result
    except BaseException:
        _restore_identity(inst, kept)
        raise


async def asave(inst, db, table: str, partial: bool, write):
    """Async twin of :func:`save`; ``write()`` returns an awaitable."""
    if not isinstance(inst, WriteRules):
        return await write()
    inst._stamp()
    if not inst._row_rules:
        return await write()
    kept = _remember_identity(inst)
    try:
        async with db.transaction():
            row_id = inst._id
            before = None if row_id is None else await db.find_document(table, row_id)
            result = await write()
            action = "create" if row_id is None else "update"
            after = await _awritten(db, table, inst, partial)
            await inst._arows_written(db, table, [(action, inst._id, before, after)])
            return result
    except BaseException:
        _restore_identity(inst, kept)
        raise


def save_many(cls, instances: list, db, insert: Callable[[Any, str, list], None]) -> list:
    """``save_many()``: stamp each instance, then ``insert(db, table, instances)`` them in
    one batch; with row rules, in one transaction with them, and a failure puts every
    instance's identity back."""
    if not instances:
        return instances
    new_only(instances)
    db, table = cls._resolve_binding(db)
    if not issubclass(cls, WriteRules):
        insert(db, table, instances)
        return instances
    for inst in instances:
        inst._stamp()
    if not cls._row_rules:
        insert(db, table, instances)
        return instances
    kept = [_remember_identity(inst) for inst in instances]
    try:
        with db.transaction():
            insert(db, table, instances)
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
        return instances
    new_only(instances)
    db, table = cls._resolve_binding(db)
    if not issubclass(cls, WriteRules):
        await insert(db, table, instances)
        return instances
    for inst in instances:
        inst._stamp()
    if not cls._row_rules:
        await insert(db, table, instances)
        return instances
    kept = [_remember_identity(inst) for inst in instances]
    try:
        async with db.transaction():
            await insert(db, table, instances)
            await cls._arows_written(
                db, table, [("create", inst._id, None, inst._snapshot) for inst in instances]
            )
    except BaseException:
        for inst, k in zip(instances, kept):
            _restore_identity(inst, k)
        raise
    return instances


def delete(inst, db, table: str, on_delete: str) -> None:
    """``delete_with_policy()``: delete the row and do what the policy does about the rows
    that refer to it, with the class's row rules. The check for referrers and the delete are
    one unit (``db.transaction()``: no other connection writes a referrer in between), and a
    failure leaves every row as it was."""
    deletable(inst, on_delete)
    row_id = int(inst._id)
    rules = isinstance(inst, WriteRules) and inst._row_rules
    with db.transaction():
        before = db.find_document(table, row_id) if rules else None
        referrers = integrity.find_referrers(db, table, row_id)
        change = _referrer_change(on_delete, table, row_id, referrers)
        if change is not None:
            change[0](db, table, row_id, referrers)
        db.delete_document(table, row_id)
        if before is not None:
            inst._rows_written(db, table, [("delete", row_id, before, None)])


async def adelete(inst, db, table: str, on_delete: str) -> None:
    """Async twin of :func:`delete`."""
    deletable(inst, on_delete)
    row_id = int(inst._id)
    rules = isinstance(inst, WriteRules) and inst._row_rules
    async with db.transaction():
        before = await db.find_document(table, row_id) if rules else None
        referrers = await async_integrity.async_find_referrers(db, table, row_id)
        change = _referrer_change(on_delete, table, row_id, referrers)
        if change is not None:
            await change[1](db, table, row_id, referrers)
        await db.delete_document(table, row_id)
        if before is not None:
            await inst._arows_written(db, table, [("delete", row_id, before, None)])


# ----- what a delete's policy does about the rows that refer to its row ------------------


def _cascade(db, table: str, row_id, referrers) -> None:
    integrity.cascade_delete(db, referrers, {(table, row_id)})


async def _acascade(db, table: str, row_id, referrers) -> None:
    await async_integrity.async_cascade_delete(db, referrers, {(table, row_id)})


# The policies that change the referring rows, each as its sync and its async way.
_CHANGES = {
    "set_null": (integrity.set_null_referrers, async_integrity.async_set_null_referrers),
    "cascade": (_cascade, _acascade),
}
_DELETE_POLICIES = ("restrict", *_CHANGES)


def _referrer_change(on_delete: str, table: str, row_id, referrers: list):
    """What ``on_delete`` does about ``referrers``, the rows that refer to ``table``:``row_id``:
    ``restrict`` refuses the delete; the others are their ``(sync, async)`` change of the
    rows. ``None`` where no row refers to it."""
    if not referrers:
        return None
    if on_delete == "restrict":
        raise ReferentialIntegrityError(
            f"Cannot delete {table}:{row_id}; referenced by {len(referrers)} row(s)"
        )
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
        raise RuntimeError(f"{cls.__name__} has row rules: a set-based write needs the queryset's database")
    return db


def set_write(cls, db, table: str, write: Callable[[], list]) -> list:
    """A set-based write on a class with row rules: ``write()`` writes the rows and returns
    each as ``(before, after)`` (``after`` ``None`` for a delete); it and the rules are one
    unit."""
    with _unit_db(cls, db).transaction():
        pairs = write()
        cls._rows_written(db, table, _set_rows(pairs))
    return pairs


async def aset_write(cls, db, table: str, write) -> list:
    """Async twin of :func:`set_write`; ``write()`` returns an awaitable."""
    async with _unit_db(cls, db).transaction():
        pairs = await write()
        await cls._arows_written(db, table, _set_rows(pairs))
    return pairs


# ----- the rows a delete's policy changes: each meets the rules of its own class ---------


def _class_of(table: str):
    cls = registry.resolve(table)
    return cls if isinstance(cls, type) and issubclass(cls, WriteRules) else None


def _versioned(table: str) -> bool:
    """Whether the class registered for ``table`` keeps a version on its rows."""
    return bool(getattr(registry.resolve(table), "_versioned_rows", False))


def _nulled_update(cls, table: str, row_id, doc: dict, versioned: bool) -> tuple[str, list]:
    """The UPDATE that writes ``doc``, a referring row with its reference nulled, with the
    stamps of its class (a promoted one in its column). A ``versioned`` row's version moves,
    as with every other write of it: a holder who loaded the row before cannot save the
    reference back."""
    stamps = cls._update_stamps() if cls is not None else {}
    promoted = getattr(cls, "__promoted__", None) or {}
    columns, params = [", _version = _version + 1"] if versioned else [], []
    for name, value in stamps.items():
        if name in promoted:
            columns.append(f", {name} = ?")
            params.append(column_arg(value))
        else:
            doc[name] = stored(value)
    return (
        f"UPDATE {table} SET data = json(?){''.join(columns)} WHERE _id = ?;",
        [json.dumps(doc), *params, row_id],
    )


def null_referrer(db, table: str, row_id, doc: dict) -> None:
    """``set_null``: write ``doc`` into the referring row ``row_id``."""
    cls = _class_of(table)
    before = db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    versioned = _versioned(table)
    if versioned and not db._is_versioned(table):
        db._ensure_versioned_table(table)
    db.adapter.execute(*_nulled_update(cls, table, row_id, doc, versioned))
    db.adapter.auto_commit()
    if before is not None:
        after = _dirty.snapshot(db.find_document(table, row_id))
        cls._rows_written(db, table, [("update", row_id, before, after)])


async def anull_referrer(db, table: str, row_id, doc: dict) -> None:
    """Async twin of :func:`null_referrer`."""
    cls = _class_of(table)
    before = await db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    versioned = _versioned(table)
    if versioned and not db._is_versioned(table):
        await db._ensure_versioned_table(table)
    cursor = await db.adapter.execute(*_nulled_update(cls, table, row_id, doc, versioned))
    await cursor.close()
    await db.adapter.auto_commit()
    if before is not None:
        after = _dirty.snapshot(await db.find_document(table, row_id))
        await cls._arows_written(db, table, [("update", row_id, before, after)])


def delete_referrer(db, table: str, row_id) -> None:
    """``cascade``: delete the referring row ``row_id``."""
    cls = _class_of(table)
    before = db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    db.delete_document(table, row_id)
    if before is not None:
        cls._rows_written(db, table, [("delete", row_id, before, None)])


async def adelete_referrer(db, table: str, row_id) -> None:
    """Async twin of :func:`delete_referrer`."""
    cls = _class_of(table)
    before = await db.find_document(table, row_id) if cls is not None and cls._row_rules else None
    await db.delete_document(table, row_id)
    if before is not None:
        await cls._arows_written(db, table, [("delete", row_id, before, None)])
