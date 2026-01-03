from __future__ import annotations

import logging
from typing import Any, Generic, Optional, Type, TypeVar

from sqler.query import SQLerExpression
from sqler.query.async_query import AsyncSQLerQuery

T = TypeVar("T")

logger = logging.getLogger("sqler.async_queryset")


class AsyncSQLerQuerySet(Generic[T]):
    """Async queryset that materializes model instances."""

    def __init__(self, model_cls: Type[T], query: AsyncSQLerQuery) -> None:
        self._model_cls = model_cls
        self._query = query
        self._resolve = True

    def resolve(self, flag: bool) -> "AsyncSQLerQuerySet[T]":
        clone = self.__class__(self._model_cls, self._query)
        clone._resolve = flag
        return clone

    # chaining
    def filter(self, expression: SQLerExpression) -> "AsyncSQLerQuerySet[T]":
        return self.__class__(self._model_cls, self._query.filter(expression))

    def exclude(self, expression: SQLerExpression) -> "AsyncSQLerQuerySet[T]":
        return self.__class__(self._model_cls, self._query.exclude(expression))

    def or_filter(self, expression: SQLerExpression) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset with the expression OR-ed in."""
        return self.__class__(self._model_cls, self._query.or_filter(expression))

    def distinct(self) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset with DISTINCT results."""
        return self.__class__(self._model_cls, self._query.distinct())

    def select(self, *fields: str) -> "AsyncSQLerQuerySet[T]":
        """Return a new queryset that only retrieves specified fields."""
        return self.__class__(self._model_cls, self._query.select(*fields))

    def order_by(self, field: str, desc: bool = False) -> "AsyncSQLerQuerySet[T]":
        return self.__class__(self._model_cls, self._query.order_by(field, desc))

    def limit(self, n: int) -> "AsyncSQLerQuerySet[T]":
        return self.__class__(self._model_cls, self._query.limit(n))

    # execution
    async def all(self) -> list[T]:
        docs = await self._query.all_dicts()
        if self._resolve:
            try:
                docs = await self._abatch_resolve(docs)
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
        for d in docs:
            if self._resolve:
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
            self._attach_metadata(inst, d)
            results.append(inst)
        return results

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
        self._attach_metadata(inst, d)
        return inst

    def _attach_metadata(self, inst: T, doc: dict) -> None:
        """Attach database metadata (_id, _version, _snapshot) to an instance.

        Args:
            inst: The model instance to attach metadata to.
            doc: The document dictionary containing the metadata.
        """
        try:
            inst._id = doc.get("_id")  # type: ignore[attr-defined]
            if "_version" in doc:
                inst._version = doc.get("_version")  # type: ignore[attr-defined]
            snap = {k: v for k, v in doc.items() if k not in {"_id", "_version"}}
            inst._snapshot = snap  # type: ignore[attr-defined]
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
        return self.__class__(self._model_cls, self._query.offset(n))

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
        """Recursively resolve all relationship references in batch (async).

        This method:
        1. Recursively collects ALL refs at all nesting depths
        2. Fetches them in batch (one query per table per depth level)
        3. Replaces refs with resolved data, recursively processing nested refs

        Args:
            docs: List of documents to resolve.
            max_depth: Maximum recursion depth to prevent infinite loops.

        Returns:
            list[dict]: Documents with references replaced by actual data.
        """
        if max_depth <= 0 or not docs:
            return docs

        import json

        adapter = self._query._adapter  # type: ignore[attr-defined]
        assert adapter is not None, "Query has no adapter bound"

        # Master dict to hold ALL resolved refs across all depths
        resolved: dict[tuple[str, int], dict] = {}

        async def collect_and_fetch(values: list, depth: int) -> None:
            """Recursively collect refs and fetch them, building up resolved dict."""
            if depth <= 0:
                return

            # Collect refs grouped by table
            refs_by_table: dict[str, set[int]] = {}

            def collect(value):
                if isinstance(value, dict) and "_table" in value and "_id" in value:
                    key = (value["_table"], int(value["_id"]))
                    if key not in resolved:  # Don't re-fetch already resolved
                        refs_by_table.setdefault(value["_table"], set()).add(int(value["_id"]))
                elif isinstance(value, dict):
                    for v in value.values():
                        collect(v)
                elif isinstance(value, list):
                    for v in value:
                        collect(v)

            for v in values:
                collect(v)

            if not refs_by_table:
                return

            # Fetch all refs and recurse into fetched docs
            nested_docs: list[dict] = []
            for table, ids in refs_by_table.items():
                if not ids:
                    continue
                placeholders = ",".join(["?"] * len(ids))
                sql = f"SELECT _id, data FROM {table} WHERE _id IN ({placeholders})"
                cur = await adapter.execute(sql, list(ids))
                rows = await cur.fetchall()
                await cur.close()
                for _id, data_json in rows:
                    obj = json.loads(data_json)
                    obj["_id"] = _id
                    resolved[(table, int(_id))] = obj
                    nested_docs.append(obj)

            # Recursively fetch nested refs
            if nested_docs:
                await collect_and_fetch(nested_docs, depth - 1)

        # Collect and fetch all refs at all depths into single resolved dict
        await collect_and_fetch(docs, max_depth)

        if not resolved:
            return docs

        # Replace refs with resolved data, recursively processing nested refs
        def make_replace():
            # Track refs currently being processed (stack) to detect cycles
            processing: set[tuple[str, int]] = set()

            def replace(value):
                if isinstance(value, dict) and "_table" in value and "_id" in value:
                    key = (value["_table"], int(value["_id"]))
                    if key in processing:
                        return value  # Circular ref - we're already processing this
                    fetched = resolved.get(key)
                    if fetched is None:
                        return value  # Not found, keep original ref
                    # Mark as processing, resolve, then unmark
                    processing.add(key)
                    result = {k: replace(v) for k, v in fetched.items()}
                    processing.discard(key)
                    return result
                if isinstance(value, dict):
                    return {k: replace(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [replace(v) for v in value]
                return value

            return replace

        return [make_replace()(d) for d in docs]
