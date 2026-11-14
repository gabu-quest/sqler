from __future__ import annotations

import logging
from typing import Any, Generic, Optional, Type, TypeVar

from sqler.models import _refs
from sqler.models._table_names import _unchecked_table_name
from sqler.query import SQLerExpression
from sqler.query.async_query import AsyncSQLerQuery

T = TypeVar("T")

logger = logging.getLogger("sqler.async_queryset")


class AsyncSQLerQuerySet(Generic[T]):
    """Async queryset that materializes model instances."""

    def __init__(self, model_cls: Type[T], query: AsyncSQLerQuery, db: Any = None) -> None:
        """``db``: the database ``query`` reads, which reads the rows references point at
        (optional: without it they are read as ``_id`` and ``data`` through the query's
        adapter)."""
        self._model_cls = model_cls
        self._query = query
        self._db = db
        self._resolve = True

    def _copy(self, query: AsyncSQLerQuery) -> "AsyncSQLerQuerySet[T]":
        """A queryset on ``query`` holding everything this one holds but its query."""
        clone = self.__class__(self._model_cls, query, self._db)
        clone._resolve = self._resolve
        return clone

    def resolve(self, flag: bool) -> "AsyncSQLerQuerySet[T]":
        """Toggle relation hydration on result materialization (default True)."""
        clone = self._copy(self._query)
        clone._resolve = flag
        return clone

    # chaining
    def filter(self, expression: SQLerExpression) -> "AsyncSQLerQuerySet[T]":
        return self._copy(self._query.filter(expression))

    def exclude(self, expression: SQLerExpression) -> "AsyncSQLerQuerySet[T]":
        return self._copy(self._query.exclude(expression))

    def or_filter(self, expression: SQLerExpression) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset with the expression OR-ed in."""
        return self._copy(self._query.or_filter(expression))

    def distinct(self) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset with DISTINCT results."""
        return self._copy(self._query.distinct())

    def select(self, *fields: str) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset that only retrieves specified fields."""
        return self._copy(self._query.select(*fields))

    def order_by(self, *fields: str, desc: bool = False) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset ordered by the given JSON field(s)."""
        return self._copy(self._query.order_by(*fields, desc=desc))

    def limit(self, n: int) -> "AsyncSQLerQuerySet[T]":
        return self._copy(self._query.limit(n))

    def group_by(self, *fields: str) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset grouped by the given field(s)."""
        return self._copy(self._query.group_by(*fields))

    def having(self, expression: SQLerExpression) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset with a HAVING clause (requires group_by)."""
        return self._copy(self._query.having(expression))

    # execution
    async def all(self) -> list[T]:
        docs = await self._query.all_dicts()
        per_row = self._resolve
        if self._resolve:
            try:
                docs, per_row = await self._abatch(docs)
            except RecursionError:
                logger.warning(
                    f"Circular reference detected during async batch resolution for "
                    f"{self._model_cls.__name__}. Returning partially resolved documents."
                )
            except Exception as e:
                logger.warning(
                    f"Error during async batch resolution for {self._model_cls.__name__}: "
                    f"{type(e).__name__}: {e}. Continuing with unresolved references."
                )
        results: list[T] = []
        other_table = self._reads_other_table()
        for d in docs:
            if per_row:
                try:
                    aresolver = getattr(self._model_cls, "_aresolve_relations", None)
                    if aresolver is not None:
                        d = await aresolver(d)  # type: ignore[assignment]
                except RecursionError:
                    logger.debug(
                        f"Circular reference during individual resolution for "
                        f"{self._model_cls.__name__}"
                    )
                except Exception as e:
                    logger.debug(
                        f"Individual resolution error for {self._model_cls.__name__}: "
                        f"{type(e).__name__}: {e}"
                    )
            inst = self._model_cls.model_validate(d)  # type: ignore[attr-defined]
            self._attach_metadata(inst, d, other_table)
            results.append(inst)
        return results

    async def as_dicts(self) -> list[dict[str, Any]]:
        """Execute and return raw dicts without Pydantic hydration.

        Returns dicts with ``_id`` attached. Respects filters, ordering,
        limits, and ``.select()`` field projection — but skips model
        validation, type coercion, computed fields, and relation resolution.
        """
        return await self._query.all_dicts()

    async def first(self) -> Optional[T]:
        d = await self._query.first_dict()
        if d is None:
            return None
        if self._resolve:
            try:
                d = (await self._abatch_resolve([d]))[0]
            except RecursionError:
                logger.warning(
                    f"Circular reference detected during async resolution for "
                    f"{self._model_cls.__name__}. Returning partially resolved document."
                )
            except Exception as e:
                logger.warning(
                    f"Error during async resolution for {self._model_cls.__name__}: "
                    f"{type(e).__name__}: {e}. Continuing with unresolved references."
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
            snap = {k: v for k, v in doc.items() if k not in {"_id", "_version"}}
            inst._snapshot = snap  # type: ignore[attr-defined]
            if other_table:
                inst._source_table = self._query._table  # type: ignore[attr-defined]
        except AttributeError as e:
            logger.debug(f"Could not attach metadata to {self._model_cls.__name__}: {e}")

    async def count(self) -> int:
        return await self._query.count()

    async def sum(self, field: str) -> Optional[float]:
        """Return the sum of values for the specified field."""
        return await self._query.sum(field)

    async def avg(self, field: str) -> Optional[float]:
        """Return the average of values for the specified field."""
        return await self._query.avg(field)

    async def min(self, field: str) -> Optional[Any]:
        """Return the minimum value for the specified field."""
        return await self._query.min(field)

    async def max(self, field: str) -> Optional[Any]:
        """Return the maximum value for the specified field."""
        return await self._query.max(field)

    async def exists(self) -> bool:
        """Check if any rows match the query."""
        return await self._query.exists()

    async def paginate(self, page: int, per_page: int = 20):
        """Return a paginated result set."""
        return await self._query.paginate(page, per_page)

    async def distinct_values(self, field: str) -> list[Any]:
        """Return distinct values for a JSON field."""
        return await self._query.distinct_values(field)

    async def update(self, **fields) -> int:
        """Update matching rows with the given field values.

        Args:
            **fields: Field names and values to update in the JSON data.

        Returns:
            int: Number of rows updated.
        """
        return await self._query.update(**fields)

    async def update_one(self, **fields) -> Optional[T]:
        """Atomically update one matching row and return it as a model instance.

        Args:
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            Model instance or None if no row matched.
        """
        doc = await self._query.update_one(**fields)
        if doc is None:
            return None
        inst = self._model_cls.model_validate(doc)  # type: ignore[attr-defined]
        self._attach_metadata(inst, doc, self._reads_other_table())
        return inst

    async def update_n(self, n: int, **fields) -> list[T]:
        """Atomically update up to *n* matching rows and return them as model instances.

        Args:
            n: Maximum number of rows to update.
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            list: Model instances, or empty list if no rows matched.
        """
        docs = await self._query.update_n(n, **fields)
        results: list[T] = []
        other_table = self._reads_other_table()
        for doc in docs:
            inst = self._model_cls.model_validate(doc)  # type: ignore[attr-defined]
            self._attach_metadata(inst, doc, other_table)
            results.append(inst)
        return results

    async def delete_all(self) -> int:
        """Delete all matching rows (bulk delete).

        Note: This is named delete_all to avoid confusion with model.delete().
        It does NOT trigger referential integrity checks or hooks.

        Returns:
            int: Number of rows deleted.
        """
        return await self._query.delete()

    def offset(self, n: int) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset with an OFFSET clause."""
        return self._copy(self._query.offset(n))

    # inspection
    def sql(self) -> str:
        return self._query.sql

    def params(self) -> list[Any]:
        return self._query.params

    # debug helpers
    def debug(self) -> tuple[str, list[Any]]:
        return (self._query.sql, self._query.params)

    async def explain(self) -> list[tuple]:
        adapter = self._query._adapter  # type: ignore[attr-defined]
        assert adapter is not None, "Query has no adapter bound"
        cur = await adapter.execute(f"EXPLAIN {self._query.sql}", self._query.params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def explain_query_plan(self) -> list[tuple]:
        adapter = self._query._adapter  # type: ignore[attr-defined]
        assert adapter is not None, "Query has no adapter bound"
        cur = await adapter.execute(f"EXPLAIN QUERY PLAN {self._query.sql}", self._query.params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def _abatch_resolve(self, docs: list[dict], max_depth: int = 5) -> list[dict]:
        """Resolve the references of ``docs`` into stored documents (see ``_refs.batch``)."""
        return (await self._abatch(docs, max_depth))[0]

    async def _abatch(self, docs: list[dict], max_depth: int = 5) -> tuple[list[dict], bool]:
        """``(documents, found)`` of ``_refs.batch``: ``found`` is False when no document
        holds a reference."""
        adapter = self._query._adapter  # type: ignore[attr-defined]
        assert adapter is not None, "Query has no adapter bound"
        walk = _refs.batch(docs, max_depth, self._model_cls, self._db)
        try:
            want = next(walk)
            while True:
                want = walk.send(await _refs.aread_rows(adapter, *want, self._db))
        except StopIteration as done:
            return done.value
