from __future__ import annotations

import inspect
from typing import Any, ClassVar, Optional, Type, TypeVar

from pydantic import BaseModel, PrivateAttr

from sqler import registry
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import NotBoundError
from sqler.models.async_queryset import AsyncSQLerQuerySet
from sqler.query import SQLerExpression
from sqler.query.async_query import AsyncSQLerQuery

TAModel = TypeVar("TAModel", bound="AsyncSQLerModel")


class AsyncSQLerModel(BaseModel):
    """Async Pydantic-based model with persistence helpers."""

    _id: Optional[int] = PrivateAttr(default=None)
    _db: ClassVar[Optional[AsyncSQLerDB]] = None
    _table: ClassVar[Optional[str]] = None

    model_config = {"extra": "ignore"}

    @classmethod
    def set_db(cls, db: AsyncSQLerDB, table: Optional[str] = None) -> None:
        cls._db = db
        explicit = getattr(cls, "__tablename__", None)
        base = cls.__name__.lower()
        if not base.endswith("s"):
            base = base + "s"
        if base in {"as"}:
            base = base + "_tbl"
        chosen = table or explicit or base
        cls._table = chosen
        cls.__tablename__ = chosen
        registry.register(cls._table, cls)

    @classmethod
    def _require_binding(cls) -> tuple[AsyncSQLerDB, str]:
        if cls._db is None or cls._table is None:
            raise NotBoundError(
                f"Model {cls.__name__} is not bound. Call set_db(db, table?) first.",
                details={"model": cls.__name__}
            )
        return cls._db, cls._table

    @classmethod
    async def from_id(cls: Type[TAModel], id_: int) -> Optional[TAModel]:
        db, table = cls._require_binding()
        doc = await db.find_document(table, id_)
        if doc is None:
            return None
        doc = await cls._aresolve_relations(doc)
        inst = cls.model_validate(doc)
        inst._id = doc.get("_id")
        return inst  # type: ignore[return-value]

    @classmethod
    def query(cls: Type[TAModel]) -> AsyncSQLerQuerySet[TAModel]:
        db, table = cls._require_binding()
        q = AsyncSQLerQuery(table=table, adapter=db.adapter)
        return AsyncSQLerQuerySet[TAModel](cls, q)

    @classmethod
    def filter(cls: Type[TAModel], expression: SQLerExpression) -> AsyncSQLerQuerySet[TAModel]:
        return cls.query().filter(expression)

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

    async def save(self: TAModel) -> TAModel:
        cls = self.__class__
        db, table = cls._require_binding()
        payload = await self._adump_with_relations()
        new_id = await db.upsert_document(table, self._id, payload)
        self._id = new_id
        return self

    async def delete(self) -> None:
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot delete unsaved model (missing _id)")
        # reuse execute directly for delete to keep API small
        await db.adapter.execute(f"DELETE FROM {table} WHERE _id = ?;", [self._id])
        await db.adapter.commit()
        self._id = None

    async def refresh(self: TAModel) -> TAModel:
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = await db.find_document(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        doc = await cls._aresolve_relations(doc)
        fresh = cls.model_validate(doc)
        for fname in self.__class__.model_fields:
            if fname == "_id":
                continue
            setattr(self, fname, getattr(fresh, fname))
        self._id = doc.get("_id")
        return self

    # ----- relationship helpers (async) -----
    @classmethod
    async def _aresolve_relations(cls, data: dict) -> dict:
        async def adecode(value: Any):
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
        async def aencode(value: Any):
            from sqler.models.async_model import AsyncSQLerModel
            from sqler.models.model import SQLerModel

            if isinstance(value, AsyncSQLerModel):
                if value._id is None:
                    raise ValueError("Related async model must be saved before saving parent")
                table = value.__class__._table
                return {"_table": table, "_id": value._id}
            if isinstance(value, SQLerModel):
                if value._id is None:
                    raise ValueError("Related model must be saved before saving parent")
                table = value.__class__._table
                return {"_table": table, "_id": value._id}
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
