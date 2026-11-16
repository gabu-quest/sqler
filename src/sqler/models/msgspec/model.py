"""SQLerMsgspecModel — msgspec Struct-based ORM model.

This module provides SQLerMsgspecModel, a high-performance alternative to
SQLerLiteModel that uses msgspec Structs for C-level JSON decode+validate.
It maintains full API compatibility with SQLerLiteModel.
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
)
from sqler.models.msgspec.base import SQLerMsgspecModelBase

if TYPE_CHECKING:
    from sqler.models.queryset import SQLerQuerySet

T = TypeVar("T", bound="SQLerMsgspecModel")


class SQLerMsgspecModel(SQLerMsgspecModelBase):
    """Msgspec Struct-based SQLer model with persistence helpers.

    Instances persist as JSON into SQLite. Its async twin is ``AsyncSQLerMsgspecModel``;
    the checks and conversions both make are in ``SQLerMsgspecModelBase``.

    Usage::

        from sqler.models.msgspec import SQLerMsgspecModel
        from sqler import SQLerDB

        class User(SQLerMsgspecModel):
            __tablename__ = "users"
            name: str
            email: str

        db = SQLerDB(":memory:")
        user = User(name="Alice", email="alice@example.com")
        user.save(db=db)
        users = User.using(db).filter(F("age") > 30).all()

    .. note::
        ``set_db()`` is deprecated. Prefer ``.using(db)`` for queries and ``db=`` for
        every other call (``.save(db=db)``, ``Model.from_id(id, db=db)``).
    """

    # Class-level database binding — no type annotation to avoid msgspec
    # trying to resolve forward references at class creation time
    _db = None
    _table = None
    # the database this class takes; every way one comes in checks it (``_check_database``)
    _takes_db = SQLerDB
    # no msgspec base keeps a version on its rows
    _versioned_rows = False

    @classmethod
    def set_db(cls: Type[T], db: "SQLerDB", table: Optional[str] = None) -> None:
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
        chosen = cls._bind_class(db, table)
        promoted = getattr(cls, "__promoted__", None)
        if promoted:
            checks = getattr(cls, "__checks__", None)
            db._ensure_table_with_promoted(chosen, promoted, checks, model=cls)
        else:
            db._ensure_table(chosen)
        registry.register(chosen, cls)

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
        return cls._queryset(db, cls._using_table(db, table))

    @classmethod
    def _queryset(cls: Type[T], db: "SQLerDB", table: str) -> "SQLerQuerySet[T]":
        """The queryset on ``table`` of ``db`` that ``query()`` and ``using()`` give: rows
        read with their version where the class's rows are versioned."""
        from sqler.models.queryset import SQLerQuerySet
        from sqler.query import SQLerQuery

        return SQLerQuerySet[T](cls, cls._shaped(SQLerQuery(table=table, adapter=db.adapter)), db)

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

        A typed reference is the model of its row, as on every base.

        Args:
            id_: Row id to load.
            db: Optional database (overrides class-level binding).

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._resolve_binding(db)
        doc = _read_document(db, table, id_)
        if doc is None:
            return None
        return _refs.models(cls, [doc], db, table)[0]

    @classmethod
    def from_ids(cls: Type[T], ids: list[int], *, db=None) -> list[T]:
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
        return _refs.models(cls, _read_documents(db, table, ids), db, table)

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
        qs = cls._lookup(lookup, db)
        existing = qs.first()
        if existing is not None:
            return existing, False
        inst = cls(**{**lookup, **(defaults or {})})
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
        db=None,
    ) -> None:
        """Create an index on a JSON field via the model class, in ``db`` when given, else the class's database."""
        db, table = cls._resolve_binding(db)
        db.create_index(table, field, unique=unique, name=name, where=where)

    @classmethod
    def ensure_index(
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
        cls.add_index(field, unique=unique, name=name, where=where, db=db)

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
            db._ensure_promoted(table, promoted, getattr(cls, "__checks__", None), model=cls)
        payloads = [inst._dump_with_relations() for inst in instances]
        if promoted:
            ids = db.insert_many_promoted(table, payloads, list(promoted.keys()))
        else:
            ids = db.insert_many(table, payloads)
        cls._inserted(instances, ids, payloads)

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
            new_id = db.upsert_document_promoted(table, self._id, payload, list(promoted.keys()))
        else:
            new_id = db.upsert_document(table, self._id, payload)
        return self._wrote(new_id, payload)

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
        db, table = _instance_binding(self, db)
        doc = _read_document(db, table, self._saved_id())
        return _refs.run(self._reload(doc, db, table))[0]


# a msgspec model is stored as the reference to its row (an async one is refused: its row
# is read and written through an AsyncSQLerDB)
SQLerMsgspecModel._reference_bases = (SQLerMsgspecModel,)
