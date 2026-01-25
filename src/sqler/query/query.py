from typing import Any, Optional, Self

from sqler.adapter.abstract import AdapterABC
from sqler.query import SQLerExpression


class QueryError(Exception):
    """Base exception for query errors."""

    pass


class NoAdapterError(ConnectionError):
    """Raised when attempting to execute operations without an adapter set."""

    pass


class InvariantViolationError(RuntimeError):
    """Raised when reading rows that violate expected invariants (e.g., NULL JSON)."""


class SQLerQuery:
    """Build and execute chainable queries against a table.

    Queries are immutable; chaining methods returns new query instances. By
    default, ``all()`` and ``first()`` return raw JSON strings from SQLite. Use
    ``all_dicts()`` and ``first_dict()`` to get parsed dicts with ``_id``.
    """

    def __init__(
        self,
        table: str,
        adapter: Optional[AdapterABC] = None,
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
        """Return a new query with the expression AND-ed in.

        Args:
            expression: Boolean expression to filter on.

        Returns:
            SQLerQuery: New query instance.
        """
        new_expression = expression if self._expression is None else (self._expression & expression)
        return self._clone(expression=new_expression)

    def exclude(self, expression: SQLerExpression) -> Self:
        """Return a new query with the NOT of expression AND-ed in.

        Args:
            expression: Boolean expression to negate and apply.

        Returns:
            SQLerQuery: New query instance.
        """
        not_expr = ~expression
        new_expression = not_expr if self._expression is None else (self._expression & not_expr)
        return self._clone(expression=new_expression)

    def or_filter(self, expression: SQLerExpression) -> Self:
        """Return a new query with the expression OR-ed in.

        Args:
            expression: Boolean expression to OR with existing filters.

        Returns:
            SQLerQuery: New query instance.
        """
        new_expression = expression if self._expression is None else (self._expression | expression)
        return self._clone(expression=new_expression)

    def distinct(self) -> Self:
        """Return a new query with DISTINCT keyword.

        Returns:
            SQLerQuery: New query instance with distinct results.
        """
        return self._clone(distinct=True)

    def order_by(self, field: str, desc: bool = False) -> Self:
        """Return a new query ordered by the given JSON field.

        Args:
            field: Dotted JSON path to sort by (e.g., ``"age"``).
            desc: Sort descending when True.

        Returns:
            SQLerQuery: New query instance.
        """
        return self._clone(order=field, desc=desc)

    def limit(self, n: int) -> Self:
        """Return a new query with a LIMIT clause.

        Args:
            n: Maximum number of rows to return.

        Returns:
            SQLerQuery: New query instance.
        """
        return self._clone(limit=n)

    def offset(self, n: int) -> Self:
        """Return a new query with an OFFSET clause.

        Args:
            n: Number of rows to skip.

        Returns:
            SQLerQuery: New query instance.
        """
        return self._clone(offset=n)

    def select(self, *fields: str) -> Self:
        """Return a new query that only retrieves specified fields.

        Args:
            *fields: Field names to retrieve from the JSON document.

        Returns:
            SQLerQuery: New query instance.
        """
        return self._clone(select_fields=list(fields))

    def with_version(self) -> Self:
        """Return a new query that includes `_version` column in results."""
        return self._clone(include_version=True)

    def _build_query(
        self, *, include_id: bool = False, include_version: bool = False
    ) -> tuple[str, list[Any]]:
        """Build the SELECT statement and parameters.

        Args:
            include_id: When True, select ``_id, data`` instead of only
                ``data``.

        Returns:
            tuple[str, list[Any]]: SQL string and parameter list.
        """
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
            # SQLite requires LIMIT with OFFSET, use -1 for unlimited
            limit_offset = f"LIMIT -1 OFFSET {self._offset}"

        distinct_kw = "DISTINCT " if self._distinct else ""
        if include_id:
            select = "_id, data" + (
                ", _version" if (include_version or self._include_version) else ""
            )
        else:
            select = "data"
        sql = f"SELECT {distinct_kw}{select} FROM {self._table} {where} {order} {limit_offset}".strip()
        sql = " ".join(sql.split())  # collapse double spaces
        params = self._expression.params if self._expression else []
        return sql, params

    def _build_aggregate_query(
        self, func: str, field: Optional[str] = None
    ) -> tuple[str, list[Any]]:
        """Build an aggregate query (COUNT, SUM, AVG, MIN, MAX).

        Args:
            func: Aggregate function name.
            field: Optional field to aggregate on.

        Returns:
            tuple[str, list[Any]]: SQL string and parameter list.
        """
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
        """Return the current SELECT SQL string."""
        return self._build_query()[0]

    @property
    def params(self) -> list[Any]:
        """Return the current parameter list."""
        return self._build_query()[1]

    def debug(self) -> tuple[str, list[Any]]:
        """Return (sql, params) for debugging."""
        return self._build_query()

    def explain(self, adapter) -> list[tuple]:
        """Run EXPLAIN <sql> using the provided adapter; return raw rows."""
        sql, params = self._build_query()
        cur = adapter.execute(f"EXPLAIN {sql}", params)
        return cur.fetchall()

    def explain_query_plan(self, adapter) -> list[tuple]:
        """Run EXPLAIN QUERY PLAN <sql>; return raw rows."""
        sql, params = self._build_query()
        cur = adapter.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        return cur.fetchall()

    def all(self) -> list[dict[str, Any]]:
        """Execute and return all matching rows as raw JSON strings.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            list[str]: JSON strings for each matching row (data column).
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_query()
        cur = self._adapter.execute(sql, params)
        return [row[0] for row in cur.fetchall()]

    def first(self) -> Optional[dict[str, Any]]:
        """Execute with ``LIMIT 1`` and return the first raw JSON string.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            str | None: JSON string for the first row, or ``None`` when empty.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        results = self.limit(1).all()
        return results[0] if results else None

    def count(self) -> int:
        """Return the count of matching rows.

        Raises:
            NoAdapterError: If the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("COUNT")
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def sum(self, field: str) -> Optional[float]:
        """Return the sum of values for the specified field.

        Args:
            field: JSON field path to sum.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            float | None: Sum of values, or None if no rows match.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("SUM", field)
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def avg(self, field: str) -> Optional[float]:
        """Return the average of values for the specified field.

        Args:
            field: JSON field path to average.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            float | None: Average of values, or None if no rows match.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("AVG", field)
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def min(self, field: str) -> Optional[Any]:
        """Return the minimum value for the specified field.

        Args:
            field: JSON field path to find minimum.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            Any | None: Minimum value, or None if no rows match.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("MIN", field)
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None

    def max(self, field: str) -> Optional[Any]:
        """Return the maximum value for the specified field.

        Args:
            field: JSON field path to find maximum.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            Any | None: Maximum value, or None if no rows match.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_aggregate_query("MAX", field)
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None

    def exists(self) -> bool:
        """Check if any rows match the query.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            bool: True if at least one row matches.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        return self.limit(1).count() > 0

    def distinct_values(self, field: str) -> list[Any]:
        """Return distinct values for a JSON field.

        Args:
            field: JSON field path to extract distinct values from.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            list[Any]: Distinct values for the field.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        sql = f"SELECT DISTINCT json_extract(data, '$.{field}') FROM {self._table} {where}".strip()
        sql = " ".join(sql.split())
        params = self._expression.params if self._expression else []
        cur = self._adapter.execute(sql, params)
        return [row[0] for row in cur.fetchall() if row[0] is not None]

    def paginate(self, page: int, per_page: int = 20) -> "PaginatedResult":
        """Return a paginated result set.

        Args:
            page: Page number (1-indexed).
            per_page: Number of items per page.

        Raises:
            NoAdapterError: If the query has no adapter.
            ValueError: If page < 1 or per_page < 1.

        Returns:
            PaginatedResult: Object with items, pagination info, and helpers.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if page < 1:
            raise ValueError("Page must be >= 1")
        if per_page < 1:
            raise ValueError("per_page must be >= 1")

        total = self.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 0
        offset = (page - 1) * per_page
        items = self.offset(offset).limit(per_page).all_dicts()

        return PaginatedResult(
            items=items,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
        )

    def all_dicts(self) -> list[dict[str, Any]]:
        """Execute and return parsed dicts with ``_id`` attached.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            list[dict[str, Any]]: One dict per row with ``_id`` included.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        import json

        sql, params = self._build_query(include_id=True, include_version=False)
        cur = self._adapter.execute(sql, params)
        rows = cur.fetchall()
        docs: list[dict[str, Any]] = []
        for row in rows:
            try:
                _id, data_json = row[0], row[1]
                ver = row[2] if self._include_version and len(row) > 2 else None
            except (IndexError, TypeError) as e:
                # Log warning but continue - malformed row shouldn't break entire query
                import warnings

                warnings.warn(f"Skipping malformed row in {self._table}: {e}", RuntimeWarning)
                continue
            if data_json is None:
                raise InvariantViolationError(f"Row {_id} in {self._table} has NULL data JSON")
            obj = json.loads(data_json)

            # Apply field selection if specified
            if self._select_fields:
                obj = {k: obj.get(k) for k in self._select_fields if k in obj}

            obj["_id"] = _id
            if ver is not None:
                obj["_version"] = ver
            docs.append(obj)
        return docs

    def first_dict(self) -> Optional[dict[str, Any]]:
        """Execute with ``LIMIT 1`` and return first parsed dict with ``_id``.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            dict | None: First matching document with ``_id``, or ``None``.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        results = self.limit(1).all_dicts()
        return results[0] if results else None

    def update(self, **fields) -> int:
        """Update matching rows with the given field values.

        Args:
            **fields: Field names and values to update in the JSON data.

        Raises:
            NoAdapterError: If the query has no adapter.
            ValueError: If no fields provided.

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

        cur = self._adapter.execute(sql, set_params + where_params)
        self._adapter.commit()
        return getattr(cur, "rowcount", 0)

    def delete(self) -> int:
        """Delete all matching rows.

        Raises:
            NoAdapterError: If the query has no adapter.

        Returns:
            int: Number of rows deleted.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")

        where = f"WHERE {self._expression.sql}" if self._expression else ""
        params = self._expression.params if self._expression else []

        sql = f"DELETE FROM {self._table} {where}".strip()
        sql = " ".join(sql.split())

        cur = self._adapter.execute(sql, params)
        self._adapter.commit()
        return getattr(cur, "rowcount", 0)


class PaginatedResult:
    """Result of a paginated query with navigation helpers."""

    def __init__(
        self,
        items: list[dict[str, Any]],
        page: int,
        per_page: int,
        total: int,
        total_pages: int,
    ):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total
        self.total_pages = total_pages

    @property
    def has_next(self) -> bool:
        """Return True if there is a next page."""
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        """Return True if there is a previous page."""
        return self.page > 1

    @property
    def next_page(self) -> Optional[int]:
        """Return the next page number, or None if on last page."""
        return self.page + 1 if self.has_next else None

    @property
    def prev_page(self) -> Optional[int]:
        """Return the previous page number, or None if on first page."""
        return self.page - 1 if self.has_prev else None

    def __iter__(self):
        """Iterate over items."""
        return iter(self.items)

    def __len__(self):
        """Return number of items on this page."""
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        """Return pagination info as a dictionary."""
        return {
            "items": self.items,
            "page": self.page,
            "per_page": self.per_page,
            "total": self.total,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_prev": self.has_prev,
        }
