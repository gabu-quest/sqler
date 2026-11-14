from __future__ import annotations

import logging
from typing import Any, Generic, Iterator, Optional, Type, TypeVar

from sqler.models import _refs
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
        docs = self._query.all_dicts()
        if self._resolve:
            try:
                docs = self._batch_resolve(docs)
            except RecursionError:
                # Circular reference detected - log and continue with unresolved refs.
                # We tolerate this specifically because resolution is bounded-depth and
                # the partial result is still useful. Any other exception indicates a
                # real bug in resolution and is re-raised so callers see it.
                logger.warning(
                    f"Circular reference detected during batch resolution for "
                    f"{self._model_cls.__name__}. Returning partially resolved documents."
                )
        results: list[T] = []
        other_table = self._reads_other_table()
        for d in docs:
            inst = self._model_cls.model_validate(d)  # type: ignore[attr-defined]
            # attach db id if present but excluded from schema
            self._attach_metadata(inst, d, other_table)
            results.append(inst)
        return results

    def as_dicts(self) -> list[dict[str, Any]]:
        """Execute and return raw dicts without Pydantic hydration.

        Returns dicts with ``_id`` attached. Respects filters, ordering,
        limits, and ``.select()`` field projection — but skips model
        validation, type coercion, computed fields, and relation resolution.
        """
        return self._query.all_dicts()

    def iter(self) -> Iterator[T]:
        """Yield model instances one at a time (memory-efficient streaming).

        Unlike ``all()`` which loads all rows into memory and resolves
        relations in batch, this method streams row by row. Relation
        resolution is NOT performed during streaming.

        Yields:
            T: Model instances.
        """
        other_table = self._reads_other_table()
        for doc in self._query.iter_dicts():
            inst = self._model_cls.model_validate(doc)  # type: ignore[attr-defined]
            self._attach_metadata(inst, doc, other_table)
            yield inst

    def first(self) -> Optional[T]:
        """Execute with LIMIT 1 and return the first model instance, if any."""
        d = self._query.first_dict()
        if d is None:
            return None
        if self._resolve:
            try:
                d = self._batch_resolve([d])[0]
            except RecursionError:
                # See note in all() — bounded-depth circular refs are tolerated;
                # any other resolution error is a real bug and re-raises.
                logger.warning(
                    f"Circular reference detected during resolution for "
                    f"{self._model_cls.__name__}. Returning partially resolved document."
                )
        inst = self._model_cls.model_validate(d)  # type: ignore[attr-defined]
        self._attach_metadata(inst, d, self._reads_other_table())
        return inst

    def _reads_other_table(self) -> bool:
        """True when this queryset reads a table other than its class's own.

        Each run that makes instances asks once and hands the answer to ``_attach_metadata``:
        the class lookup behind it is slow on a pydantic class, and the class may have been
        rebound since the queryset was built.
        """
        table = self._query._table
        return table is not None and table != _unchecked_table_name(self._model_cls)

    def _attach_metadata(self, inst: T, doc: dict, other_table: bool) -> None:
        """Attach database metadata (_id, _version, _snapshot) to an instance.

        Args:
            inst: The model instance to attach metadata to.
            doc: The document dictionary containing the metadata.
            other_table: The run's ``_reads_other_table()`` answer.
        """
        try:
            inst._id = doc.get("_id")  # type: ignore[attr-defined]
            if "_version" in doc:
                inst._version = doc.get("_version")  # type: ignore[attr-defined]
            # capture snapshot of loaded state (excluding private keys)
            snap = {k: v for k, v in doc.items() if k not in {"_id", "_version"}}
            inst._snapshot = snap  # type: ignore[attr-defined]
            if other_table:
                inst._source_table = self._query._table  # type: ignore[attr-defined]
        except AttributeError as e:
            # This indicates a model configuration issue - worth logging
            logger.debug(f"Could not attach metadata to {self._model_cls.__name__}: {e}")

    def count(self) -> int:
        """Return the count of matching rows."""
        return self._query.count()

    def sum(self, field: str) -> Optional[float]:
        """Return the sum of values for the specified field."""
        return self._query.sum(field)

    def avg(self, field: str) -> Optional[float]:
        """Return the average of values for the specified field."""
        return self._query.avg(field)

    def min(self, field: str) -> Optional[Any]:
        """Return the minimum value for the specified field."""
        return self._query.min(field)

    def max(self, field: str) -> Optional[Any]:
        """Return the maximum value for the specified field."""
        return self._query.max(field)

    def exists(self) -> bool:
        """Check if any rows match the query."""
        return self._query.exists()

    def distinct_values(self, field: str) -> list[Any]:
        """Return distinct values for a JSON field."""
        return self._query.distinct_values(field)

    def paginate(self, page: int, per_page: int = 20):
        """Return a paginated result set."""
        return self._query.paginate(page, per_page)

    def update(self, **fields) -> int:
        """Update matching rows with the given field values.

        Args:
            **fields: Field names and values to update in the JSON data.

        Returns:
            int: Number of rows updated.
        """
        return self._query.update(**fields)

    def update_one(self, **fields) -> Optional[T]:
        """Atomically update one matching row and return it as a model instance.

        Uses the query's WHERE and ORDER BY to select the row, then updates
        and returns it in a single atomic operation.

        Args:
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            Model instance or None if no row matched.
        """
        doc = self._query.update_one(**fields)
        if doc is None:
            return None
        inst = self._model_cls.model_validate(doc)  # type: ignore[attr-defined]
        self._attach_metadata(inst, doc, self._reads_other_table())
        return inst

    def update_n(self, n: int, **fields) -> list[T]:
        """Atomically update up to *n* matching rows and return them as model instances.

        Args:
            n: Maximum number of rows to update.
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            list: Model instances, or empty list if no rows matched.
        """
        docs = self._query.update_n(n, **fields)
        results: list[T] = []
        other_table = self._reads_other_table()
        for doc in docs:
            inst = self._model_cls.model_validate(doc)  # type: ignore[attr-defined]
            self._attach_metadata(inst, doc, other_table)
            results.append(inst)
        return results

    def delete_all(self) -> int:
        """Delete all matching rows (bulk delete).

        Note: This is named delete_all to avoid confusion with model.delete().
        It does NOT trigger referential integrity checks or hooks.

        Returns:
            int: Number of rows deleted.
        """
        return self._query.delete()

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

    # --- internal: batch resolve references to avoid N+1 ---
    def _batch_resolve(self, docs: list[dict], max_depth: int = 5) -> list[dict]:
        """Resolve the references of ``docs`` into stored documents (see ``_refs.batch``)."""
        adapter = self._query._adapter  # type: ignore[attr-defined]
        if adapter is None:
            raise RuntimeError("QuerySet has no adapter bound")
        walk = _refs.batch(docs, max_depth)
        try:
            want = next(walk)
            while True:
                want = walk.send(_refs.read_rows(adapter, *want, self._db))
        except StopIteration as done:
            return done.value[0]
