"""SQLerLiteModel - Dataclass-based ORM model.

This module provides SQLerLiteModel, a Pydantic-free alternative that uses
standard library dataclasses. It maintains full API compatibility with
SQLerModel while being compatible with Pyodide and other environments
where Pydantic's native extensions cannot be installed.
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
from sqler.exceptions import UniqueConstraintError
from sqler.models import _dirty, _refs, _table_names, _write_rules
from sqler.models._table_names import (  # noqa: F401
    _default_table_name,
    _instance_binding,
    _model_table_name,
    _pluralize,
    _read_document,
    _read_documents,
)
from sqler.models.lite.base import SQLerLiteModelBase
from sqler.utils import validate_field_name, validate_table_name

if TYPE_CHECKING:
    from sqler.db.sqler_db import SQLerDB
    from sqler.models.queryset import SQLerQuerySet

# The deprecation warning is attributed to the first caller outside the sqler package, so a
# subclass's ``super().set_db()`` does not hide it behind sqler's own file.
_SQLER_PREFIXES = (os.path.dirname(os.path.abspath(registry.__file__)) + os.sep,)

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
    # whether ``from_ids`` reads each row with its ``_version`` column (the safe bases)
    _versioned_rows: ClassVar[bool] = False

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
            skip_file_prefixes=_SQLER_PREFIXES,
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
            cls._db._ensure_table_with_promoted(cls._table, promoted, checks, model=cls)
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
    def using(cls: Type[T], db: "SQLerDB", table: Optional[str] = None) -> "SQLerQuerySet[T]":
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
        table = validate_table_name(table) if table is not None else _model_table_name(cls)
        return cls._queryset(db, table)

    @classmethod
    def _queryset(cls: Type[T], db: "SQLerDB", table: str) -> "SQLerQuerySet[T]":
        """The queryset on ``table`` of ``db`` that ``query()`` and ``using()`` give: rows
        read with their version where the class's rows are versioned."""
        from sqler.models.queryset import SQLerQuerySet
        from sqler.query import SQLerQuery

        q = SQLerQuery(table=table, adapter=db.adapter)
        if cls._versioned_rows:
            q = q.with_version()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        return SQLerQuerySet[T](cls, q, db)

    @classmethod
    def db(cls: Type[T]) -> "SQLerDB":
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    _require_binding = classmethod(_table_names._require_binding)
    _resolve_binding = classmethod(_table_names._resolve_binding)

    @classmethod
    def from_id(cls: Type[T], id_: int, *, db=None) -> Optional[T]:
        """Hydrate an instance by ``_id``.

        Args:
            id_: Row id to load.
            db: Optional database (overrides class-level binding).

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._resolve_binding(db)
        doc = _read_document(db, table, id_, cls._versioned_rows)
        if doc is None:
            return None
        return _refs.run(_refs.load(cls, [doc], db, table))[0]

    @classmethod
    def from_ids(cls: Type[T], ids: list[int], *, db=None) -> list[T]:
        """Hydrate multiple instances by id list (batch operation).

        Args:
            ids: List of row ids to load.
            db: Optional database (overrides class-level binding).

        Returns:
            List of model instances (in same order as input ids).
            Missing ids are silently omitted from result.
        """
        if not ids:
            return []
        db, table = cls._resolve_binding(db)
        read = _refs.read_versioned if cls._versioned_rows else _read_documents
        return _refs.run(_refs.load(cls, read(db, table, ids), db, table))

    @classmethod
    def count(cls: Type[T]) -> int:
        """Return total count of rows in this model's table."""
        return cls.query().count()

    @classmethod
    def query(cls: Type[T]) -> "SQLerQuerySet[T]":
        """Return a queryset for chaining and execution."""
        return cls._queryset(*cls._require_binding())

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
        try:
            inst.save(db=db)
        except UniqueConstraintError:
            # another caller inserted the same lookup between the read and the insert
            existing = qs.first()
            if existing is None:
                raise  # refused by a unique index on a field outside the lookup
            return existing, False
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
        return _write_rules.save_many(cls, instances, db, cls._insert_rows)

    @classmethod
    def _insert_rows(cls, db, table: str, instances: list) -> None:
        """Write new instances in one batch; each takes its ``_id`` and snapshot."""
        promoted = getattr(cls, "__promoted__", None)

        if promoted:
            checks = getattr(cls, "__checks__", None)
            db._ensure_promoted(table, promoted, checks, model=cls)

        payloads = [inst._dump_with_relations() for inst in instances]

        if promoted:
            promoted_fields = list(promoted.keys())
            ids = db.insert_many_promoted(table, payloads, promoted_fields)
        else:
            ids = db.insert_many(table, payloads)

        for inst, id_, payload in zip(instances, ids, payloads):
            object.__setattr__(inst, "_id", id_)
            object.__setattr__(inst, "_snapshot", _dirty.snapshot(payload))

    def save(self: T, *, db=None, _partial: bool = False) -> T:
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
        return _write_rules.save(
            self, db, table, _partial, lambda: self._write(db, table, _partial)
        )

    def _write(self: T, db, table: str, _partial: bool) -> T:
        """Write this instance's row (``save()`` without the rules of its class)."""
        cls = self.__class__
        payload = self._dump_with_relations()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
        if _partial:
            new_id = self._id
            changed = _dirty.changed_fields(self._snapshot or {}, payload)
            db._update_fields(table, new_id, payload, changed, promoted or ())
        elif promoted:
            promoted_fields = list(promoted.keys())
            new_id = db.upsert_document_promoted(table, self._id, payload, promoted_fields)
        else:
            new_id = db.upsert_document(table, self._id, payload)
        object.__setattr__(self, "_id", new_id)
        object.__setattr__(self, "_snapshot", _dirty.snapshot(payload))
        return self

    def delete(self, *, db=None) -> None:
        """Delete this instance by ``_id`` and unset it.

        Args:
            db: Optional database to delete from (overrides class-level binding).
        """
        self.delete_with_policy(db=db)

    def delete_with_policy(self, *, db=None, on_delete: str = "restrict") -> None:
        """Delete this instance with a specified integrity policy.

        The delete and what the policy does to referring rows are one transaction
        (the caller's, if one is open), and the instance meets the rules of its class
        as ``delete()`` does: hooks and the audit row.

        Args:
            db: Optional database to delete from (overrides class-level binding).
            on_delete: One of "restrict", "set_null", or "cascade".
        """
        db, table = _instance_binding(self, db)
        _write_rules.delete(self, db, table, on_delete)

    def refresh(self: T, *, db=None) -> T:
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
        cls = self.__class__
        db, table = _instance_binding(self, db)
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = _read_document(db, table, self._id, cls._versioned_rows)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        own = {(table, self._id): self}
        return _refs.run(_refs.load(cls, [doc], db, table, self._refreshed, own))[0]

    def _refreshed(self: T, doc: dict, row: dict) -> T:
        """This instance holding the row ``doc`` (references in place) as ``row`` was read."""
        for f in fields(self):
            if not f.name.startswith("_") and f.name in doc:
                object.__setattr__(self, f.name, doc[f.name])
        object.__setattr__(self, "_snapshot", _dirty.snapshot(row))
        return self

    # ----- relationship encoding/decoding -----
    @classmethod
    def _resolve_relations(cls, data: dict, db=None) -> dict:
        """``data`` with the model of its row in place of each typed reference, loaded from
        ``db`` or the class's database (see ``_refs.resolve``)."""
        return _refs.resolve(data, db if db is not None else cls.db(), {}, cls)

    def _dump_with_relations(self) -> dict:
        """The document ``save()`` writes: each field in stored form, a related model as the
        reference to its row."""
        return {
            f.name: stored(getattr(self, f.name), SQLerLiteModel, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
