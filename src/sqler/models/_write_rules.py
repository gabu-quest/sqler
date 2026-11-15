"""What happens besides the row itself when a row of a model class is written.

Every way of writing an instance (``save()``, ``save_partial()``, ``save_many()``,
``delete()``, ``delete_with_policy()``, on every base, sync and async) makes one call into
this module, and only this module and the mixins know which rules exist. A mixin with such
a rule derives from :class:`WriteRules` and adds its part cooperatively:

* ``_stamp()``: what to set on an instance just before it is written (the timestamp and
  audit-field mixins).
* ``_rows_written()`` / ``_arows_written()``, for a class whose ``_row_rules`` is true:
  told, inside the write's transaction, of each row written as
  ``(action, _id, before, after)``: ``action`` is ``"create"``, ``"update"`` or
  ``"delete"``, ``before`` is the stored document before (``None`` for a create) and
  ``after`` the one written (``None`` for a delete).

A class with no such mixin pays one ``isinstance`` test per write and no statement.
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar, Optional

from sqler.models import _dirty

Row = tuple[str, Any, Optional[dict], Optional[dict]]

_DELETE_POLICIES = ("restrict", "set_null", "cascade")


class WriteRules:
    """Base of every mixin with a rule about the rows its class writes; each part defaults
    to nothing, and a mixin that overrides one calls on with ``super()``."""

    _row_rules: ClassVar[bool] = False

    def _stamp(self) -> None:
        """Set what this class keeps on an instance just before it is written."""

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
        raise ValueError("on_delete must be 'restrict', 'set_null', or 'cascade'")


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


def delete(inst, db, table: str, on_delete: str, remove: Callable[[], None]) -> None:
    """``delete_with_policy()``: ``remove()`` deletes the row and does what the policy does to
    the rows that refer to it. Whatever writes more than one statement (a policy that
    touches referrers, row rules) is one unit, and a failure leaves the rows and the
    instance's identity as they were."""
    deletable(inst, on_delete)
    rules = isinstance(inst, WriteRules) and inst._row_rules
    if on_delete == "restrict" and not rules:
        return remove()
    row_id = inst._id
    before = inst._dump_with_relations() if rules else None
    kept = _remember_identity(inst)
    try:
        with db.transaction():
            remove()
            if rules:
                inst._rows_written(db, table, [("delete", row_id, before, None)])
    except BaseException:
        _restore_identity(inst, kept)
        raise


async def adelete(inst, db, table: str, on_delete: str, remove) -> None:
    """Async twin of :func:`delete`; ``remove()`` returns an awaitable."""
    deletable(inst, on_delete)
    rules = isinstance(inst, WriteRules) and inst._row_rules
    if on_delete == "restrict" and not rules:
        return await remove()
    row_id = inst._id
    before = await inst._adump_with_relations() if rules else None
    kept = _remember_identity(inst)
    try:
        async with db.transaction():
            await remove()
            if rules:
                await inst._arows_written(db, table, [("delete", row_id, before, None)])
    except BaseException:
        _restore_identity(inst, kept)
        raise
