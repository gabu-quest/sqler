"""AsyncSQLerMsgspecModel — the async twin of SQLerMsgspecModel.

Every public call of ``SQLerMsgspecModel`` is here under the same name with the same
arguments and result, awaited where it touches the database, on an ``AsyncSQLerDB``. The
checks and conversions both make are in ``SQLerMsgspecModelBase``; this class holds only
its order of calls.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Type,
    TypeVar,
)

from sqler import registry
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import UniqueConstraintError
from sqler.models import _dirty, _refs, _table_names, _write_rules
from sqler.models._table_names import _aread_document, _aread_documents, _instance_binding
from sqler.models.msgspec.base import SQLerMsgspecModelBase
from sqler.models.msgspec.model import SQLerMsgspecModel

if TYPE_CHECKING:
    from sqler.models.async_queryset import AsyncSQLerQuerySet

TAModel = TypeVar("TAModel", bound="AsyncSQLerMsgspecModel")


class AsyncSQLerMsgspecModel(SQLerMsgspecModelBase):
    """Async msgspec Struct-based SQLer model with persistence helpers.

    Instances persist as JSON into SQLite.

    Usage::

        from sqler import AsyncSQLerDB, AsyncSQLerMsgspecModel

        class User(AsyncSQLerMsgspecModel):
            __tablename__ = "users"
            name: str
            email: str

        db = AsyncSQLerDB.in_memory()
        await db.connect()
        user = User(name="Alice", email="alice@example.com")
        await user.save(db=db)
        users = await User.using(db).filter(F("age") > 30).all()

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and ``db=`` for
        every other call (``await .save(db=db)``, ``await Model.from_id(id, db=db)``).
    """

    # Class-level database binding — no type annotation, so msgspec makes no field of it
    _db = None
    _table = None
    # the database this class takes; every way one comes in checks it (``_check_database``)
    _takes_db = AsyncSQLerDB
    # no msgspec base keeps a version on its rows
    _versioned_rows = False

    @classmethod
    def set_db(cls, db: "AsyncSQLerDB", table: Optional[str] = None) -> None:
        """Bind this model class to an async database and table.

        .. deprecated::
            Use :meth:`using` for queries and ``db=`` for every other call
            (``save(db=db)``, ``from_id(id, db=db)``, ``add_index(field, db=db)``).

        Args:
            db: AsyncSQLerDB instance to use for persistence.
            table: Optional table name. Defaults to lowercase plural of the class name.

        Raises:
            TypeError: ``db`` is neither ``None`` nor an ``AsyncSQLerDB``; the class is left as it was.
        """
        registry.register(cls._bind_class(db, table), cls)

    @classmethod
    def using(
        cls: Type[TAModel], db: "AsyncSQLerDB", table: Optional[str] = None
    ) -> "AsyncSQLerQuerySet[TAModel]":
        """Return a queryset bound to a specific DB (no class-level mutation).

        Args:
            db: Database to read.
            table: Optional table to read instead of the class's own. The class
                binding is not changed. A row read through the queryset has ``db``
                and the table as its home: ``save()``, ``delete()`` and ``refresh()``
                use them.

        A read creates no table: on a table nobody created, ``all()`` is empty,
        ``first()`` is ``None`` and ``count()`` is 0, and the table is still missing.
        """
        return cls._queryset(db, cls._using_table(db, table))

    @classmethod
    def _queryset(cls: Type[TAModel], db: "AsyncSQLerDB", table: str) -> "AsyncSQLerQuerySet[TAModel]":
        """The queryset on ``table`` of ``db`` that ``query()`` and ``using()`` give."""
        from sqler.models.async_queryset import AsyncSQLerQuerySet
        from sqler.query.async_query import AsyncSQLerQuery

        return AsyncSQLerQuerySet[TAModel](cls, cls._shaped(AsyncSQLerQuery(table=table, adapter=db.adapter)), db)

    @classmethod
    def db(cls: Type[TAModel]) -> "AsyncSQLerDB":
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    _require_binding = classmethod(_table_names._require_binding)
    _resolve_binding = classmethod(_table_names._resolve_binding)

    @classmethod
    async def from_id(cls: Type[TAModel], id_: int, *, db=None) -> Optional[TAModel]:
        """Hydrate an instance by ``_id``.

        A typed reference is the model of its row, as on every base.

        Args:
            id_: Row id to load.
            db: Optional database (overrides class-level binding).

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._resolve_binding(db)
        doc = await _aread_document(db, table, id_)
        if doc is None:
            return None
        return (await _refs.amodels(cls, [doc], db, table))[0]

    @classmethod
    async def from_ids(cls: Type[TAModel], ids: list[int], *, db=None) -> list[TAModel]:
        """Hydrate multiple instances by id list (batch operation).

        A typed reference is the model of its row, as on every base.

        Args:
            ids: List of row ids to load.
            db: Optional database (overrides class-level binding).

        Returns:
            List of model instances (in same order as input ids).
            Missing ids are silently omitted from result.
        """
        if cls._no_ids(ids, db):
            return []
        db, table = cls._resolve_binding(db)
        return await _refs.amodels(cls, await _aread_documents(db, table, ids), db, table)

    @classmethod
    async def count(cls: Type[TAModel]) -> int:
        """Return total count of rows in this model's table."""
        return await cls.query().count()

    @classmethod
    def query(cls: Type[TAModel]) -> "AsyncSQLerQuerySet[TAModel]":
        """Return an async queryset for chaining and execution."""
        return cls._queryset(*cls._require_binding())

    @classmethod
    def filter(cls: Type[TAModel], expression: Any) -> "AsyncSQLerQuerySet[TAModel]":
        """Shorthand for ``cls.query().filter(expression)``."""
        return cls.query().filter(expression)

    @classmethod
    async def all(cls: Type[TAModel]) -> list[TAModel]:
        """Return all instances from the database."""
        return await cls.query().all()

    @classmethod
    async def first(cls: Type[TAModel]) -> Optional[TAModel]:
        """Return the first instance or None."""
        return await cls.query().first()

    @classmethod
    async def first_or_create(
        cls: Type[TAModel],
        lookup: dict,
        defaults: Optional[dict] = None,
        *,
        db=None,
    ) -> tuple[TAModel, bool]:
        """Find the first matching row or create a new one.

        Args:
            lookup: Fields to filter by (used for both lookup and creation).
            defaults: Additional fields merged into the new instance on creation.
            db: Optional database (overrides class-level binding).

        Returns:
            tuple[Model, bool]: (instance, created) where created is True if new.
        """
        qs = cls._lookup(lookup, db)
        existing = await qs.first()
        if existing is not None:
            return existing, False
        inst = cls(**{**lookup, **(defaults or {})})
        try:
            await inst.save(db=db)
        except UniqueConstraintError:
            # another caller inserted the same lookup between the read and the insert
            existing = await qs.first()
            if existing is None:
                raise  # refused by a unique index on a field outside the lookup
            return existing, False
        return inst, True

    @classmethod
    async def add_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
        db=None,
    ) -> None:
        """Create an index on a JSON field via the model class, in ``db`` when given, else the class's database."""
        db, table = cls._resolve_binding(db)
        await db.create_index(table, field, unique=unique, name=name, where=where)

    @classmethod
    async def ensure_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
        db=None,
    ) -> None:
        """Ensure an index on a JSON path or literal column exists (idempotent), in ``db``
        when given, else the class's database."""
        await cls.add_index(field, unique=unique, name=name, where=where, db=db)

    # ----- instance methods -----
    @classmethod
    async def save_many(cls: Type[TAModel], instances: list[TAModel], *, db=None) -> list[TAModel]:
        """Insert multiple new instances in a single batch operation.

        Uses multi-row INSERT for significantly better throughput than
        calling ``save()`` in a loop. Only supports new (unsaved) instances.
        Each instance meets the rules of its class as ``save()`` does: hooks,
        timestamps and audit rows (see the mixins); with an audit log the batch
        and its audit rows are one transaction.

        Args:
            instances: List of model instances with ``_id is None``.
            db: Optional database (overrides class-level binding).

        Returns:
            list: The same instances with ``_id`` populated.

        Raises:
            ValueError: If any instance already has an ``_id``.
        """
        return await _write_rules.asave_many(cls, instances, db, cls._insert_rows)

    @classmethod
    async def _insert_rows(cls, db, table: str, instances: list) -> None:
        """Write new instances in one batch; each takes its ``_id`` and snapshot."""
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            await db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
        payloads = [inst._dump_with_relations() for inst in instances]
        if promoted:
            ids = await db.insert_many_promoted(table, payloads, list(promoted.keys()))
        else:
            ids = await db.insert_many(table, payloads)
        cls._inserted(instances, ids, payloads)

    async def save(self: TAModel, *, db=None, _partial: bool = False) -> TAModel:
        """Insert or update this instance and update ``_id``.

        Args:
            db: Optional database to write to (overrides class-level binding).
            _partial: Write only the fields that differ from the row as this model last
                loaded or saved it, and leave the row's other fields as they are
                (``save_partial()``). The row must exist.

        Returns:
            self: The same instance (for chaining).
        """
        db, table = _instance_binding(self, db)
        return await _write_rules.asave(
            self, db, table, _partial, lambda: self._write(db, table, _partial)
        )

    async def _write(self: TAModel, db, table: str, _partial: bool) -> TAModel:
        """Write this instance's row (``save()`` without the rules of its class)."""
        cls = self.__class__
        payload = self._dump_with_relations()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            await db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
        if _partial:
            new_id = self._id
            changed = _dirty.changed_fields(self._snapshot or {}, payload)
            await db._update_fields(table, new_id, payload, changed, promoted or ())
        elif promoted:
            new_id = await db.upsert_document_promoted(table, self._id, payload, list(promoted.keys()))
        else:
            new_id = await db.upsert_document(table, self._id, payload)
        return self._wrote(new_id, payload)

    async def delete(self, *, db=None) -> None:
        """Delete this instance by ``_id`` and unset it.

        Args:
            db: Optional database to delete from (overrides class-level binding).
        """
        await self.delete_with_policy(db=db)

    async def delete_with_policy(self, *, db=None, on_delete: str = "restrict") -> None:
        """Delete this instance with a specified integrity policy.

        The delete and what the policy does to referring rows are one transaction
        (the caller's, if one is open), and the instance meets the rules of its class
        as ``delete()`` does: hooks and the audit row.

        Args:
            db: Optional database to delete from (overrides class-level binding).
            on_delete: One of "restrict", "set_null", or "cascade".
        """
        db, table = _instance_binding(self, db)
        await _write_rules.adelete(self, db, table, on_delete)

    async def refresh(self: TAModel, *, db=None) -> TAModel:
        """Reload this instance's fields from the database.

        Args:
            db: Optional database to read from (overrides the instance's home); the
                instance's home moves there.

        Raises:
            ValueError: If the instance has not been saved.
            LookupError: If the row no longer exists.

        Returns:
            self: The same instance (for chaining).
        """
        db, table = _instance_binding(self, db)
        doc = await _aread_document(db, table, self._saved_id())
        return (await _refs.arun(self._reload(doc, db, table)))[0]


# an async msgspec model, and a sync one, is stored as the reference to its row: a sync
# model's row is read through the database its class is bound to
AsyncSQLerMsgspecModel._reference_bases = (AsyncSQLerMsgspecModel, SQLerMsgspecModel)
