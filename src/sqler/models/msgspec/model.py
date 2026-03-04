"""SQLerMsgspecModel — msgspec Struct-based ORM model.

This module provides SQLerMsgspecModel, a high-performance alternative to
SQLerLiteModel that uses msgspec Structs for C-level JSON decode+validate.
It maintains full API compatibility with SQLerLiteModel.
"""

from __future__ import annotations

import warnings
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Type,
    TypeVar,
)

from sqler import registry
from sqler.exceptions import NotBoundError
from sqler.models.msgspec.base import SQLerMsgspecModelBase, _public_fields
from sqler.utils import validate_table_name

if TYPE_CHECKING:
    from sqler.db.sqler_db import SQLerDB
    from sqler.models.queryset import SQLerQuerySet

T = TypeVar("T", bound="SQLerMsgspecModel")


def _pluralize(word: str) -> str:
    """Pluralize a word using common English rules."""
    w = word.lower()
    if w.endswith("s"):
        return w
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"
    return w + "s"


def _default_table_name(name: str) -> str:
    """Generate default table name from class name."""
    lower = name.lower()
    reserved = {"a", "as", "by", "and", "or", "not", "null", "index", "table"}
    if lower in reserved:
        return lower + "_tbl"
    return _pluralize(name)


class SQLerMsgspecModel(SQLerMsgspecModelBase):
    """Msgspec Struct-based SQLer model with persistence helpers.

    Define subclasses as msgspec Structs. Bind the class to a database
    via :meth:`set_db`. Instances persist as JSON into SQLite.

    Usage::

        from sqler.models.msgspec import SQLerMsgspecModel
        from sqler import SQLerDB

        class User(SQLerMsgspecModel):
            __tablename__ = "users"
            name: str
            email: str

        db = SQLerDB(":memory:")
        User.set_db(db)

        user = User(name="Alice", email="alice@example.com")
        user.save()
    """

    # Class-level database binding — no type annotation to avoid msgspec
    # trying to resolve forward references at class creation time
    _db = None
    _table = None

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
        cls._db._ensure_table(cls._table)
        registry.register(cls._table, cls)

    @classmethod
    def bind(cls: Type[T], db: "SQLerDB", table: Optional[str] = None) -> None:
        """Alias for set_db()."""
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
        return SQLerQuerySet[T](cls, q)

    @classmethod
    def db(cls: Type[T]) -> "SQLerDB":
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    @classmethod
    def _require_binding(cls) -> tuple["SQLerDB", str]:
        """Return the bound DB and table or raise if unbound."""
        if cls._db is None or cls._table is None:
            raise NotBoundError(
                f"Model {cls.__name__} is not bound. Call set_db(db, table?) first.",
                details={"model": cls.__name__},
            )
        return cls._db, cls._table

    @classmethod
    def _resolve_binding(cls, db=None) -> tuple["SQLerDB", str]:
        """Return (db, table) using the provided db or the class-level binding."""
        if db is not None:
            table = (
                cls._table
                or getattr(cls, "__tablename__", None)
                or _default_table_name(cls.__name__)
            )
            return db, validate_table_name(table)
        return cls._require_binding()

    @classmethod
    def from_id(cls: Type[T], id_: int) -> Optional[T]:
        """Hydrate an instance by ``_id``.

        Note: relation resolution is not performed (prototype scope).
        Fields containing reference dicts (``{"_table": ..., "_id": ...}``)
        are returned as-is. Use ``SQLerLiteModel`` if relation hydration
        is needed.

        Args:
            id_: Row id to load.

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._require_binding()
        doc = db.find_document(table, id_)
        if doc is None:
            return None
        inst = cls.model_validate(doc)
        inst._id = doc.get("_id")
        inst._snapshot = cls._make_snapshot(doc)
        return inst

    @classmethod
    def from_ids(cls: Type[T], ids: list[int]) -> list[T]:
        """Hydrate multiple instances by id list (batch operation).

        Note: relation resolution is not performed (prototype scope).
        See :meth:`from_id` for details.

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
        instances = []
        for doc in docs:
            inst = cls.model_validate(doc)
            inst._id = doc.get("_id")
            inst._snapshot = cls._make_snapshot(doc)
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
        new_id = db.upsert_document(table, self._id, payload)
        self._id = new_id
        self._snapshot = payload.copy()
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
        self._id = None

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
        # Update fields in-place
        for f in _public_fields(type(self)):
            if f.name in doc:
                setattr(self, f.name, doc[f.name])
        self._snapshot = cls._make_snapshot(doc)
        return self

    @classmethod
    def _make_snapshot(cls, doc: dict) -> dict:
        """Create snapshot excluding metadata keys."""
        return {k: v for k, v in doc.items() if k not in {"_id", "_version"}}

    def _dump_with_relations(self) -> dict:
        """Serialize model with relationship encoding.

        Related model instances are encoded as {"_table": ..., "_id": ...}
        for storage.
        """
        from datetime import date, datetime

        visited: set[tuple[str, int]] = set()

        def encode(value: object) -> Any:
            # Handle SQLerMsgspecModel instances
            if isinstance(value, SQLerMsgspecModel):
                if value._id is None:
                    raise ValueError("Related model must be saved before saving parent")
                table = value.__class__._table
                if table is None:
                    raise ValueError(
                        f"Related model {value.__class__.__name__} has no bound table"
                    )
                table = validate_table_name(table)
                if (table, int(value._id)) not in visited:
                    visited.add((table, int(value._id)))
                return {"_table": table, "_id": value._id}

            # Handle already-encoded ref dicts
            if isinstance(value, dict) and "_table" in value and "_id" in value:
                return {"_table": value["_table"], "_id": value["_id"]}

            if isinstance(value, list):
                return [encode(v) for v in value]
            if isinstance(value, dict):
                return {k: encode(v) for k, v in value.items()}
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, date):
                return value.isoformat()
            return value

        payload: dict = {}
        for f in _public_fields(type(self)):
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
