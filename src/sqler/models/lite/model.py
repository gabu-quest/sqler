"""SQLerLiteModel - Dataclass-based ORM model.

This module provides SQLerLiteModel, a Pydantic-free alternative that uses
standard library dataclasses. It maintains full API compatibility with
SQLerModel while being compatible with Pyodide and other environments
where Pydantic's native extensions cannot be installed.
"""

from __future__ import annotations

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
from sqler.models._table_names import _default_table_name, _pluralize  # noqa: F401
from sqler.models.lite.base import SQLerLiteModelBase
from sqler.utils import validate_field_name, validate_table_name

if TYPE_CHECKING:
    from sqler.db.sqler_db import SQLerDB
    from sqler.models.queryset import SQLerQuerySet

T = TypeVar("T", bound="SQLerLiteModel")


class SQLerLiteModel(SQLerLiteModelBase):
    """Dataclass-based SQLer model with persistence helpers.

    Instances persist as JSON into SQLite.

    Usage::

        from dataclasses import dataclass
        from sqler import SQLerLiteModel, SQLerDB

        @dataclass
        class User(SQLerLiteModel):
            __tablename__ = "users"
            name: str
            email: str

        db = SQLerDB(":memory:")
        user = User(name="Alice", email="alice@example.com")
        user.save(db=db)
        users = User.using(db).filter(F("age") > 30).all()

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and
        ``.save(db=db)`` / ``.delete(db=db)`` for writes.
    """

    # Class-level database binding (not dataclass fields)
    _db: ClassVar[Optional["SQLerDB"]] = None
    _table: ClassVar[Optional[str]] = None

    # ----- class methods -----
    @classmethod
    def set_db(cls: Type[T], db: "SQLerDB", table: Optional[str] = None) -> None:
        """Bind this model class to a database and table.

        .. deprecated::
            Use :meth:`using` for queries and ``save(db=db)``/``delete(db=db)``
            for writes.

        Args:
            db: Database instance to use for persistence.
            table: Optional table name. Defaults to lowercase plural of the
                class name (e.g., ``User`` → ``users``).
        """
        warnings.warn(
            f"{cls.__name__}.set_db() is deprecated. "
            "Use .using(db) for queries and .save(db=db)/.delete(db=db) for writes. "
            "set_db() will be removed in a future version.",
            DeprecationWarning,
            stacklevel=2,
        )
        explicit = getattr(cls, "__tablename__", None)
        chosen = validate_table_name(
            table or explicit or _default_table_name(cls.__name__)
        )
        cls._db = db
        cls._table = chosen
        cls.__tablename__ = chosen  # type: ignore[attr-defined]
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            checks = getattr(cls, "__checks__", None)
            cls._db._ensure_table_with_promoted(cls._table, promoted, checks)
        else:
            cls._db._ensure_table(cls._table)
        registry.register(cls._table, cls)

    @classmethod
    def bind(cls: Type[T], db: "SQLerDB", table: Optional[str] = None) -> None:
        """Alias for set_db() (deprecated)."""
        warnings.warn(
            f"{cls.__name__}.bind() is deprecated. "
            "Use .using(db) for queries and .save(db=db)/.delete(db=db) for writes. "
            "bind() will be removed in a future version.",
            DeprecationWarning,
            stacklevel=2,
        )
        with warnings.catch_warnings():
            # bind() already emitted its own deprecation; suppress the inner set_db() one
            warnings.simplefilter("ignore", DeprecationWarning)
            cls.set_db(db, table)

    @classmethod
    def using(cls: Type[T], db: "SQLerDB") -> "SQLerQuerySet[T]":
        """Return a queryset bound to a specific DB (no class-level mutation)."""
        from sqler.models.queryset import SQLerQuerySet
        from sqler.query import SQLerQuery

        table = validate_table_name(
            getattr(cls, "__tablename__", None) or _default_table_name(cls.__name__)
        )
        q = SQLerQuery(table=table, adapter=db.adapter)
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        return SQLerQuerySet[T](cls, q)

    @classmethod
    def db(cls: Type[T]) -> "SQLerDB":
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    @classmethod
    def _require_binding(cls) -> tuple["SQLerDB", str]:
        """Return the bound DB and table or raise if unbound.

        Raises:
            NotBoundError: If :meth:`set_db` has not been called.
        """
        if cls._db is None or cls._table is None:
            raise NotBoundError(
                f"Model {cls.__name__} is not bound. "
                "Use .using(db) for queries or .save(db=db) for writes.",
                details={"model": cls.__name__},
            )
        return cls._db, cls._table

    @classmethod
    def _resolve_binding(cls, db=None) -> tuple["SQLerDB", str]:
        """Return (db, table) using the provided db or the class-level binding."""
        if db is not None:
            table = cls._table or getattr(cls, "__tablename__", None) or _default_table_name(cls.__name__)
            return db, validate_table_name(table)
        return cls._require_binding()

    @classmethod
    def from_id(cls: Type[T], id_: int) -> Optional[T]:
        """Hydrate an instance by ``_id``.

        Args:
            id_: Row id to load.

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._require_binding()
        doc = db.find_document(table, id_)
        if doc is None:
            return None
        doc = cls._resolve_relations(doc)
        inst = cls.model_validate(doc)
        object.__setattr__(inst, "_id", doc.get("_id"))
        object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
        return inst

    @classmethod
    def from_ids(cls: Type[T], ids: list[int]) -> list[T]:
        """Hydrate multiple instances by id list (batch operation).

        Args:
            ids: List of row ids to load.

        Returns:
            List of model instances (in same order as input ids).
            Missing ids are silently omitted from result.
        """
        if not ids:
            return []
        db, table = cls._require_binding()
        docs = db.find_documents(table, ids)
        if not docs:
            return []
        # Use queryset's batch resolution
        qs = cls.query()
        docs = qs._batch_resolve(docs)
        instances = []
        for doc in docs:
            inst = cls.model_validate(doc)
            object.__setattr__(inst, "_id", doc.get("_id"))
            object.__setattr__(inst, "_snapshot", cls._make_snapshot(doc))
            instances.append(inst)
        return instances

    @classmethod
    def count(cls: Type[T]) -> int:
        """Return total count of rows in this model's table."""
        return cls.query().count()

    @classmethod
    def query(cls: Type[T]) -> "SQLerQuerySet[T]":
        """Return a queryset for chaining and execution."""
        db, table = cls._require_binding()
        q = db.query(table)
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        from sqler.models.queryset import SQLerQuerySet

        return SQLerQuerySet[T](cls, q)

    @classmethod
    def filter(cls: Type[T], expression: Any) -> "SQLerQuerySet[T]":
        """Shorthand for ``cls.query().filter(expression)``."""
        return cls.query().filter(expression)

    @classmethod
    def all(cls: Type[T]) -> list[T]:
        """Return all instances from the database."""
        return cls.query().all()

    @classmethod
    def first(cls: Type[T]) -> Optional[T]:
        """Return the first instance or None."""
        return cls.query().first()

    @classmethod
    def first_or_create(
        cls: Type[T],
        lookup: dict,
        defaults: Optional[dict] = None,
        *,
        db=None,
    ) -> tuple[T, bool]:
        """Find the first matching row or create a new one.

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
        existing = qs.first()
        if existing is not None:
            return existing, False
        merged = {**lookup, **(defaults or {})}
        inst = cls(**merged)
        inst.save(db=db)
        return inst, True

    @classmethod
    def add_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Create an index on a JSON field via the model class."""
        db, table = cls._require_binding()
        db.create_index(table, field, unique=unique, name=name, where=where)

    @classmethod
    def ensure_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Ensure an index on a JSON path or literal column exists (idempotent)."""
        cls.add_index(field, unique=unique, name=name, where=where)

    # ----- instance methods -----
    @classmethod
    def save_many(cls: Type[T], instances: list[T], *, db=None) -> list[T]:
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
                raise ValueError("save_many() only accepts new instances (_id must be None)")

        db, table = cls._resolve_binding(db)
        promoted = getattr(cls, "__promoted__", None)

        if promoted:
            checks = getattr(cls, "__checks__", None)
            db._ensure_table_with_promoted(table, promoted, checks)

        payloads = [inst._dump_with_relations() for inst in instances]

        if promoted:
            promoted_fields = list(promoted.keys())
            ids = db.insert_many_promoted(table, payloads, promoted_fields)
        else:
            ids = db.insert_many(table, payloads)

        for inst, id_ in zip(instances, ids):
            object.__setattr__(inst, "_id", id_)
        return instances

    def save(self: T, *, db=None) -> T:
        """Insert or update this instance and update ``_id``.

        Args:
            db: Optional database to write to (overrides class-level binding).

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._resolve_binding(db)
        payload = self._dump_with_relations()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            promoted_fields = list(promoted.keys())
            new_id = db.upsert_document_promoted(table, self._id, payload, promoted_fields)
        else:
            new_id = db.upsert_document(table, self._id, payload)
        object.__setattr__(self, "_id", new_id)
        object.__setattr__(self, "_snapshot", payload.copy())
        return self

    def delete(self, *, db=None) -> None:
        """Delete this instance by ``_id`` and unset it.

        Args:
            db: Optional database to delete from (overrides class-level binding).
        """
        self.delete_with_policy(db=db)

    def delete_with_policy(self, *, db=None, on_delete: str = "restrict") -> None:
        """Delete this instance with a specified integrity policy.

        Args:
            db: Optional database to delete from (overrides class-level binding).
            on_delete: One of "restrict", "set_null", or "cascade".
        """
        cls = self.__class__
        db, table = cls._resolve_binding(db)
        if self._id is None:
            raise ValueError("Cannot delete unsaved model (missing _id)")
        target = (table, int(self._id))
        if on_delete not in {"restrict", "set_null", "cascade"}:
            raise ValueError("on_delete must be 'restrict','set_null', or 'cascade'")

        # Find referrers
        from sqler.models import ReferentialIntegrityError
        from sqler.models.integrity import (
            cascade_delete,
            find_referrers,
            set_null_referrers,
        )

        referrers = find_referrers(db, table, int(self._id))
        if on_delete == "restrict" and referrers:
            raise ReferentialIntegrityError(
                f"Cannot delete {table}:{self._id}; referenced by {len(referrers)} row(s)"
            )
        if on_delete == "set_null":
            set_null_referrers(db, table, int(self._id), referrers)
        elif on_delete == "cascade":
            visited: set[tuple[str, int]] = {target}
            cascade_delete(db, referrers, visited)

        db.delete_document(table, self._id)
        object.__setattr__(self, "_id", None)

    def refresh(self: T) -> T:
        """Reload this instance's fields from the database.

        Raises:
            ValueError: If the instance has not been saved.
            LookupError: If the row no longer exists.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = db.find_document(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        doc = cls._resolve_relations(doc)
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

    # ----- relationship encoding/decoding -----
    @classmethod
    def _is_ref_dict(cls, value: object) -> bool:
        """Check if value is a reference dict."""
        return isinstance(value, dict) and "_table" in value and "_id" in value

    @classmethod
    def _resolve_relations(cls, data: dict) -> dict:
        """Resolve reference dicts to model instances recursively."""

        def decode(value: object) -> Any:
            if isinstance(value, dict):
                if cls._is_ref_dict(value):
                    table = value.get("_table")
                    rid = value.get("_id")
                    mdl = registry.resolve(table) if isinstance(table, str) else None
                    if mdl is not None and hasattr(mdl, "from_id"):
                        try:
                            return mdl.from_id(rid)
                        except (LookupError, RuntimeError, ValueError):
                            return value
                return {k: decode(v) for k, v in value.items()}
            if isinstance(value, list):
                return [decode(v) for v in value]
            return value

        return {k: decode(v) for k, v in data.items()}

    def _dump_with_relations(self) -> dict:
        """Serialize model with relationship encoding.

        Related model instances are encoded as {"_table": ..., "_id": ...}
        for storage.
        """
        from datetime import date, datetime

        visited: set[tuple[str, int]] = set()

        def encode(value: object) -> Any:
            # Handle SQLerLiteModel instances
            if isinstance(value, SQLerLiteModel):
                if value._id is None:
                    raise ValueError("Related model must be saved before saving parent")
                table = value.__class__._table
                if table and (table, int(value._id)) not in visited:
                    visited.add((table, int(value._id)))
                return {"_table": table, "_id": value._id}

            # Handle already-encoded ref dicts
            if isinstance(value, dict) and "_table" in value and "_id" in value:
                return {"_table": value["_table"], "_id": value["_id"]}

            # Handle lists
            if isinstance(value, list):
                return [encode(v) for v in value]

            # Handle dicts
            if isinstance(value, dict):
                return {k: encode(v) for k, v in value.items()}

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
            payload[f.name] = encode(getattr(self, f.name))
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
