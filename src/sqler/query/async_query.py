import re
from typing import Any, Optional, Self

from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.exceptions import NoAdapterError
from sqler.query.expression import SQLerExpression
from sqler.query.query import PaginatedResult, _rewrite_promoted_refs


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
        order_fields: Optional[list[tuple[str, bool]]] = None,
        promoted_fields: Optional[list[str]] = None,
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
        self._order_fields = order_fields
        self._promoted_fields = promoted_fields or []

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
            order_fields=kwargs.get("order_fields", self._order_fields),
            promoted_fields=kwargs.get("promoted_fields", self._promoted_fields),
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

    def order_by(self, *fields: str, desc: bool = False) -> Self:
        """Return a new query ordered by the given JSON field(s).

        Supports Django-style ``-`` prefix for descending order::

            order_by("-priority", "eta", "ulid")
        """
        if not fields:
            return self._clone()
        parsed: list[tuple[str, bool]] = []
        for f in fields:
            if f.startswith("-"):
                parsed.append((f[1:], True))
            else:
                parsed.append((f, False))
        if len(parsed) == 1 and desc and not fields[0].startswith("-"):
            parsed[0] = (parsed[0][0], True)
        return self._clone(order=None, desc=False, order_fields=parsed)

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
        if self._order_fields:
            parts = []
            for field_name, is_desc in self._order_fields:
                part = f"json_extract(data, '$.{field_name}')"
                if is_desc:
                    part += " DESC"
                parts.append(part)
            order = "ORDER BY " + ", ".join(parts)
        elif self._order:
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
            if self._promoted_fields:
                select += ", " + ", ".join(self._promoted_fields)
        else:
            select = "data"
        sql = f"SELECT {distinct_kw}{select} FROM {self._table} {where} {order} {limit_offset}".strip()
        sql = " ".join(sql.split())
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)
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
        elif self._promoted_fields and field in self._promoted_fields:
            select = f"{func}({field})"
        else:
            select = f"{func}(json_extract(data, '$.{field}'))"
        sql = f"SELECT {select} FROM {self._table} {where}".strip()
        sql = " ".join(sql.split())
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)
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
        await self._adapter.auto_commit()
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
        await self._adapter.auto_commit()
        return rows

    async def all(self) -> list[str]:
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_query()
        cur = await self._adapter.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        await self._adapter.auto_commit()
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
        await self._adapter.auto_commit()
        return int(row[0]) if row else 0

    async def sum(self, field: str) -> Optional[float]:
        """Return the sum of values for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("SUM", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
        return float(row[0]) if row and row[0] is not None else None

    async def avg(self, field: str) -> Optional[float]:
        """Return the average of values for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("AVG", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
        return float(row[0]) if row and row[0] is not None else None

    async def min(self, field: str) -> Optional[Any]:
        """Return the minimum value for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("MIN", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
        return row[0] if row else None

    async def max(self, field: str) -> Optional[Any]:
        """Return the maximum value for the specified field."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("MAX", field)
        cur = await self._adapter.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
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
        await self._adapter.auto_commit()
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
        await self._adapter.auto_commit()
        ver_offset = 1 if self._include_version else 0
        promoted_start = 2 + ver_offset

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

            # Merge promoted column values
            if self._promoted_fields:
                for i, col_name in enumerate(self._promoted_fields):
                    idx = promoted_start + i
                    if idx < len(row):
                        obj[col_name] = row[idx]

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

        Supports F-expressions for atomic operations::

            await query.update(count=F("count") + 1)

        Args:
            **fields: Field names and values (or SQLerUpdateExpression) to update.

        Returns:
            int: Number of rows updated.
        """
        from sqler.query.field import SQLerUpdateExpression

        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if not fields:
            raise ValueError("No fields to update")

        import json

        promoted_set: list[str] = []
        promoted_params: list[Any] = []
        json_set_parts: list[str] = []
        json_set_params: list[Any] = []

        for field, value in fields.items():
            is_promoted = field in self._promoted_fields
            if isinstance(value, SQLerUpdateExpression):
                expr_sql = value.sql_expr
                if self._promoted_fields:
                    expr_sql = _rewrite_promoted_refs(expr_sql, self._promoted_fields)
                if is_promoted:
                    promoted_set.append(f"{field} = {expr_sql}")
                    promoted_params.extend(value.params)
                else:
                    json_set_parts.append(f"'$.{field}', {expr_sql}")
                    json_set_params.extend(value.params)
            else:
                serialized = json.dumps(value) if isinstance(value, (dict, list)) else value
                if is_promoted:
                    promoted_set.append(f"{field} = ?")
                    promoted_params.append(serialized)
                else:
                    json_set_parts.append(f"'$.{field}', ?")
                    json_set_params.append(serialized)

        set_clauses: list[str] = []
        all_set_params: list[Any] = []

        if json_set_parts:
            set_clauses.append(f"data = json_set(data, {', '.join(json_set_parts)})")
            all_set_params.extend(json_set_params)
        if promoted_set:
            set_clauses.extend(promoted_set)
            all_set_params.extend(promoted_params)

        where = f"WHERE {self._expression.sql}" if self._expression else ""
        where_params = self._expression.params if self._expression else []
        if self._promoted_fields and where:
            where = _rewrite_promoted_refs(where, self._promoted_fields)

        sql = f"UPDATE {self._table} SET {', '.join(set_clauses)} {where}".strip()
        sql = " ".join(sql.split())

        cur = await self._adapter.execute(sql, all_set_params + where_params)
        rowcount = cur.rowcount
        await cur.close()
        await self._adapter.auto_commit()
        return rowcount if rowcount >= 0 else 0

    async def update_one(self, **fields) -> Optional[dict[str, Any]]:
        """Atomically update one matching row and return it (async).

        Uses ``UPDATE ... WHERE _id = (SELECT _id ... LIMIT 1) RETURNING ...``
        for race-free claiming.

        Args:
            **fields: Field names and values (or SQLerUpdateExpression) to update.

        Returns:
            dict | None: The updated row as a dict, or None if no row matched.
        """
        from sqler.query.field import SQLerUpdateExpression

        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if not fields:
            raise ValueError("No fields to update")

        import json

        promoted_set: list[str] = []
        promoted_params: list[Any] = []
        json_set_parts: list[str] = []
        json_set_params: list[Any] = []

        for field, value in fields.items():
            is_promoted = field in self._promoted_fields
            if isinstance(value, SQLerUpdateExpression):
                expr_sql = value.sql_expr
                if self._promoted_fields:
                    expr_sql = _rewrite_promoted_refs(expr_sql, self._promoted_fields)
                if is_promoted:
                    promoted_set.append(f"{field} = {expr_sql}")
                    promoted_params.extend(value.params)
                else:
                    json_set_parts.append(f"'$.{field}', {expr_sql}")
                    json_set_params.extend(value.params)
            else:
                serialized = json.dumps(value) if isinstance(value, (dict, list)) else value
                if is_promoted:
                    promoted_set.append(f"{field} = ?")
                    promoted_params.append(serialized)
                else:
                    json_set_parts.append(f"'$.{field}', ?")
                    json_set_params.append(serialized)

        set_clauses: list[str] = []
        all_set_params: list[Any] = []

        if json_set_parts:
            set_clauses.append(f"data = json_set(data, {', '.join(json_set_parts)})")
            all_set_params.extend(json_set_params)
        if promoted_set:
            set_clauses.extend(promoted_set)
            all_set_params.extend(promoted_params)

        if self._include_version:
            set_clauses.append("_version = _version + 1")

        # Build inner SELECT
        inner_where = f"WHERE {self._expression.sql}" if self._expression else ""
        inner_where_params = self._expression.params if self._expression else []

        inner_order = ""
        if self._order_fields:
            parts = []
            for field_name, is_desc in self._order_fields:
                part = f"json_extract(data, '$.{field_name}')"
                if is_desc:
                    part += " DESC"
                parts.append(part)
            inner_order = "ORDER BY " + ", ".join(parts)
        elif self._order:
            inner_order = f"ORDER BY json_extract(data, '$.{self._order}')" + (
                " DESC" if self._desc else ""
            )

        if self._promoted_fields:
            if inner_where:
                inner_where = _rewrite_promoted_refs(inner_where, self._promoted_fields)
            if inner_order:
                inner_order = _rewrite_promoted_refs(inner_order, self._promoted_fields)

        inner_select = f"SELECT _id FROM {self._table} {inner_where} {inner_order} LIMIT 1"
        inner_select = " ".join(inner_select.split())

        returning_cols = ["_id", "data"]
        if self._include_version:
            returning_cols.append("_version")
        if self._promoted_fields:
            returning_cols.extend(self._promoted_fields)

        sql = (
            f"UPDATE {self._table} SET {', '.join(set_clauses)} "
            f"WHERE _id = ({inner_select}) "
            f"RETURNING {', '.join(returning_cols)}"
        )
        sql = " ".join(sql.split())

        all_params = all_set_params + inner_where_params
        cur = await self._adapter.execute(sql, all_params)
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()

        if row is None:
            return None

        obj = json.loads(row[1])
        obj["_id"] = row[0]

        col_offset = 2
        if self._include_version:
            obj["_version"] = row[col_offset]
            col_offset += 1
        if self._promoted_fields:
            for i, col_name in enumerate(self._promoted_fields):
                obj[col_name] = row[col_offset + i]

        return obj

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
        rowcount = cur.rowcount
        await cur.close()
        await self._adapter.auto_commit()
        return rowcount if rowcount >= 0 else 0
