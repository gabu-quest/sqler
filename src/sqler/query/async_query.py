from typing import Any, Optional, Self

from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.exceptions import NoAdapterError
from sqler.query.expression import SQLerExpression
from sqler.query.query import PaginatedResult


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
        offset: Optional[int] = None,
        include_version: bool = False,
        select_fields: Optional[list[str]] = None,
        distinct: bool = False,
    ):
        self._table = table
        self._adapter = adapter
        self._expression = expression
        self._order = order
        self._desc = desc
        self._limit = limit
        self._offset = offset
        self._include_version = include_version
        self._select_fields = select_fields
        self._distinct = distinct

    def _clone(self, **kwargs) -> Self:
        """Create a clone with optional overrides."""
        return self.__class__(
            table=kwargs.get("table", self._table),
            adapter=kwargs.get("adapter", self._adapter),
            expression=kwargs.get("expression", self._expression),
            order=kwargs.get("order", self._order),
            desc=kwargs.get("desc", self._desc),
            limit=kwargs.get("limit", self._limit),
            offset=kwargs.get("offset", self._offset),
            include_version=kwargs.get("include_version", self._include_version),
            select_fields=kwargs.get("select_fields", self._select_fields),
            distinct=kwargs.get("distinct", self._distinct),
        )

    def filter(self, expression: SQLerExpression) -> Self:
        new_expression = expression if self._expression is None else (self._expression & expression)
        return self._clone(expression=new_expression)

    def exclude(self, expression: SQLerExpression) -> Self:
        not_expr = ~expression
        new_expression = not_expr if self._expression is None else (self._expression & not_expr)
        return self._clone(expression=new_expression)

    def or_filter(self, expression: SQLerExpression) -> Self:
        """Return a new query with the expression OR-ed in."""
        new_expression = expression if self._expression is None else (self._expression | expression)
        return self._clone(expression=new_expression)

    def distinct(self) -> Self:
        """Return a new query with DISTINCT keyword."""
        return self._clone(distinct=True)

    def order_by(self, field: str, desc: bool = False) -> Self:
        return self._clone(order=field, desc=desc)

    def limit(self, n: int) -> Self:
        return self._clone(limit=n)

    def offset(self, n: int) -> Self:
        """Return a new query with an OFFSET clause.

        Args:
            n: Number of rows to skip.

        Returns:
            AsyncSQLerQuery: New query instance.
        """
        return self._clone(offset=n)

    def select(self, *fields: str) -> Self:
        """Return a new query that only retrieves specified fields.

        Args:
            *fields: Field names to retrieve from the JSON document.

        Returns:
            AsyncSQLerQuery: New query instance.
        """
        return self._clone(select_fields=list(fields))

    def with_version(self) -> Self:
        return self._clone(include_version=True)

    def _build_query(self, *, include_id: bool = False) -> tuple[str, list[Any]]:
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        order = ""
        if self._order:
            order = f"ORDER BY json_extract(data, '$.{self._order}')" + (
                " DESC" if self._desc else ""
            )
        limit_offset = ""
        if self._limit is not None:
            limit_offset = f"LIMIT {self._limit}"
            if self._offset is not None:
                limit_offset += f" OFFSET {self._offset}"
        elif self._offset is not None:
            limit_offset = f"LIMIT -1 OFFSET {self._offset}"

        distinct_kw = "DISTINCT " if self._distinct else ""
        if include_id:
            select = "_id, data" + (", _version" if self._include_version else "")
        else:
            select = "data"
        sql = f"SELECT {distinct_kw}{select} FROM {self._table} {where} {order} {limit_offset}".strip()
        sql = " ".join(sql.split())
        params = self._expression.params if self._expression else []
        return sql, params

    def _build_aggregate_query(
        self, func: str, field: Optional[str] = None
    ) -> tuple[str, list[Any]]:
        """Build an aggregate query (COUNT, SUM, AVG, MIN, MAX)."""
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        if field is None:
            select = f"{func}(*)"
        elif field == "_id":
            select = f"{func}(_id)"
        else:
            select = f"{func}(json_extract(data, '$.{field}'))"
        sql = f"SELECT {select} FROM {self._table} {where}".strip()
        sql = " ".join(sql.split())
        params = self._expression.params if self._expression else []
        return sql, params

    @property
    def sql(self) -> str:
        return self._build_query()[0]

    @property
    def params(self) -> list[Any]:
        return self._build_query()[1]

    def debug(self) -> tuple[str, list[Any]]:
        """Return (sql, params) for debugging."""
        return self._build_query()

    async def explain(self) -> list[tuple]:
        """Run EXPLAIN <sql> using the bound adapter; return raw rows.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            list[tuple]: Raw EXPLAIN output rows.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_query()
        cur = await self._adapter.execute(f"EXPLAIN {sql}", params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def explain_query_plan(self) -> list[tuple]:
        """Run EXPLAIN QUERY PLAN <sql>; return raw rows.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            list[tuple]: Raw EXPLAIN QUERY PLAN output rows.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_query()
        cur = await self._adapter.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def all(self) -> list[str]:
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_query()
        cur = await self._adapter.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [row[0] for row in rows]

    async def first(self) -> Optional[str]:
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        limited = self.limit(1)
        res = await limited.all()
        return res[0] if res else None

    async def count(self) -> int:
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("COUNT")
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0

    async def sum(self, field: str) -> Optional[float]:
        """Return the sum of values for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("SUM", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return float(row[0]) if row and row[0] is not None else None

    async def avg(self, field: str) -> Optional[float]:
        """Return the average of values for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("AVG", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return float(row[0]) if row and row[0] is not None else None

    async def min(self, field: str) -> Optional[Any]:
        """Return the minimum value for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("MIN", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    async def max(self, field: str) -> Optional[Any]:
        """Return the maximum value for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("MAX", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    async def exists(self) -> bool:
        """Check if any rows match the query."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        return await self.limit(1).count() > 0

    async def distinct_values(self, field: str) -> list[Any]:
        """Return distinct values for a JSON field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        sql = f"SELECT DISTINCT json_extract(data, '$.{field}') FROM {self._table} {where}".strip()
        sql = " ".join(sql.split())
        params = self._expression.params if self._expression else []
        cur = await self._adapter.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [row[0] for row in rows if row[0] is not None]

    async def paginate(self, page: int, per_page: int = 20) -> PaginatedResult:
        """Return a paginated result set.

        Args:
            page: Page number (1-indexed).
            per_page: Number of items per page.

        Returns:
            PaginatedResult: Object with items, pagination info, and helpers.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if page < 1:
            raise ValueError("Page must be >= 1")
        if per_page < 1:
            raise ValueError("per_page must be >= 1")

        total = await self.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 0
        offset = (page - 1) * per_page
        items = await self.offset(offset).limit(per_page).all_dicts()

        return PaginatedResult(
            items=items,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
        )

    async def all_dicts(self) -> list[dict[str, Any]]:
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
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
            except (IndexError, TypeError) as e:
                import warnings

                warnings.warn(f"Skipping malformed row in {self._table}: {e}", RuntimeWarning)
                continue
            obj = json.loads(data_json)

            # Apply field selection if specified
            if self._select_fields:
                obj = {k: obj.get(k) for k in self._select_fields if k in obj}

            obj["_id"] = _id
            if ver is not None:
                obj["_version"] = ver
            docs.append(obj)
        return docs

    async def first_dict(self) -> Optional[dict[str, Any]]:
        res = await self.limit(1).all_dicts()
        return res[0] if res else None

    async def update(self, **fields) -> int:
        """Update matching rows with the given field values.

        Args:
            **fields: Field names and values to update in the JSON data.

        Returns:
            int: Number of rows updated.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if not fields:
            raise ValueError("No fields to update")

        import json

        # Build SET clause using json_set
        set_parts = []
        set_params = []
        for field, value in fields.items():
            set_parts.append(f"'$.{field}', ?")
            set_params.append(json.dumps(value) if isinstance(value, (dict, list)) else value)

        set_clause = ", ".join(set_parts)
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        where_params = self._expression.params if self._expression else []

        sql = f"UPDATE {self._table} SET data = json_set(data, {set_clause}) {where}".strip()
        sql = " ".join(sql.split())

        cur = await self._adapter.execute(sql, set_params + where_params)
        await self._adapter.commit()

        # Check changes() to get rowcount
        ch = await self._adapter.execute("SELECT changes();")
        row = await ch.fetchone()
        await ch.close()
        await cur.close()
        return int(row[0]) if row else 0

    async def delete(self) -> int:
        """Delete all matching rows.

        Returns:
            int: Number of rows deleted.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")

        where = f"WHERE {self._expression.sql}" if self._expression else ""
        params = self._expression.params if self._expression else []

        sql = f"DELETE FROM {self._table} {where}".strip()
        sql = " ".join(sql.split())

        cur = await self._adapter.execute(sql, params)
        await self._adapter.commit()

        # Check changes() to get rowcount
        ch = await self._adapter.execute("SELECT changes();")
        row = await ch.fetchone()
        await ch.close()
        await cur.close()
        return int(row[0]) if row else 0
