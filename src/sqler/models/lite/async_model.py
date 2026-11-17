"""AsyncSQLerLiteModel - Async dataclass-based ORM model.

This module provides AsyncSQLerLiteModel, a Pydantic-free async alternative
that uses standard library dataclasses. It maintains full API compatibility
with AsyncSQLerModel while being compatible with Pyodide.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import fields
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Optional,
    Type,
    TypeVar,
)

from sqler import registry
from sqler._stored import stored
from sqler.db._statements import IndexFields
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import UniqueConstraintError
from sqler.models import _dirty, _refs, _table_names, _write_rules
from sqler.models._table_names import (
    _aread_document,
    _aread_documents,
    _default_table_name,
    _instance_binding,
    _model_table_name,
)
from sqler.models.lite.base import SQLerLiteModelBase
from sqler.models.lite.model import SQLerLiteModel
from sqler.utils import validate_field_name, validate_table_name

if TYPE_CHECKING:
    from sqler.models.async_queryset import AsyncSQLerQuerySet

# The deprecation warning is attributed to the first caller outside the sqler package, so a
# subclass's ``super().set_db()`` does not hide it behind sqler's own file.
_SQLER_PREFIXES = (os.path.dirname(os.path.abspath(registry.__file__)) + os.sep,)

TAModel = TypeVar("TAModel", bound="AsyncSQLerLiteModel")


class AsyncSQLerLiteModel(SQLerLiteModelBase):
    """Async dataclass-based SQLer model with persistence helpers.

    Instances persist as JSON into SQLite.

    Usage::

        from dataclasses import dataclass
        from sqler import AsyncSQLerLiteModel, AsyncSQLerDB

        @dataclass
        class User(AsyncSQLerLiteModel):
            __tablename__ = "users"
            name: str
            email: str

        db = AsyncSQLerDB(":memory:")
        await db.connect()
        user = User(name="Alice", email="alice@example.com")
        await user.save(db=db)
        users = await User.using(db).filter(F("age") > 30).all()

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and ``db=`` for
        every other call (``await .save(db=db)``, ``Model.from_id(id, db=db)``).
    """

    # Class-level database binding (not dataclass fields)
    _db: ClassVar[Optional["AsyncSQLerDB"]] = None
    # the database this class takes; every way one comes in checks it (``_check_database``)
    _takes_db: ClassVar[type[AsyncSQLerDB]] = AsyncSQLerDB
    _table: ClassVar[Optional[str]] = None
    # whether ``from_ids`` reads each row with its ``_version`` column (the safe bases)
    _versioned_rows: ClassVar[bool] = False

    @classmethod
    def set_db(cls, db: "AsyncSQLerDB", table: Optional[str] = None) -> None:
        """Bind this model class to an async database and table.

        .. deprecated::
            Use :meth:`using` for queries and ``db=`` for every other call
            (``save(db=db)``, ``from_id(id, db=db)``, ``add_index(field, db=db)``).

        Args:
            db: AsyncSQLerDB instance to use for persistence.
            table: Optional table name. Defaults to lowercase plural.

        Raises:
            TypeError: ``db`` is neither ``None`` nor an ``AsyncSQLerDB``; the class is left as it was.
        """
        _table_names._check_given(cls, db)
        warnings.warn(
            f"{cls.__name__}.set_db() is deprecated. "
            f"{_table_names.DATABASE_RULE} "
            "set_db() will be removed in a future version.",
            DeprecationWarning,
            skip_file_prefixes=_SQLER_PREFIXES,
        )
        cls._db = db
        explicit = getattr(cls, "__tablename__", None)
        chosen = validate_table_name(
            table or explicit or _default_table_name(cls.__name__)
        )
        cls._table = chosen
        cls.__tablename__ = chosen  # type: ignore[attr-defined]
        registry.register(cls._table, cls)

    @classmethod
    def bind(cls, db: "AsyncSQLerDB", table: Optional[str] = None) -> None:
        """Alias for set_db() (deprecated)."""
        warnings.warn(
            f"{cls.__name__}.bind() is deprecated. "
            f"{_table_names.DATABASE_RULE} "
            "bind() will be removed in a future version.",
            DeprecationWarning,
            stacklevel=2,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(db, table)

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
        _table_names._check_database(cls, db)
        table = validate_table_name(table) if table is not None else _model_table_name(cls)
        return cls._queryset(db, table)

    @classmethod
    def _queryset(cls: Type[TAModel], db: "AsyncSQLerDB", table: str) -> "AsyncSQLerQuerySet[TAModel]":
        from sqler.models.async_queryset import AsyncSQLerQuerySet
        from sqler.query.async_query import AsyncSQLerQuery

        q = AsyncSQLerQuery(table=table, adapter=db.adapter)
        if cls._versioned_rows:
            q = q.with_version()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        return AsyncSQLerQuerySet[TAModel](cls, q, db)

    _require_binding = classmethod(_table_names._require_binding)
    _resolve_binding = classmethod(_table_names._resolve_binding)

    @classmethod
    def db(cls: Type[TAModel]) -> "AsyncSQLerDB":
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    @classmethod
    async def from_id(cls: Type[TAModel], id_: int, *, db=None) -> Optional[TAModel]:
        """Hydrate an instance by ``_id``.

        Args:
            id_: Row id to load.
            db: Optional database (overrides class-level binding).

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._resolve_binding(db)
        doc = await _aread_document(db, table, id_, cls._versioned_rows)
        if doc is None:
            return None
        return (await _refs.amodels(cls, [doc], db, table))[0]

    @classmethod
    async def from_ids(cls: Type[TAModel], ids: list[int], *, db=None) -> list[TAModel]:
        """Hydrate multiple instances by id list (batch operation).

        Args:
            ids: List of row ids to load.
            db: Optional database (overrides class-level binding).

        Returns:
            List of model instances. Missing ids are omitted.
        """
        if not ids:
            _table_names._check_given(cls, db)
            return []
        db, table = cls._resolve_binding(db)
        read = _refs.aread_versioned if cls._versioned_rows else _aread_documents
        return await _refs.amodels(cls, await read(db, table, ids), db, table)

    @classmethod
    async def count(cls: Type[TAModel]) -> int:
        """Return total count of rows in this model's table."""
        return await cls.query().count()

    @classmethod
    def query(cls: Type[TAModel]) -> "AsyncSQLerQuerySet[TAModel]":
        """Return an async queryset for chaining and execution."""
        db, table = cls._require_binding()
        return cls._queryset(db, table)

    @classmethod
    def filter(cls: Type[TAModel], expression: Any) -> "AsyncSQLerQuerySet[TAModel]":
        """Shorthand for ``cls.query().filter(expression)``."""
        return cls.query().filter(expression)

    @classmethod
    async def first_or_create(
        cls: Type[TAModel],
        lookup: dict,
        defaults: Optional[dict] = None,
        *,
        db=None,
    ) -> tuple[TAModel, bool]:
        """Find the first matching row or create a new one (async).

        Args:
            lookup: Fields to filter by (used for both lookup and creation).
            defaults: Additional fields merged into the new instance on creation.
            db: Optional database (overrides class-level binding).

        Returns:
            tuple[Model, bool]: (instance, created) where created is True if new.
        """
        from sqler.query.field import SQLerField as F

        for key in lookup:
            validate_field_name(key)
        if db is not None:
            qs = cls.using(db)
        else:
            qs = cls.query()
        for key, value in lookup.items():
            qs = qs.filter(F(key) == value)
        existing = await qs.first()
        if existing is not None:
            return existing, False
        merged = {**lookup, **(defaults or {})}
        inst = cls(**merged)
        try:
            await inst.save(db=db)
        except UniqueConstraintError as error:
            return _write_rules.found_after_race(await qs.first(), error)
        return inst, True

    @classmethod
    async def all(cls: Type[TAModel]) -> list[TAModel]:
        """Return all instances from the database."""
        return await cls.query().all()

    @classmethod
    async def first(cls: Type[TAModel]) -> Optional[TAModel]:
        """Return the first instance or None."""
        return await cls.query().first()

    @classmethod
    async def add_index(
        cls,
        field: IndexFields,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
        db=None,
    ) -> None:
        """Create an index on one field or several (as ``create_index`` takes them), in ``db`` when given, else the class's database."""
        db, table = cls._resolve_binding(db)
        await db.create_index(table, field, unique=unique, name=name, where=where)

    @classmethod
    async def ensure_index(
        cls,
        field: IndexFields,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
        db=None,
    ) -> None:
        """Ensure an index exists (idempotent), in ``db`` when given, else the class's
        database."""
        await cls.add_index(field, unique=unique, name=name, where=where, db=db)

    @classmethod
    async def save_many(cls: Type[TAModel], instances: list[TAModel], *, db=None) -> list[TAModel]:
        """Insert multiple new instances in a single batch operation.

        Uses multi-row INSERT for significantly better throughput than
        calling ``save()`` in a loop. Only supports new (unsaved) instances.
        Each instance meets the rules of its class as ``save()`` does: hooks,
        timestamps and audit rows (see the mixins); with an audit log the batch and
        its audit rows are one transaction.

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
    async def asave_many(cls: Type[TAModel], instances: list[TAModel], *, db=None) -> list[TAModel]:
        """Deprecated name of :meth:`save_many`: it warns and calls it."""
        warnings.warn(
            f"{cls.__name__}.asave_many() is deprecated. Use save_many(); "
            "asave_many() will be removed in a future version.",
            DeprecationWarning,
            skip_file_prefixes=_SQLER_PREFIXES,
        )
        return await cls.save_many(instances, db=db)

    @classmethod
    async def _insert_rows(cls, db, table: str, instances: list) -> None:
        """Write new instances in one batch; each takes its ``_id`` and snapshot."""
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            await _write_rules.ensure_columns(cls, db, table, promoted)
        payloads = [await inst._adump_with_relations() for inst in instances]
        insert, args = _write_rules.rows_insert(db, table, payloads, promoted)
        ids = await insert(*args)

        for inst, id_, payload in zip(instances, ids, payloads):
            object.__setattr__(inst, "_id", id_)
            object.__setattr__(inst, "_snapshot", _dirty.snapshot(payload))

    async def save(self: TAModel, *, db=None, _partial: bool = False) -> TAModel:
        """Insert or update this instance.

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
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            await _write_rules.ensure_columns(cls, db, table, promoted)
        payload = await self._adump_with_relations()
        written = await _write_rules.row_write(db, table, self, payload, _partial, promoted)
        new_id = self._id if _partial else written
        object.__setattr__(self, "_id", new_id)
        object.__setattr__(self, "_snapshot", _dirty.snapshot(payload))
        return self

    async def delete(self, *, db=None) -> None:
        """Delete this instance by ``_id``.

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

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = _instance_binding(self, db)
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = _write_rules.refresh_row(await _aread_document(db, table, self._id, cls._versioned_rows), self._id)
        own = {(table, self._id): self}
        return (await _refs.arun(_refs.load(cls, [doc], db, table, self._refreshed, own)))[0]

    def _refreshed(self: TAModel, doc: dict, row: dict) -> TAModel:
        """This instance holding the row ``doc`` (references in place) as ``row`` was read."""
        for name, value in type(self)._init_fields(doc).items():  # typed, as a load gives it
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_snapshot", _dirty.snapshot(row))
        return self

    # ----- async relationship helpers -----
    @classmethod
    async def _aresolve_relations(cls, data: dict, db=None) -> dict:
        """``data`` with the model of its row in place of each typed reference, loaded from
        ``db`` or the class's database (see ``_refs.resolve``)."""
        return await _refs.aresolve(data, db if db is not None else cls.db(), {}, cls)

    async def _adump_with_relations(self) -> dict:
        """Serialize model with relationship encoding (async)."""
        return self._dump_with_relations()

    def _dump_with_relations(self) -> dict:
        """The document ``save()`` writes: each field in stored form, a related model (async
        or sync) as the reference to its row. It awaits nothing."""
        return {
            f.name: stored(getattr(self, f.name), (AsyncSQLerLiteModel, SQLerLiteModel), f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
