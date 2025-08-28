from __future__ import annotations

from typing import Any, Generic, Optional, Type, TypeVar

from sqler.query import SQLerExpression, SQLerQuery

T = TypeVar("T")


class SQLerQuerySet(Generic[T]):
    """Query wrapper that materializes model instances.

    This class wraps a :class:`~sqler.query.query.SQLerQuery` and converts
    results into instances of the bound Pydantic model class.
    """

    def __init__(
        self,
        model_cls: Type[T],
        query: SQLerQuery,
    ) -> None:
        self._model_cls = model_cls
        self._query = query
        self._resolve = True

    def resolve(self, flag: bool) -> "SQLerQuerySet[T]":
        """Toggle relation hydration on result materialization (default True)."""
        clone = self.__class__(self._model_cls, self._query)
        clone._resolve = flag
        return clone

    # chaining returns new wrappers
    def filter(self, expression: SQLerExpression) -> "SQLerQuerySet[T]":
        """Return a new queryset filtered by the expression."""
        return self.__class__(self._model_cls, self._query.filter(expression))

    def exclude(self, expression: SQLerExpression) -> "SQLerQuerySet[T]":
        """Return a new queryset excluding rows matching the expression."""
        return self.__class__(self._model_cls, self._query.exclude(expression))

    def order_by(self, field: str, desc: bool = False) -> "SQLerQuerySet[T]":
        """Return a new queryset ordered by the given JSON field."""
        return self.__class__(self._model_cls, self._query.order_by(field, desc))

    def limit(self, n: int) -> "SQLerQuerySet[T]":
        """Return a new queryset with a LIMIT clause."""
        return self.__class__(self._model_cls, self._query.limit(n))

    # execution
    def all(self) -> list[T]:
        """Execute and return a list of model instances."""
        docs = self._query.all_dicts()
        if self._resolve:
            try:
                docs = self._batch_resolve(docs)
            except Exception:
                pass
        results: list[T] = []
        for d in docs:
            inst = self._model_cls.model_validate(d)  # type: ignore[attr-defined]
            # attach db id if present but excluded from schema
            try:
                inst._id = d.get("_id")  # type: ignore[attr-defined]
                if "_version" in d:
                    inst._version = d.get("_version")  # type: ignore[attr-defined]
                # capture snapshot of loaded state (excluding private keys)
                snap = {k: v for k, v in d.items() if k not in {"_id", "_version"}}
                inst._snapshot = snap  # type: ignore[attr-defined]
            except Exception:
                pass
            results.append(inst)
        return results

    def first(self) -> Optional[T]:
        """Execute with LIMIT 1 and return the first model instance, if any."""
        d = self._query.first_dict()
        if d is None:
            return None
        if self._resolve:
            try:
                d = self._batch_resolve([d])[0]
            except Exception:
                pass
        inst = self._model_cls.model_validate(d)  # type: ignore[attr-defined]
        try:
            inst._id = d.get("_id")  # type: ignore[attr-defined]
            if "_version" in d:
                inst._version = d.get("_version")  # type: ignore[attr-defined]
            snap = {k: v for k, v in d.items() if k not in {"_id", "_version"}}
            inst._snapshot = snap  # type: ignore[attr-defined]
        except Exception:
            pass
        return inst

    def count(self) -> int:
        """Return the count of matching rows."""
        return self._query.count()

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
    def _batch_resolve(self, docs: list[dict]) -> list[dict]:
        # collect refs grouped by table
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

        # fetch all refs per table
        resolved: dict[tuple[str, int], dict] = {}
        adapter = self._query._adapter  # type: ignore[attr-defined]
        for table, ids in refs_by_table.items():
            if not ids:
                continue
            placeholders = ",".join(["?"] * len(ids))
            sql = f"SELECT _id, data FROM {table} WHERE _id IN ({placeholders})"
            cur = adapter.execute(sql, list(ids))
            rows = cur.fetchall()
            for _id, data_json in rows:
                import json

                obj = json.loads(data_json)
                obj["_id"] = _id
                resolved[(table, int(_id))] = obj

        # replace in-doc refs with fetched payloads, per-document visited guard
        def make_replace():
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

            return replace

        return [make_replace()(d) for d in docs]
