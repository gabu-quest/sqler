from __future__ import annotations

import logging
from typing import Any, Generic, Iterator, Optional, Type, TypeVar

from sqler.models import _refs, _write_rules
from sqler.models._table_names import _unchecked_table_name
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
        adapter)."""
        self._model_cls = model_cls
        self._query = query
        self._db = db
        self._resolve = True

    def _copy(self, query: SQLerQuery) -> "SQLerQuerySet[T]":
        """A queryset on ``query`` holding everything this one holds but its query."""
        clone = self.__class__(self._model_cls, query, self._db)
        clone._resolve = self._resolve
        return clone

    def _checked(self) -> SQLerQuery:
        """The query, once its database has checked the table (``SchemaError`` when a promoted
        column the rows are read with would hide a value still in a document)."""
        if self._query._promoted_fields and self._db is not None:
            self._db._columns_of(self._query._table)
        return self._query

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
        return self._load(self._checked().all_dicts())

    def as_dicts(self) -> list[dict[str, Any]]:
        """Execute and return raw dicts without Pydantic hydration.

        Returns dicts with ``_id`` attached. Respects filters, ordering,
        limits, and ``.select()`` field projection — but skips model
        validation, type coercion, computed fields, and relation resolution.
        """
        return self._checked().all_dicts()

    def iter(self) -> Iterator[T]:
        """Yield model instances one at a time (memory-efficient streaming).

        Unlike ``all()`` which loads all rows into memory and resolves
        relations in batch, this method streams row by row. Relation
        resolution is NOT performed during streaming. On a plain ``":memory:"``
        database the rows are read whole first and held while they are yielded.

        Yields:
            T: Model instances.
        """
        other_table = self._reads_other_table()
        for doc in self._checked().iter_dicts():
            inst = self._sourced(_refs.from_row(self._model_cls, doc, doc), other_table)
            yield inst

    def first(self) -> Optional[T]:
        """Execute with LIMIT 1 and return the first model instance, if any."""
        row = self._checked().first_dict()
        if row is None:
            return None
        return (self._load([row]))[0]

    def _reads_other_table(self) -> bool:
        """True when this queryset reads a table other than its class's own.

        Each run that makes instances asks once and hands the answer to ``_sourced``:
        the class lookup behind it is slow on a pydantic class, and the class may have been
        rebound since the queryset was built.
        """
        table = self._query._table
        return table is not None and table != _unchecked_table_name(self._model_cls)

    def _sourced(self, inst: T, other_table: bool) -> T:
        """``inst``, a model of a row this queryset read, knowing the table it was read from
        when that is not its class's own (``other_table``: the run's
        ``_reads_other_table()`` answer)."""
        if other_table:
            _refs._set(inst, "_source_table", self._query._table)
        return inst

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
        return self._checked().paginate(page, per_page)

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
            return self._checked().update(**fields)
        return len(self._written(lambda q: q._changed(fields)))

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
        return self._sourced(_refs.from_row(self._model_cls, docs[0], docs[0]), self._reads_other_table())

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
        other_table = self._reads_other_table()
        return [self._sourced(_refs.from_row(self._model_cls, doc, doc), other_table) for doc in docs]

    def delete_all(self) -> int:
        """Delete all matching rows (bulk delete).

        Note: This is named delete_all to avoid confusion with model.delete().
        It runs no referential integrity check (a row that refers to a deleted one keeps
        its reference) and calls no hook. An audit log gets a ``delete`` row per row
        deleted, with the row as it was stored, in one transaction with the delete.

        Returns:
            int: Number of rows deleted.
        """
        if not _write_rules.row_rules(self._model_cls):
            return self._checked().delete()
        return len(self._written(lambda q: [(doc, None) for doc in q._deleted()]))

    def _updated(self, fields: dict, n: int, op: str) -> list[dict]:
        """The documents ``update_one`` (``op`` ``=``) and ``update_n`` (``IN``) return, as
        written."""
        fields = _write_rules.stamped(self._model_cls, fields)
        if not _write_rules.row_rules(self._model_cls):
            return self._checked()._update_picked(fields, n, op)
        return [after for _, after in self._written(lambda q: q._changed(fields, n))]

    def _written(self, write) -> list:
        """``write(query)`` and the rules of the class's audit log, as one unit."""
        query = self._checked()
        return _write_rules.set_write(self._model_cls, self._db, query._table, lambda: write(query))

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
        other_table = self._reads_other_table()
        if not self._resolve:
            return [self._sourced(_refs.from_row(self._model_cls, row, row), other_table) for row in rows]
        adapter = self._query._adapter  # type: ignore[attr-defined]
        if adapter is None:
            raise RuntimeError("QuerySet has no adapter bound")
        walk = _refs.load(self._model_cls, rows, self._db, self._query._table)
        return [self._sourced(inst, other_table) for inst in _refs.run(walk, adapter)]
