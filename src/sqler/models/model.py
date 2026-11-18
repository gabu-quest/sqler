from __future__ import annotations

import os
import reprlib
import warnings
from typing import TYPE_CHECKING, Any, ClassVar, Optional, Type, TypeVar

from pydantic import BaseModel, PrivateAttr

from sqler import registry
from sqler._stored import stored
from sqler.db._statements import IndexFields
from sqler.db.sqler_db import SQLerDB
from sqler.exceptions import UniqueConstraintError
from sqler.models import _dirty, _refs, _table_names, _write_rules
from sqler.models._table_names import (  # noqa: F401
    _default_table_name,
    _instance_binding,
    _model_table_name,
    _pluralize,
    _read_document,
    _read_documents,
    _register_defined_class,
)
from sqler.models.queryset import SQLerQuerySet
from sqler.query import SQLerExpression
from sqler.utils import validate_field_name, validate_table_name

# The deprecation warning is attributed to the first caller outside the sqler package, so a
# subclass's ``super().set_db()`` does not hide it behind sqler's own file.
_SQLER_PREFIXES = (os.path.dirname(os.path.abspath(registry.__file__)) + os.sep,)

TModel = TypeVar("TModel", bound="SQLerModel")


class SQLerModel(BaseModel):
    """Pydantic-based model with persistence helpers for SQLerDB.

    Instances persist as JSON (excluding the private ``_id`` attribute) into
    a table with schema
    ``(_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)``.

    Usage::

        db = SQLerDB(":memory:")
        user = User(name="Alice").save(db=db)
        users = User.using(db).filter(F("age") > 30).all()

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and ``db=`` for
        every other call (``.save(db=db)``, ``Model.from_id(id, db=db)``).
    """

    # internal id stored outside the JSON blob
    _id: Optional[int] = PrivateAttr(default=None)
    # snapshot of last-loaded document (for merge strategies in perf mode)
    _snapshot: Optional[dict] = PrivateAttr(default=None)

    # class-bound db + table metadata
    _db: ClassVar[Optional[SQLerDB]] = None
    # the database this class takes; every way one comes in checks it (``_check_database``)
    _takes_db: ClassVar[type[SQLerDB]] = SQLerDB
    _table: ClassVar[Optional[str]] = None
    # whether ``from_ids`` reads each row with its ``_version`` column (the safe bases)
    _versioned_rows: ClassVar[bool] = False
    if TYPE_CHECKING:
        # a model's table: its own, or the one ``set_db()`` gives it
        __tablename__: ClassVar[str]

    # ----- class config -----
    model_config = {
        "extra": "ignore",
        "frozen": False,
    }

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        _register_defined_class(cls)

    @reprlib.recursive_repr()
    def __repr__(self) -> str:
        """pydantic's, with a model met again inside itself (a cycle of rows) shown as ``...``."""
        return super().__repr__()

    # ----- class methods -----
    @classmethod
    def set_db(cls: Type[TModel], db: SQLerDB, table: Optional[str] = None) -> None:
        """Bind this model class to a database and table.

        .. deprecated::
            Use :meth:`using` for queries and ``db=`` for every other call
            (``save(db=db)``, ``from_id(id, db=db)``, ``add_index(field, db=db)``).

        Args:
            db: Database instance to use for persistence.
            table: Optional table name. Defaults to lowercase plural of the
                class name (e.g., ``User`` → ``users``).

        Raises:
            TypeError: ``db`` is neither ``None`` nor a ``SQLerDB``; the class is left as it was.
        """
        _table_names._check_given(cls, db)
        warnings.warn(
            f"{cls.__name__}.set_db() is deprecated. "
            f"{_table_names.DATABASE_RULE} "
            "set_db() will be removed in a future version.",
            DeprecationWarning,
            skip_file_prefixes=_SQLER_PREFIXES,
        )
        explicit = getattr(cls, "__tablename__", None)
        chosen = validate_table_name(table or explicit or _default_table_name(cls.__name__))
        cls._db = db
        cls._table = chosen
        cls.__tablename__ = chosen

        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            checks = getattr(cls, "__checks__", None)
            db._ensure_table_with_promoted(cls._table, promoted, checks, model=cls)
        else:
            cls._db._ensure_table(cls._table)
        registry.register(cls._table, cls)

    @classmethod
    def db(cls: Type[TModel]) -> SQLerDB:
        """Return the bound database for this model."""
        db, _ = cls._require_binding()
        return db

    # ergonomic relation field builder
    @classmethod
    def ref(cls, name: str):
        """Return a model-aware field builder for a related field name.

        Usage: User.ref("address").field("city") == "Kyoto"
        """
        from .model_field import SQLerModelField

        class _RefBuilder:
            def __init__(self, model_cls, base: str | list[str]):
                self.model_cls = model_cls
                # a key, or the one-key path ``any()`` passes, which ``SQLerModelField`` reads by its ``str()``
                self.path: list[Any] = [base]

            def field(self, *parts: str) -> SQLerModelField:
                return SQLerModelField(self.model_cls, self.path + list(parts))

            def any(self) -> "_RefAnyBuilder":
                return _RefAnyBuilder(self.model_cls, self.path)

        class _RefAnyBuilder(_RefBuilder):
            def field(self, *parts: str) -> SQLerModelField:
                return SQLerModelField(self.model_cls, self.path + list(parts), array_any=True)

        return _RefBuilder(cls, name)

    _require_binding = classmethod(_table_names._require_binding)
    _resolve_binding = classmethod(_table_names._resolve_binding)

    @classmethod
    def from_id(cls: Type[TModel], id_: int, *, db=None) -> Optional[TModel]:
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
        return _refs.models(cls, [doc], db, table)[0]

    @classmethod
    def from_ids(cls: Type[TModel], ids: list[int], *, db=None) -> list[TModel]:
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
            _table_names._check_given(cls, db)
            return []
        db, table = cls._resolve_binding(db)
        read = _refs.read_versioned if cls._versioned_rows else _read_documents
        return _refs.models(cls, read(db, table, ids), db, table)

    @classmethod
    def count(cls: Type[TModel]) -> int:
        """Return total count of rows in this model's table.

        Shorthand for ``cls.query().count()``.
        """
        return cls.query().count()

    @classmethod
    def query(cls: Type[TModel]) -> SQLerQuerySet[TModel]:
        """Return a queryset for chaining and execution."""
        return cls._queryset(*cls._require_binding())

    @classmethod
    def all(cls: Type[TModel]) -> list[TModel]:
        """Return all instances from the database."""
        return cls.query().all()

    @classmethod
    def first(cls: Type[TModel]) -> Optional[TModel]:
        """Return the first instance or ``None``."""
        return cls.query().first()

    @classmethod
    def using(cls: Type[TModel], db: SQLerDB, table: Optional[str] = None) -> SQLerQuerySet[TModel]:
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
    def _queryset(cls: Type[TModel], db: SQLerDB, table: str) -> SQLerQuerySet[TModel]:
        """The queryset on ``table`` of ``db`` that ``query()`` and ``using()`` give: rows
        read with their version where the class's rows are versioned."""
        from sqler.query import SQLerQuery

        q = SQLerQuery(table=table, adapter=db.adapter)
        if cls._versioned_rows:
            q = q.with_version()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            q = q._clone(promoted_fields=list(promoted.keys()))
        return SQLerQuerySet[TModel](cls, q, db)

    @classmethod
    def filter(cls: Type[TModel], expression: SQLerExpression) -> SQLerQuerySet[TModel]:
        """Shorthand for ``cls.query().filter(expression)``."""
        return cls.query().filter(expression)

    @classmethod
    def first_or_create(
        cls: Type[TModel],
        lookup: dict,
        defaults: Optional[dict] = None,
        *,
        db=None,
    ) -> tuple[TModel, bool]:
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
        except UniqueConstraintError as error:
            return _write_rules.found_after_race(qs.first(), error)
        return inst, True  # type: ignore[return-value]

    @classmethod
    def add_index(
        cls,
        field: IndexFields,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
        db=None,
    ) -> None:
        """Create an index on one field or several via the model class.

        Args:
            field: A field, or a list of fields each a name or ``(name, "asc" | "desc")``,
                as ``create_index`` takes it.
            unique: Enforce uniqueness.
            name: Optional index name.
            where: Optional partial-index WHERE clause.
            db: Optional database (overrides class-level binding).
        """
        db, table = cls._resolve_binding(db)
        db.create_index(table, field, unique=unique, name=name, where=where)

    @classmethod
    def ensure_index(
        cls,
        field: IndexFields,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
        db=None,
    ) -> None:
        """Ensure an index on one field or several exists (idempotent), in ``db``
        when given, else the class's database."""
        cls.add_index(field, unique=unique, name=name, where=where, db=db)

    # ----- instance methods -----
    @classmethod
    def save_many(cls: Type[TModel], instances: list[TModel], *, db=None) -> list[TModel]:
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
        return _write_rules.save_many(cls, instances, db, cls._insert_rows)

    @classmethod
    def _insert_rows(cls, db, table: str, instances: list) -> None:
        """Write new instances in one batch; each takes its ``_id`` and snapshot."""
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            _write_rules.ensure_columns(cls, db, table, promoted)
        payloads = [inst._dump_with_relations() for inst in instances]
        insert, args = _write_rules.rows_insert(db, table, payloads, promoted)
        ids = insert(*args)

        for inst, id_, payload in zip(instances, ids, payloads):
            inst._id = id_
            inst._snapshot = _dirty.snapshot(payload)

    def save(self: TModel, *, db=None, _partial: bool = False) -> TModel:
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

    def _write(self: TModel, db, table: str, _partial: bool) -> TModel:
        """Write this instance's row (``save()`` without the rules of its class)."""
        cls = self.__class__
        payload = self._dump_with_relations()
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            _write_rules.ensure_columns(cls, db, table, promoted)
        written = _write_rules.row_write(db, table, self, payload, _partial, promoted)
        new_id = self._id if _partial else written
        self._id = new_id
        self._snapshot = _dirty.snapshot(payload)
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

    def refresh(self: TModel, *, db=None) -> TModel:
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
        doc = _write_rules.refresh_row(
            _read_document(db, table, self._id, cls._versioned_rows), self._id
        )
        own = {(table, self._id): self}
        return _refs.run(_refs.load(cls, [doc], db, table, self._refreshed, own))[0]

    def _refreshed(self: TModel, doc: dict, row: dict) -> TModel:
        """This instance holding the row ``doc`` (references in place) as ``row`` was read."""
        fresh = _refs.validate(type(self), doc)
        for fname in type(self).model_fields:
            setattr(self, fname, getattr(fresh, fname))
        self._id = doc.get("_id")
        self._snapshot = _dirty.snapshot(row)
        return self

    # ----- dirty tracking -----
    is_dirty = _dirty.is_dirty
    get_dirty_fields = _dirty.get_dirty_fields

    # ----- reference validation -----
    @classmethod
    def validate_references(cls, *, db=None):
        """Validate all references across all tables in the database.

        Args:
            db: Optional database (overrides class-level binding).

        Returns:
            List of BrokenRef instances describing dangling references.
        """
        from .integrity import validate_references

        db, _ = cls._resolve_binding(db)
        return validate_references(db)

    # ----- relationship encoding/decoding -----
    def _dump_with_relations(self) -> dict:
        """The document ``save()`` writes: each field in stored form, a related model as the
        reference to its row."""
        return {
            name: stored(getattr(self, name), SQLerModel, name)
            for name in type(self).model_fields
            if name != "_id"
        }
