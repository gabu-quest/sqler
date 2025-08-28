from __future__ import annotations

from typing import Any, Generic, Optional, Type, TypeVar

from sqler.query import SQLerExpression
from sqler.query.async_query import AsyncSQLerQuery

T = TypeVar("T")


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
            except Exception:
                pass
        results: list[T] = []
        for d in docs:
            if self._resolve:
                try:
                    aresolver = getattr(self._model_cls, "_aresolve_relations")
                    d = await aresolver(d)  # type: ignore[assignment]
                except Exception:
                    pass
            inst = self._model_cls.model_validate(d)  # type: ignore[attr-defined]
            try:
                inst._id = d.get("_id")  # type: ignore[attr-defined]
            except Exception:
                pass
            results.append(inst)
        return results

    async def first(self) -> Optional[T]:
        d = await self._query.first_dict()
        if d is None:
            return None
        if self._resolve:
            try:
                d = (await self._abatch_resolve([d]))[0]
            except Exception:
                pass
        inst = self._model_cls.model_validate(d)  # type: ignore[attr-defined]
        try:
            inst._id = d.get("_id")  # type: ignore[attr-defined]
        except Exception:
            pass
        return inst

    async def count(self) -> int:
        return await self._query.count()

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
        cur = await adapter.execute(f"EXPLAIN {self._query.sql}", self._query.params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def explain_query_plan(self) -> list[tuple]:
        adapter = self._query._adapter  # type: ignore[attr-defined]
        cur = await adapter.execute(f"EXPLAIN QUERY PLAN {self._query.sql}", self._query.params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def _abatch_resolve(self, docs: list[dict]) -> list[dict]:
        refs_by_table: dict[str, set[int]] = {}

        def collect(value):
            if isinstance(value, dict) and "_table" in value and "_id" in value:
                refs_by_table.setdefault(value["_table"], set()).add(int(value["_id"]))
            elif isinstance(value, dict):
                for v in value.values():
                    collect(v)
            elif isinstance(value, list):
                for v in value:
                    collect(v)

        for d in docs:
            collect(d)

        resolved: dict[tuple[str, int], dict] = {}
        adapter = self._query._adapter  # type: ignore[attr-defined]
        for table, ids in refs_by_table.items():
            if not ids:
                continue
            placeholders = ",".join(["?"] * len(ids))
            sql = f"SELECT _id, data FROM {table} WHERE _id IN ({placeholders})"
            cur = await adapter.execute(sql, list(ids))
            rows = await cur.fetchall()
            await cur.close()
            for _id, data_json in rows:
                import json

                obj = json.loads(data_json)
                obj["_id"] = _id
                resolved[(table, int(_id))] = obj

        visited: set[tuple[str, int]] = set()

        def replace(value):
            if isinstance(value, dict) and "_table" in value and "_id" in value:
                key = (value["_table"], int(value["_id"]))
                if key in visited:
                    return value
                visited.add(key)
                return resolved.get(key, value)
            if isinstance(value, dict):
                return {k: replace(v) for k, v in value.items()}
            if isinstance(value, list):
                return [replace(v) for v in value]
            return value

        return [replace(d) for d in docs]
