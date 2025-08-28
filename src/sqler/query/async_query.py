from typing import Any, Optional, Self

from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.query.expression import SQLerExpression


class AsyncSQLerQuery:
    """Async query builder/executor mirroring SQLerQuery semantics."""

    def __init__(
        self,
        table: str,
        adapter: Optional[AsyncSQLiteAdapter] = None,
        expression: Optional[SQLerExpression] = None,
        order: Optional[str] = None,
        desc: bool = False,
        limit: Optional[int] = None,
        include_version: bool = False,
    ):
        self._table = table
        self._adapter = adapter
        self._expression = expression
        self._order = order
        self._desc = desc
        self._limit = limit
        self._include_version = include_version

    def filter(self, expression: SQLerExpression) -> Self:
        new_expression = expression if self._expression is None else (self._expression & expression)
        return self.__class__(
            self._table,
            self._adapter,
            new_expression,
            self._order,
            self._desc,
            self._limit,
            self._include_version,
        )

    def exclude(self, expression: SQLerExpression) -> Self:
        not_expr = ~expression
        new_expression = not_expr if self._expression is None else (self._expression & not_expr)
        return self.__class__(
            self._table,
            self._adapter,
            new_expression,
            self._order,
            self._desc,
            self._limit,
            self._include_version,
        )

    def order_by(self, field: str, desc: bool = False) -> Self:
        return self.__class__(
            self._table,
            self._adapter,
            self._expression,
            field,
            desc,
            self._limit,
            self._include_version,
        )

    def limit(self, n: int) -> Self:
        return self.__class__(
            self._table,
            self._adapter,
            self._expression,
            self._order,
            self._desc,
            n,
            self._include_version,
        )

    def with_version(self) -> Self:
        return self.__class__(
            self._table,
            self._adapter,
            self._expression,
            self._order,
            self._desc,
            self._limit,
            True,
        )

    def _build_query(self, *, include_id: bool = False) -> tuple[str, list[Any]]:
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        order = ""
        if self._order:
            order = f"ORDER BY json_extract(data, '$.{self._order}')" + (
                " DESC" if self._desc else ""
            )
        limit = f"LIMIT {self._limit}" if self._limit is not None else ""
        if include_id:
            select = "_id, data" + (", _version" if self._include_version else "")
        else:
            select = "data"
        sql = f"SELECT {select} FROM {self._table} {where} {order} {limit}".strip()
        sql = " ".join(sql.split())
        params = self._expression.params if self._expression else []
        return sql, params

    @property
    def sql(self) -> str:
        return self._build_query()[0]

    @property
    def params(self) -> list[Any]:
        return self._build_query()[1]

    async def all(self) -> list[str]:
        if self._adapter is None:
            raise ConnectionError("No adapter set for query")
        sql, params = self._build_query()
        cur = await self._adapter.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [row[0] for row in rows]

    async def first(self) -> Optional[str]:
        if self._adapter is None:
            raise ConnectionError("No adapter set for query")
        limited = self.limit(1)
        res = await limited.all()
        return res[0] if res else None

    async def count(self) -> int:
        if self._adapter is None:
            raise ConnectionError("No adapter set for query")
        sql, params = self._build_query()
        count_sql = sql.replace("SELECT data", "SELECT count(*)")
        cur = await self._adapter.execute(count_sql, params)
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0

    async def all_dicts(self) -> list[dict[str, Any]]:
        if self._adapter is None:
            raise ConnectionError("No adapter set for query")
        import json

        sql, params = self._build_query(include_id=True)
        cur = await self._adapter.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        docs: list[dict[str, Any]] = []
        for row in rows:
            try:
                _id, data_json = row[0], row[1]
                ver = row[2] if self._include_version and len(row) > 2 else None
            except Exception:
                continue
            obj = json.loads(data_json)
            obj["_id"] = _id
            if ver is not None:
                obj["_version"] = ver
            docs.append(obj)
        return docs

    async def first_dict(self) -> Optional[dict[str, Any]]:
        res = await self.limit(1).all_dicts()
        return res[0] if res else None
