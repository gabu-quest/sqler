from __future__ import annotations

import os
import warnings
from typing import Any, ClassVar, Optional, Type, TypeVar

from pydantic import BaseModel, PrivateAttr

from sqler import registry
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import NotBoundError, UniqueConstraintError
from sqler.models import _refs
from sqler.models._table_names import (
    _aread_document,
    _aread_documents,
    _default_table_name,
    _instance_binding,
    _model_table_name,
    _ref_table,
    _register_defined_class,
)
from sqler.models.async_queryset import AsyncSQLerQuerySet
from sqler.query import SQLerExpression
from sqler.query.async_query import AsyncSQLerQuery
from sqler.utils import validate_field_name, validate_table_name

# The deprecation warning is attributed to the first caller outside the sqler package, so a
# subclass's ``super().set_db()`` does not hide it behind sqler's own file.
_SQLER_PREFIXES = (os.path.dirname(os.path.abspath(registry.__file__)) + os.sep,)

TAModel = TypeVar("TAModel", bound="AsyncSQLerModel")


class AsyncSQLerModel(BaseModel):
    """Async Pydantic-based model with persistence helpers.

    Instances persist as JSON (excluding the private ``_id`` attribute) into
    a table with schema
    ``(_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)``.

    Usage::

        db = AsyncSQLerDB(":memory:")
        await db.connect()
        user = User(name="Alice")
        await user.save(db=db)
        users = await User.using(db).filter(F("age") > 30).all()

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and
        ``await .save(db=db)`` / ``await .delete(db=db)`` for writes.
    """

    # internal id stored outside the JSON blob
    _id: Optional[int] = PrivateAttr(default=None)
    # snapshot of last-loaded document (for merge strategies in perf mode)
    _snapshot: Optional[dict] = PrivateAttr(default=None)
    # table this row was read from, when ``using(db, table)`` named one other than the class's
    _source_table: Optional[str] = PrivateAttr(default=None)

    # class-bound db + table metadata
    _db: ClassVar[Optional[AsyncSQLerDB]] = None
    _table: ClassVar[Optional[str]] = None
    # whether ``from_ids`` reads each row with its ``_version`` column (the safe bases)
    _versioned_rows: ClassVar[bool] = False

    # ----- class config -----
    model_config = {
        "extra": "ignore",
        "frozen": False,
    }

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        _register_defined_class(cls)

    @classmethod
    def set_db(cls, db: AsyncSQLerDB, table: Optional[str] = None) -> None:
        warnings.warn(
            f"{cls.__name__}.set_db() is deprecated. "
            "Use .using(db) for queries and .save(db=db)/.delete(db=db) for writes. "
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
        cls.__tablename__ = chosen
        # Note: promoted table creation is deferred to first use since we can't await here.
        # The _ensure_table_with_promoted call happens in the async ensure_schema() or first save/query.
        registry.register(cls._table, cls)

    @classmethod
    def _require_binding(cls) -> tuple[AsyncSQLerDB, str]:
        if cls._db is None or cls._table is None:
            raise NotBoundError(
                f"Model {cls.__name__} is not bound. "
                "Use .using(db) for queries or .save(db=db) for writes.",
                details={"model": cls.__name__},
            )
        return cls._db, cls._table

    @classmethod
    def _resolve_binding(cls, db=None) -> tuple[AsyncSQLerDB, str]:
        """Return (db, table) using the provided db or the class-level binding."""
        if db is not None:
            return db, _model_table_name(cls)
        return cls._require_binding()

    @classmethod
    def db(cls: Type[TAModel]) -> AsyncSQLerDB:
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    @classmethod
    async def from_id(cls: Type[TAModel], id_: int) -> Optional[TAModel]:
        db, table = cls._require_binding()
        doc = await _aread_document(db, table, id_)
        if doc is None:
            return None
        return _refs.from_row(cls, await cls._aresolve_relations(doc, db=db))

    @classmethod
    async def from_ids(cls: Type[TAModel], ids: list[int], *, db=None) -> list[TAModel]:
        """Hydrate multiple instances by id list (batch operation).

        Fetches all documents in a single query and resolves all relations
        in batch (avoiding N+1 queries). This is much faster than looping
        over ``from_id()`` for multiple records.

        Args:
            ids: List of row ids to load.
            db: Optional database (overrides class-level binding).

        Returns:
            List of model instances (in same order as input ids).
            Missing ids are silently omitted from result.
        """
        if not ids:
            return []
        given = db
        db, table = cls._resolve_binding(db)
        read = _refs.aread_versioned if cls._versioned_rows else _aread_documents
        docs = await read(db, table, ids)
        if not docs:
            return []
        # Use queryset's batch resolution for efficient nested ref loading
        qs = cls.using(given) if given is not None else cls.query()
        return [_refs.from_row(cls, doc) for doc in await qs._abatch_resolve(docs)]

    @classmethod
    async def count(cls: Type[TAModel]) -> int:
        """Return total count of rows in this model's table.

        Shorthand for ``await cls.query().count()``.
        """
        return await cls.query().count()

    @classmethod
    def query(cls: Type[TAModel]) -> AsyncSQLerQuerySet[TAModel]:
        """Return a queryset on the bound table."""
        db, table = cls._require_binding()
        return cls._queryset(db, table)

    @classmethod
    def using(
        cls: Type[TAModel], db: AsyncSQLerDB, table: Optional[str] = None
    ) -> AsyncSQLerQuerySet[TAModel]:
        """Return a queryset bound to a specific DB (no class-level mutation).

        Args:
            db: Database to read.
            table: Optional table to read instead of the class's own. The class
                binding is not changed. A row read from it remembers the table:
                ``save()``, ``delete()`` and ``refresh()`` use that table.

        A read creates no table: on a table nobody created, ``all()`` is empty,
        ``first()`` is ``None`` and ``count()`` is 0, and the table is still missing.
        """
        table = validate_table_name(table) if table is not None else _model_table_name(cls)
        return cls._queryset(db, table)

    @classmethod
    def _queryset(cls: Type[TAModel], db: AsyncSQLerDB, table: str) -> AsyncSQLerQuerySet[TAModel]:
        q = AsyncSQLerQuery(table=table, adapter=db.adapter)
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        return AsyncSQLerQuerySet[TAModel](cls, q, db)

    @classmethod
    def filter(cls: Type[TAModel], expression: SQLerExpression) -> AsyncSQLerQuerySet[TAModel]:
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
        except UniqueConstraintError:
            # another caller inserted the same lookup between the read and the insert
            existing = await qs.first()
            if existing is None:
                raise  # refused by a unique index on a field outside the lookup
            return existing, False
        return inst, True  # type: ignore[return-value]

    # ergonomic relation field builder
    @classmethod
    def ref(cls, name: str):
        """Return a model-aware field builder for a related field name.

        Usage: User.ref("address").field("city") == "Kyoto"
        """
        from .model_field import SQLerModelField

        class _RefBuilder:
            def __init__(self, model_cls, base: str):
                self.model_cls = model_cls
                self.path = [base]

            def field(self, *parts: str) -> SQLerModelField:
                return SQLerModelField(self.model_cls, self.path + list(parts))

            def any(self) -> "_RefAnyBuilder":
                return _RefAnyBuilder(self.model_cls, self.path)

        class _RefAnyBuilder(_RefBuilder):
            def field(self, *parts: str) -> SQLerModelField:
                return SQLerModelField(self.model_cls, self.path + list(parts), array_any=True)

        return _RefBuilder(cls, name)

    @classmethod
    async def add_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Create an index on a JSON field via the model class.

        Args:
            field: Dotted JSON path or literal column.
            unique: Enforce uniqueness.
            name: Optional index name.
            where: Optional partial-index WHERE clause.
        """
        db, table = cls._require_binding()
        await db.create_index(table, field, unique=unique, name=name, where=where)

    @classmethod
    async def ensure_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Ensure an index on a JSON path or literal column exists (idempotent)."""
        await cls.add_index(field, unique=unique, name=name, where=where)

    @classmethod
    async def _ensure_schema(cls) -> None:
        """Ensure the table schema is set up (including promoted columns)."""
        db, table = cls._require_binding()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            checks = getattr(cls, "__checks__", None)
            await db._ensure_table_with_promoted(table, promoted, checks)
        else:
            await db._ensure_table(table)

    @classmethod
    async def asave_many(cls: Type[TAModel], instances: list[TAModel], *, db=None) -> list[TAModel]:
        """Insert multiple new instances in a single batch operation.

        Uses multi-row INSERT for significantly better throughput than
        calling ``save()`` in a loop. Only supports new (unsaved) instances.

        Args:
            instances: List of model instances with ``_id is None``.
            db: Optional database (overrides class-level binding).

        Returns:
            list: The same instances with ``_id`` populated.

        Raises:
            ValueError: If any instance already has an ``_id``.
        """
        if not instances:
            return instances
        for inst in instances:
            if inst._id is not None:
                raise ValueError("asave_many() only accepts new instances (_id must be None)")

        db, table = cls._resolve_binding(db)
        promoted = getattr(cls, "__promoted__", None)

        if promoted:
            checks = getattr(cls, "__checks__", None)
            await db._ensure_promoted(table, promoted, checks)

        payloads = [await inst._adump_with_relations() for inst in instances]

        if promoted:
            promoted_fields = list(promoted.keys())
            ids = await db.insert_many_promoted(table, payloads, promoted_fields)
        else:
            ids = await db.insert_many(table, payloads)

        for inst, id_ in zip(instances, ids):
            inst._id = id_
        return instances

    async def save(self: TAModel, *, db=None) -> TAModel:
        cls = self.__class__
        db, table = _instance_binding(self, db)
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            checks = getattr(cls, "__checks__", None)
            await db._ensure_promoted(table, promoted, checks)
        else:
            await db._ensure_table(table)
        payload = await self._adump_with_relations()
        if promoted:
            promoted_fields = list(promoted.keys())
            new_id = await db.upsert_document_promoted(table, self._id, payload, promoted_fields)
        else:
            new_id = await db.upsert_document(table, self._id, payload)
        self._id = new_id
        return self

    async def delete(self, *, db=None) -> None:
        """Delete this instance by ``_id`` and unset it.

        Args:
            db: Optional database to delete from (overrides class-level binding).
        """
        await self.delete_with_policy(db=db)

    async def delete_with_policy(self, *, db=None, on_delete: str = "restrict") -> None:
        """Delete this instance with a specified integrity policy.

        Args:
            db: Optional database to delete from (overrides class-level binding).
            on_delete: One of "restrict", "set_null", or "cascade".
                - "restrict": Deletes the row without checking for references.
                - "set_null": Nullifies all references to this row before deleting.
                - "cascade": Recursively deletes all rows that reference this row.
        """
        from .async_integrity import (
            async_cascade_delete,
            async_find_referrers,
            async_set_null_referrers,
        )

        db, table = _instance_binding(self, db)
        if self._id is None:
            raise ValueError("Cannot delete unsaved model (missing _id)")
        if on_delete not in {"restrict", "set_null", "cascade"}:
            raise ValueError("on_delete must be 'restrict', 'set_null', or 'cascade'")

        if on_delete == "set_null":
            referrers = await async_find_referrers(db, table, self._id)
            await async_set_null_referrers(db, table, self._id, referrers)
        elif on_delete == "cascade":
            referrers = await async_find_referrers(db, table, self._id)
            await async_cascade_delete(db, referrers, set())

        await db.delete_document(table, self._id)
        self._id = None

    async def refresh(self: TAModel) -> TAModel:
        cls = self.__class__
        db, table = _instance_binding(self)
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = await _aread_document(db, table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        doc = await cls._aresolve_relations(doc, db=db)
        fresh = cls.model_validate(doc)
        for fname in self.__class__.model_fields:
            if fname == "_id":
                continue
            setattr(self, fname, getattr(fresh, fname))
        self._id = doc.get("_id")
        return self

    # ----- relationship helpers (async) -----
    @classmethod
    async def _aresolve_relations(cls, data: dict, db=None, _seen: frozenset = frozenset()) -> dict:
        """Replace the references in ``data`` by the rows they point to (see ``_refs.aresolve``)."""
        return await _refs.aresolve(data, db, _seen)

    @classmethod
    async def _aload_refs(cls, ids: list, seen: frozenset) -> dict:
        """Load the rows ``ids`` of this class's table with one statement.

        Returns ``{id: instance}``; a row that is gone is missing, one that cannot be
        hydrated maps to ``_refs.KEEP``.
        """
        db, table = cls._require_binding()
        out: dict = {}
        for doc in await _aread_documents(db, table, ids):
            rid = doc["_id"]
            try:
                doc = await cls._aresolve_relations(doc, db=db, _seen=seen | {(table, rid)})
                inst = _refs.from_row(cls, doc)
            except Exception:
                out[rid] = _refs.KEEP
                continue
            out[rid] = inst
        return out

    async def _adump_with_relations(self) -> dict:
        async def aencode(value: Any):
            from datetime import datetime

            from sqler.models.async_model import AsyncSQLerModel
            from sqler.models.model import SQLerModel

            if isinstance(value, AsyncSQLerModel):
                if value._id is None:
                    raise ValueError("Related async model must be saved before saving parent")
                return {"_table": _ref_table(value), "_id": value._id}
            if isinstance(value, SQLerModel):
                if value._id is None:
                    raise ValueError("Related model must be saved before saving parent")
                return {"_table": _ref_table(value), "_id": value._id}
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, list):
                return [await aencode(v) for v in value]
            if isinstance(value, dict):
                out = {}
                for k, v in value.items():
                    out[k] = await aencode(v)
                return out
            if isinstance(value, BaseModel):
                return value.model_dump()
            return value

        payload: dict = {}
        for name in self.__class__.model_fields:
            if name == "_id":
                continue
            payload[name] = await aencode(getattr(self, name))
        return payload
