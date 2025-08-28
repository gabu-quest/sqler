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
        """Return a new query with the expression AND-ed in.

        Args:
            expression: Boolean expression to filter on.

        Returns:
            SQLerQuery: New query instance.
        """
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
        """Return a new query with the NOT of expression AND-ed in.

        Args:
            expression: Boolean expression to negate and apply.

        Returns:
            SQLerQuery: New query instance.
        """
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
        """Return a new query ordered by the given JSON field.

        Args:
            field: Dotted JSON path to sort by (e.g., ``"age"``).
            desc: Sort descending when True.

        Returns:
            SQLerQuery: New query instance.
        """
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
        """Return a new query with a LIMIT clause.

        Args:
            n: Maximum number of rows to return.

        Returns:
            SQLerQuery: New query instance.
        """
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
        """Return a new query that includes `_version` column in results."""
        return self.__class__(
            self._table,
            self._adapter,
            self._expression,
            self._order,
            self._desc,
            self._limit,
            True,
        )

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
        limit = f"LIMIT {self._limit}" if self._limit is not None else ""
        if include_id:
            select = "_id, data" + (
                ", _version" if (include_version or self._include_version) else ""
            )
        else:
            select = "data"
        sql = f"SELECT {select} FROM {self._table} {where} {order} {limit}".strip()
        sql = " ".join(sql.split())  # collapse double spaces
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
        return self.limit(1).all()[0] if self.limit(1).all() else None

    def count(self) -> int:
        """Return the count of matching rows.

        Raises:
            NoAdapterError: If the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        sql, params = self._build_query()
        count_sql = sql.replace("SELECT data", "SELECT count(*)")
        cur = self._adapter.execute(count_sql, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0

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
            except Exception:
                continue
            if data_json is None:
                raise InvariantViolationError(f"Row {_id} in {self._table} has NULL data JSON")
            obj = json.loads(data_json)
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
