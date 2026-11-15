from __future__ import annotations

import logging
from typing import Any, Generic, Iterator, Optional, Type, TypeVar

from sqler import registry
from sqler.models import _refs, _write_rules
from sqler.models._table_names import _Home
from sqler.query import SQLerExpression, SQLerQuery

T = TypeVar("T")

logger = logging.getLogger("sqler.queryset")


class SQLerQuerySet(Generic[T]):
    """Query wrapper that materializes model instances.

    This class wraps a :class:`~sqler.query.query.SQLerQuery` and converts
    results into instances of the bound Pydantic model class.
    """

    def __init__(self, model_cls: Type[T], query: SQLerQuery, db: Any = None) -> None:
        """``db``: the database ``query`` reads, which reads the rows references point at
        (optional: without it they are read as ``_id`` and ``data`` through the query's
        adapter).

        The table is noted as one where rows of ``model_cls`` live (``registry.note``), so a
        delete of a row they may refer to reads its fields there."""
        self._model_cls = model_cls
        self._query = query
        self._db = db
        self._resolve = True
        if isinstance(model_cls, type):
            registry.note(query._table, model_cls)

    def _copy(self, query: SQLerQuery) -> "SQLerQuerySet[T]":
        """A queryset on ``query`` holding everything this one holds but its query."""
        clone = object.__new__(self.__class__)  # not __init__: its class and table are noted
        clone._model_cls = self._model_cls
        clone._query = query
        clone._db = self._db
        clone._resolve = self._resolve
        return clone

    def _checked(self, use: str = "") -> SQLerQuery:
        """The query, once its database has checked the table (``SchemaError`` when a promoted
        column the rows are read with would hide a value still in a document), for ``use``.

        A versioned query (``with_version()``) asks its database whether the table has the
        ``_version`` column (``_has_version_column``: no statement once it is remembered)
        where it needs the column: for ``"rows"`` (rows read or returned) a table without it
        is read without it, each row at version 0, and its schema stays as it is; for
        ``"update"`` (a set-based update moves each row's version) the table gets the column
        first, as the first versioned write of one row adds it; a missing table stays
        missing, and the update finds no row. Any other use (a count, an aggregate) reads no
        version and asks nothing."""
        db, query = self._db, self._query
        if use and db is not None and query._include_version:
            has = db._has_version_column(query._table)
            if use == "update":
                if has is False:
                    db._ensure_versioned_table(query._table)
            elif not has:
                query = query._clone(include_version=False)
        if query._promoted_fields and db is not None:
            db._columns_of(query._table)
        return query

    def resolve(self, flag: bool) -> "SQLerQuerySet[T]":
        """Toggle relation hydration on result materialization (default True)."""
        clone = self._copy(self._query)
        clone._resolve = flag
        return clone

    # chaining returns new wrappers
    def filter(self, expression: SQLerExpression) -> "SQLerQuerySet[T]":
        """Return a new queryset filtered by the expression."""
        return self._copy(self._query.filter(expression))

    def exclude(self, expression: SQLerExpression) -> "SQLerQuerySet[T]":
        """Return a new queryset excluding rows matching the expression."""
        return self._copy(self._query.exclude(expression))

    def or_filter(self, expression: SQLerExpression) -> "SQLerQuerySet[T]":
        """Return a new queryset with the expression OR-ed in."""
        return self._copy(self._query.or_filter(expression))

    def distinct(self) -> "SQLerQuerySet[T]":
        """Return a new queryset with DISTINCT results."""
        return self._copy(self._query.distinct())

    def select(self, *fields: str) -> "SQLerQuerySet[T]":
        """Return a new queryset that only retrieves specified fields."""
        return self._copy(self._query.select(*fields))

    def order_by(self, *fields: str, desc: bool = False) -> "SQLerQuerySet[T]":
        """Return a new queryset ordered by the given JSON field(s)."""
        return self._copy(self._query.order_by(*fields, desc=desc))

    def limit(self, n: int) -> "SQLerQuerySet[T]":
        """Return a new queryset with a LIMIT clause."""
        return self._copy(self._query.limit(n))

    def group_by(self, *fields: str) -> "SQLerQuerySet[T]":
        """Return a new queryset grouped by the given field(s).

        When active, aggregate methods return ``list[dict]`` instead of scalars.
        """
        return self._copy(self._query.group_by(*fields))

    def having(self, expression: SQLerExpression) -> "SQLerQuerySet[T]":
        """Return a new queryset with a HAVING clause (requires group_by)."""
        return self._copy(self._query.having(expression))

    # execution
    def all(self) -> list[T]:
        """Execute and return a list of model instances."""
        return self._load(self._checked("rows").all_dicts())

    def as_dicts(self) -> list[dict[str, Any]]:
        """Execute and return raw dicts without Pydantic hydration.

        Returns dicts with ``_id`` attached. Respects filters, ordering,
        limits, and ``.select()`` field projection — but skips model
        validation, type coercion, computed fields, and relation resolution.
        """
        return self._checked("rows").all_dicts()

    def iter(self) -> Iterator[T]:
        """Yield model instances one at a time (memory-efficient streaming).

        Unlike ``all()`` which loads all rows into memory and resolves
        relations in batch, this method streams row by row. Relation
        resolution is NOT performed during streaming. On a plain ``":memory:"``
        database the rows are read whole first and held while they are yielded.

        Yields:
            T: Model instances.
        """
        home = _Home(self._db, self._query._table)
        for doc in self._checked("rows").iter_dicts():
            yield _refs.from_row(self._model_cls, doc, doc, home)

    def first(self) -> Optional[T]:
        """Execute with LIMIT 1 and return the first model instance, if any."""
        row = self._checked("rows").first_dict()
        if row is None:
            return None
        return (self._load([row]))[0]

    def _models(self, docs: list[dict]) -> list[T]:
        """The models of ``docs``, rows this queryset read as they were read (no reference
        followed), each with its home: this queryset's database and table."""
        home = _Home(self._db, self._query._table)
        return [_refs.from_row(self._model_cls, doc, doc, home) for doc in docs]

    def count(self) -> int:
        """Return the count of matching rows."""
        return self._checked().count()

    def sum(self, field: str) -> Optional[float]:
        """Return the sum of values for the specified field."""
        return self._checked().sum(field)

    def avg(self, field: str) -> Optional[float]:
        """Return the average of values for the specified field."""
        return self._checked().avg(field)

    def min(self, field: str) -> Optional[Any]:
        """Return the minimum value for the specified field."""
        return self._checked().min(field)

    def max(self, field: str) -> Optional[Any]:
        """Return the maximum value for the specified field."""
        return self._checked().max(field)

    def exists(self) -> bool:
        """Check if any rows match the query."""
        return self._checked().exists()

    def distinct_values(self, field: str) -> list[Any]:
        """Return distinct values for a JSON field."""
        return self._checked().distinct_values(field)

    def paginate(self, page: int, per_page: int = 20):
        """Return a paginated result set."""
        return self._checked("rows").paginate(page, per_page)

    def update(self, **fields) -> int:
        """Update matching rows with the given field values.

        A set-based write meets the rules of the class as ``save()`` does, but for its
        hooks: on a versioned class each row's version moves; a class that stamps sets
        ``updated_at`` (and ``updated_by``) on each row, at the statement's one time, unless
        the caller names the field; an audit log gets an ``update`` row per row changed, in
        one transaction with the write. It calls no hook; use a loop of ``save()`` where a
        hook must run.

        Args:
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            int: Number of rows updated.
        """
        fields = _write_rules.stamped(self._model_cls, fields)
        if not _write_rules.row_rules(self._model_cls):
            return self._checked("update").update(**fields)
        return len(self._written("update", lambda q: q._changed(fields)))

    def update_one(self, **fields) -> Optional[T]:
        """Atomically update one matching row and return it as a model instance.

        Uses the query's WHERE and ORDER BY to select the row, then updates
        and returns it in a single atomic operation. The class's rules apply as in
        :meth:`update`; no hook is called.

        Args:
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            Model instance or None if no row matched.
        """
        docs = self._updated(fields, 1, "=")
        if not docs:
            return None
        return self._models(docs[:1])[0]

    def update_n(self, n: int, **fields) -> list[T]:
        """Atomically update up to *n* matching rows and return them as model instances.

        The class's rules apply as in :meth:`update`; no hook is called.

        Args:
            n: Maximum number of rows to update.
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            list: Model instances, or empty list if no rows matched.
        """
        docs = self._updated(fields, n, "IN")
        return self._models(docs)

    def delete_all(self, on_delete: str = "restrict") -> int:
        """Delete all matching rows (bulk delete).

        Note: This is named delete_all to avoid confusion with model.delete().
        ``on_delete`` is what a row's ``delete_with_policy()`` takes, with the same default:
        ``restrict`` raises ``ReferentialIntegrityError`` and deletes nothing while a row
        outside the set refers to a row in it; ``set_null`` nulls those references;
        ``cascade`` deletes those rows too. An audit log gets a ``delete`` row per row
        deleted, with the row as it was stored. The rows, the policy and the audit rows are
        one unit. It calls no hook. A class no class can refer to, with no row rules, sends
        its one ``DELETE``. A queryset built without a database raises ``RuntimeError`` where
        the delete would be such a unit, as a set-based update with row rules does.

        Returns:
            int: Number of rows deleted.
        """
        if not _write_rules.set_delete(self._model_cls, self._query._table, on_delete):
            return self._checked().delete()
        return len(self._written("rows", lambda q: [(doc, None) for doc in q._deleted()], on_delete))

    def _updated(self, fields: dict, n: int, op: str) -> list[dict]:
        """The documents ``update_one`` (``op`` ``=``) and ``update_n`` (``IN``) return, as
        written."""
        fields = _write_rules.stamped(self._model_cls, fields)
        if not _write_rules.row_rules(self._model_cls):
            return self._checked("update")._update_picked(fields, n, op)
        return [after for _, after in self._written("update", lambda q: q._changed(fields, n))]

    def _written(self, use: str, write, on_delete: Optional[str] = None) -> list:
        """``write(query)``, what ``on_delete`` does about the rows that refer to the rows it
        deletes, and the rules of the class's audit log, as one unit (``use``: as
        :meth:`_checked` takes it)."""
        query = self._checked(use)
        return _write_rules.set_write(
            self._model_cls, self._db, query._table, lambda: write(query), on_delete
        )

    def offset(self, n: int) -> "SQLerQuerySet[T]":
        """Return a new queryset with an OFFSET clause."""
        return self._copy(self._query.offset(n))

    # inspection
    def sql(self) -> str:
        """Return the underlying SELECT SQL string."""
        return self._query.sql

    def params(self) -> list[Any]:
        """Return the underlying parameter list."""
        return self._query.params

    # debug helpers passthrough
    def debug(self) -> tuple[str, list[Any]]:
        return self._query.debug()

    def explain(self, adapter) -> list[tuple]:
        return self._query.explain(adapter)

    def explain_query_plan(self, adapter) -> list[tuple]:
        return self._query.explain_query_plan(adapter)

    # --- internal: the models of the rows read ---
    def _load(self, rows: list[dict]) -> list[T]:
        """The models of ``rows``, references followed unless ``resolve(False)`` (see
        ``_refs.load``)."""
        if not self._resolve:
            return self._models(rows)
        adapter = self._query._adapter  # type: ignore[attr-defined]
        if adapter is None:
            raise RuntimeError("QuerySet has no adapter bound")
        walk = _refs.load(self._model_cls, rows, self._db, self._query._table)
        return _refs.run(walk, adapter)
