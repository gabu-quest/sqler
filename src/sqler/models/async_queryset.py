from __future__ import annotations

import logging
from typing import Any, Generic, Optional, Type, TypeVar

from sqler.models import _refs, _write_rules
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

    async def _checked(self) -> AsyncSQLerQuery:
        """The query, once its database has checked the table (``SchemaError`` when a promoted
        column the rows are read with would hide a value still in a document)."""
        if self._query._promoted_fields and self._db is not None:
            await self._db._columns_of(self._query._table)
        return self._query

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
        """Execute and return a list of model instances."""
        return await self._load(await (await self._checked()).all_dicts())

    async def as_dicts(self) -> list[dict[str, Any]]:
        """Execute and return raw dicts without Pydantic hydration.

        Returns dicts with ``_id`` attached. Respects filters, ordering,
        limits, and ``.select()`` field projection — but skips model
        validation, type coercion, computed fields, and relation resolution.
        """
        return await (await self._checked()).all_dicts()

    async def first(self) -> Optional[T]:
        row = await (await self._checked()).first_dict()
        if row is None:
            return None
        return (await self._load([row]))[0]

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

    async def count(self) -> int:
        return await (await self._checked()).count()

    async def sum(self, field: str) -> Optional[float]:
        """Return the sum of values for the specified field."""
        return await (await self._checked()).sum(field)

    async def avg(self, field: str) -> Optional[float]:
        """Return the average of values for the specified field."""
        return await (await self._checked()).avg(field)

    async def min(self, field: str) -> Optional[Any]:
        """Return the minimum value for the specified field."""
        return await (await self._checked()).min(field)

    async def max(self, field: str) -> Optional[Any]:
        """Return the maximum value for the specified field."""
        return await (await self._checked()).max(field)

    async def exists(self) -> bool:
        """Check if any rows match the query."""
        return await (await self._checked()).exists()

    async def paginate(self, page: int, per_page: int = 20):
        """Return a paginated result set."""
        return await (await self._checked()).paginate(page, per_page)

    async def distinct_values(self, field: str) -> list[Any]:
        """Return distinct values for a JSON field."""
        return await (await self._checked()).distinct_values(field)

    async def update(self, **fields) -> int:
        """Update matching rows with the given field values; the class's rules apply as in
        :meth:`SQLerQuerySet.update` (version, stamps, audit rows; no hook is called).

        Args:
            **fields: Field names and values to update in the JSON data.

        Returns:
            int: Number of rows updated.
        """
        fields = _write_rules.stamped(self._model_cls, fields)
        if not _write_rules.row_rules(self._model_cls):
            return await (await self._checked()).update(**fields)
        return len(await self._written(lambda q: q._changed(fields)))

    async def update_one(self, **fields) -> Optional[T]:
        """Atomically update one matching row and return it as a model instance; the
        class's rules apply as in :meth:`update`.

        Args:
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            Model instance or None if no row matched.
        """
        docs = await self._updated(fields, 1, "=")
        if not docs:
            return None
        return self._sourced(_refs.from_row(self._model_cls, docs[0], docs[0]), self._reads_other_table())

    async def update_n(self, n: int, **fields) -> list[T]:
        """Atomically update up to *n* matching rows and return them as model instances; the
        class's rules apply as in :meth:`update`.

        Args:
            n: Maximum number of rows to update.
            **fields: Field names and values (or F-expressions) to update.

        Returns:
            list: Model instances, or empty list if no rows matched.
        """
        docs = await self._updated(fields, n, "IN")
        other_table = self._reads_other_table()
        return [self._sourced(_refs.from_row(self._model_cls, doc, doc), other_table) for doc in docs]

    async def delete_all(self) -> int:
        """Delete all matching rows (bulk delete).

        Note: This is named delete_all to avoid confusion with model.delete().
        It runs no referential integrity check and calls no hook; an audit log gets a
        ``delete`` row per row deleted, as :meth:`SQLerQuerySet.delete_all` writes them.

        Returns:
            int: Number of rows deleted.
        """
        if not _write_rules.row_rules(self._model_cls):
            return await (await self._checked()).delete()
        return len(await self._written(self._deleted_rows))

    @staticmethod
    async def _deleted_rows(query: AsyncSQLerQuery) -> list:
        return [(doc, None) for doc in await query._deleted()]

    async def _updated(self, fields: dict, n: int, op: str) -> list[dict]:
        """The documents ``update_one`` (``op`` ``=``) and ``update_n`` (``IN``) return, as
        written."""
        fields = _write_rules.stamped(self._model_cls, fields)
        if not _write_rules.row_rules(self._model_cls):
            return await (await self._checked())._update_picked(fields, n, op)
        return [after for _, after in await self._written(lambda q: q._changed(fields, n))]

    async def _written(self, write) -> list:
        """``write(query)`` and the rules of the class's audit log, as one unit."""
        query = await self._checked()
        return await _write_rules.aset_write(self._model_cls, self._db, query._table, lambda: write(query))

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

    async def _load(self, rows: list[dict]) -> list[T]:
        """The models of ``rows``, references followed unless ``resolve(False)`` (see
        ``_refs.load``)."""
        other_table = self._reads_other_table()
        if not self._resolve:
            return [self._sourced(_refs.from_row(self._model_cls, row, row), other_table) for row in rows]
        adapter = self._query._adapter  # type: ignore[attr-defined]
        if adapter is None:
            raise RuntimeError("QuerySet has no adapter bound")
        walk = _refs.load(self._model_cls, rows, self._db, self._query._table)
        return [self._sourced(inst, other_table) for inst in await _refs.arun(walk, adapter)]
