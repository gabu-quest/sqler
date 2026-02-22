"""AsyncSQLerLiteModel - Async dataclass-based ORM model.

This module provides AsyncSQLerLiteModel, a Pydantic-free async alternative
that uses standard library dataclasses. It maintains full API compatibility
with AsyncSQLerModel while being compatible with Pyodide.
"""

from __future__ import annotations

import inspect
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
from sqler.exceptions import NotBoundError
from sqler.models.lite.base import SQLerLiteModelBase
from sqler.models.lite.model import _default_table_name
from sqler.utils import validate_table_name

if TYPE_CHECKING:
    from sqler.db.async_db import AsyncSQLerDB
    from sqler.models.async_queryset import AsyncSQLerQuerySet

TAModel = TypeVar("TAModel", bound="AsyncSQLerLiteModel")


class AsyncSQLerLiteModel(SQLerLiteModelBase):
    """Async dataclass-based SQLer model with persistence helpers.

    Define subclasses decorated with @dataclass. Bind the class to an async
    database via :meth:`set_db`. Instances persist as JSON into SQLite.

    Usage:
        from dataclasses import dataclass
        from sqler import AsyncSQLerLiteModel, AsyncSQLerDB

        @dataclass
        class User(AsyncSQLerLiteModel):
            __tablename__ = "users"
            name: str
            email: str

        db = AsyncSQLerDB(":memory:")
        await db.connect()
        User.set_db(db)

        user = User(name="Alice", email="alice@example.com")
        await user.save()
    """

    # Class-level database binding (not dataclass fields)
    _db: ClassVar[Optional["AsyncSQLerDB"]] = None
    _table: ClassVar[Optional[str]] = None

    @classmethod
    def set_db(cls, db: "AsyncSQLerDB", table: Optional[str] = None) -> None:
        """Bind this model class to an async database and table.

        .. deprecated::
            Use :meth:`using` for queries and ``save(db=db)``/``delete(db=db)``
            for writes.

        Args:
            db: AsyncSQLerDB instance to use for persistence.
            table: Optional table name. Defaults to lowercase plural.
        """
        warnings.warn(
            f"{cls.__name__}.set_db() is deprecated. "
            "Use .using(db) for queries and .save(db=db)/.delete(db=db) for writes. "
            "set_db() will be removed in a future version.",
            DeprecationWarning,
            stacklevel=2,
        )
        cls._db = db
        explicit = getattr(cls, "__tablename__", None)
        chosen = table or explicit or _default_table_name(cls.__name__)
        cls._table = chosen
        cls.__tablename__ = chosen  # type: ignore[attr-defined]
        registry.register(cls._table, cls)

    @classmethod
    def bind(cls, db: "AsyncSQLerDB", table: Optional[str] = None) -> None:
        """Alias for set_db()."""
        cls.set_db(db, table)

    @classmethod
    def using(cls: Type[TAModel], db: "AsyncSQLerDB") -> "AsyncSQLerQuerySet[TAModel]":
        """Return a queryset bound to a specific DB (no class-level mutation)."""
        from sqler.models.async_queryset import AsyncSQLerQuerySet
        from sqler.query.async_query import AsyncSQLerQuery

        table = validate_table_name(
            getattr(cls, "__tablename__", None) or _default_table_name(cls.__name__)
        )
        q = AsyncSQLerQuery(table=table, adapter=db.adapter)
        return AsyncSQLerQuerySet[TAModel](cls, q)

    @classmethod
    def _require_binding(cls) -> tuple["AsyncSQLerDB", str]:
        """Return the bound DB and table or raise if unbound."""
        if cls._db is None or cls._table is None:
            raise NotBoundError(
                f"Model {cls.__name__} is not bound. Call set_db(db, table?) first.",
                details={"model": cls.__name__},
            )
        return cls._db, cls._table

    @classmethod
    def _resolve_binding(cls, db=None) -> tuple["AsyncSQLerDB", str]:
        """Return (db, table) using the provided db or the class-level binding."""
        if db is not None:
            table = cls._table or getattr(cls, "__tablename__", None) or _default_table_name(cls.__name__)
            return db, validate_table_name(table)
        return cls._require_binding()

    @classmethod
    def db(cls: Type[TAModel]) -> "AsyncSQLerDB":
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    @classmethod
    async def from_id(cls: Type[TAModel], id_: int) -> Optional[TAModel]:
        """Hydrate an instance by ``_id``.

        Args:
            id_: Row id to load.

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._require_binding()
        doc = await db.find_document(table, id_)
        if doc is None:
            return None
        doc = await cls._aresolve_relations(doc)
        inst = cls.model_validate(doc)
        object.__setattr__(inst, "_id", doc.get("_id"))
        object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
        return inst

    @classmethod
    async def from_ids(cls: Type[TAModel], ids: list[int]) -> list[TAModel]:
        """Hydrate multiple instances by id list (batch operation).

        Args:
            ids: List of row ids to load.

        Returns:
            List of model instances. Missing ids are omitted.
        """
        if not ids:
            return []
        db, table = cls._require_binding()
        docs = await db.find_documents(table, ids)
        if not docs:
            return []
        # Use queryset's batch resolution
        qs = cls.query()
        docs = await qs._abatch_resolve(docs)
        instances = []
        for doc in docs:
            inst = cls.model_validate(doc)
            object.__setattr__(inst, "_id", doc.get("_id"))
            object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
            instances.append(inst)
        return instances

    @classmethod
    async def count(cls: Type[TAModel]) -> int:
        """Return total count of rows in this model's table."""
        db, table = cls._require_binding()
        await db._ensure_table(table)
        return await cls.query().count()

    @classmethod
    def query(cls: Type[TAModel]) -> "AsyncSQLerQuerySet[TAModel]":
        """Return an async queryset for chaining and execution."""
        from sqler.models.async_queryset import AsyncSQLerQuerySet
        from sqler.query.async_query import AsyncSQLerQuery

        db, table = cls._require_binding()
        q = AsyncSQLerQuery(table=table, adapter=db.adapter)
        return AsyncSQLerQuerySet[TAModel](cls, q)

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
    async def add_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Create an index on a JSON field."""
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
        """Ensure an index exists (idempotent)."""
        await cls.add_index(field, unique=unique, name=name, where=where)

    async def save(self: TAModel, *, db=None) -> TAModel:
        """Insert or update this instance.

        Args:
            db: Optional database to write to (overrides class-level binding).

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._resolve_binding(db)
        payload = await self._adump_with_relations()
        new_id = await db.upsert_document(table, self._id, payload)
        object.__setattr__(self, "_id", new_id)
        object.__setattr__(self, "_snapshot", payload.copy())
        return self

    async def delete(self, *, db=None) -> None:
        """Delete this instance by ``_id``.

        Args:
            db: Optional database to delete from (overrides class-level binding).
        """
        await self.delete_with_policy(db=db)

    async def delete_with_policy(self, *, db=None, on_delete: str = "restrict") -> None:
        """Delete this instance with a specified integrity policy.

        Args:
            db: Optional database to delete from (overrides class-level binding).
            on_delete: One of "restrict", "set_null", or "cascade".
        """
        from sqler.models.async_integrity import (
            async_cascade_delete,
            async_find_referrers,
            async_set_null_referrers,
        )

        cls = self.__class__
        db, table = cls._resolve_binding(db)
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
        object.__setattr__(self, "_id", None)

    async def refresh(self: TAModel) -> TAModel:
        """Reload this instance's fields from the database.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = await db.find_document(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        doc = await cls._aresolve_relations(doc)
        # Update fields in-place
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            if f.name in doc:
                object.__setattr__(self, f.name, doc[f.name])
        object.__setattr__(self, "_snapshot", cls._make_snapshot(doc))
        return self

    @classmethod
    def _make_snapshot(cls, doc: dict) -> dict:
        """Create snapshot excluding metadata keys."""
        return {k: v for k, v in doc.items() if k not in {"_id", "_version"}}

    # ----- async relationship helpers -----
    @classmethod
    async def _aresolve_relations(cls, data: dict) -> dict:
        """Resolve reference dicts to model instances recursively (async)."""

        async def adecode(value: Any) -> Any:
            if isinstance(value, dict):
                if isinstance(value.get("_table"), str) and "_id" in value:
                    table = value["_table"]
                    rid = value["_id"]
                    mdl = registry.resolve(table)
                    if mdl is not None and hasattr(mdl, "from_id"):
                        fn = getattr(mdl, "from_id")
                        if inspect.iscoroutinefunction(fn):
                            try:
                                return await fn(rid)
                            except Exception:
                                return value
                        else:
                            try:
                                return fn(rid)
                            except Exception:
                                return value
                out = {}
                for k, v in value.items():
                    out[k] = await adecode(v)
                return out
            if isinstance(value, list):
                return [await adecode(v) for v in value]
            return value

        out = {}
        for k, v in data.items():
            out[k] = await adecode(v)
        return out

    async def _adump_with_relations(self) -> dict:
        """Serialize model with relationship encoding (async)."""
        from datetime import date, datetime

        async def aencode(value: Any) -> Any:
            # Handle AsyncSQLerLiteModel instances
            if isinstance(value, AsyncSQLerLiteModel):
                if value._id is None:
                    raise ValueError("Related async model must be saved before saving parent")
                table = value.__class__._table
                return {"_table": table, "_id": value._id}

            # Handle sync lite models
            from sqler.models.lite.model import SQLerLiteModel

            if isinstance(value, SQLerLiteModel):
                if value._id is None:
                    raise ValueError("Related model must be saved before saving parent")
                table = value.__class__._table
                return {"_table": table, "_id": value._id}

            # Handle already-encoded ref dicts
            if isinstance(value, dict) and "_table" in value and "_id" in value:
                return {"_table": value["_table"], "_id": value["_id"]}

            # Handle lists
            if isinstance(value, list):
                return [await aencode(v) for v in value]

            # Handle dicts
            if isinstance(value, dict):
                out = {}
                for k, v in value.items():
                    out[k] = await aencode(v)
                return out

            # Handle datetime/date
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, date):
                return value.isoformat()

            return value

        payload: dict = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            payload[f.name] = await aencode(getattr(self, f.name))
        return payload

    # ----- dirty tracking -----
    def is_dirty(self) -> bool:
        """Check if instance has unsaved changes."""
        if self._snapshot is None:
            return True
        current = self.model_dump()
        return current != self._snapshot

    def get_dirty_fields(self) -> set[str]:
        """Get names of fields that have changed since last save/load."""
        if self._snapshot is None:
            return self.get_field_names()
        current = self.model_dump()
        dirty = set()
        for key, value in current.items():
            if key not in self._snapshot or self._snapshot[key] != value:
                dirty.add(key)
        return dirty
