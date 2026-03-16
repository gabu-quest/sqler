import re
from typing import Any, Iterator, Optional, Self

from sqler.adapter.abstract import AdapterABC
from sqler.query import SQLerExpression
from sqler.utils import validate_field_name


def _rewrite_promoted_refs(sql: str, promoted_fields: list[str]) -> str:
    """Replace json_extract(data, '$.field') with direct column reference for promoted fields."""
    for field in promoted_fields:
        # Match both json_extract(data, '$.field') and JSON_EXTRACT(data, '$.field')
        pattern = re.compile(
            r"""json_extract\(data,\s*'\$\.""" + re.escape(field) + r"""'\)""",
            re.IGNORECASE,
        )
        sql = pattern.sub(field, sql)
    return sql


# Built-in columns that live on the SQLite row, not inside the JSON blob.
# Queries using F("_id") or F("_version") must hit the real column, not
# json_extract(data, '$._id') which always returns NULL.
_META_COLUMNS = ["_id", "_version"]


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
        order_fields: Optional[list[tuple[str, bool]]] = None,
        promoted_fields: Optional[list[str]] = None,
        group_by_fields: Optional[list[str]] = None,
        having: Optional[SQLerExpression] = None,
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
        self._group_by_fields = group_by_fields or []
        self._having = having

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
            group_by_fields=kwargs.get("group_by_fields", self._group_by_fields),
            having=kwargs.get("having", self._having),
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

    def order_by(self, *fields: str, desc: bool = False) -> Self:
        """Return a new query ordered by the given JSON field(s).

        Supports Django-style ``-`` prefix for descending order on individual
        fields.  When multiple positional arguments are given, each field can
        optionally start with ``-`` for DESC::

            order_by("-priority", "eta", "ulid")

        The legacy single-field form still works::

            order_by("name", desc=True)

        Args:
            *fields: One or more dotted JSON paths.  A leading ``-`` means DESC.
            desc: When True **and** only one field is given without a ``-``
                prefix, sort that field descending (backward-compat).

        Returns:
            SQLerQuery: New query instance.
        """
        if not fields:
            return self._clone()
        parsed: list[tuple[str, bool]] = []
        for f in fields:
            if f.startswith("-"):
                raw = f[1:]
                validate_field_name(raw)
                parsed.append((raw, True))
            else:
                validate_field_name(f)
                parsed.append((f, False))
        # backward compat: single field + desc=True
        if len(parsed) == 1 and desc and not fields[0].startswith("-"):
            parsed[0] = (parsed[0][0], True)
        return self._clone(order=None, desc=False, order_fields=parsed)

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
        for f in fields:
            validate_field_name(f)
        return self._clone(select_fields=list(fields))

    def with_version(self) -> Self:
        """Return a new query that includes `_version` column in results."""
        return self._clone(include_version=True)

    def group_by(self, *fields: str) -> Self:
        """Return a new query grouped by the given field(s).

        When group_by is active, aggregate methods (count, sum, avg, min, max)
        return ``list[dict]`` instead of scalars.

        Args:
            *fields: Field names to group by.

        Returns:
            SQLerQuery: New query instance.
        """
        for f in fields:
            validate_field_name(f)
        return self._clone(group_by_fields=list(fields))

    def having(self, expression: SQLerExpression) -> Self:
        """Return a new query with a HAVING clause (requires group_by).

        Args:
            expression: Boolean expression for the HAVING clause.

        Returns:
            SQLerQuery: New query instance.
        """
        return self._clone(having=expression)

    def _field_expr(self, field: str) -> str:
        """Return the SQL expression for a field (promoted or json_extract)."""
        if field == "_id":
            return "_id"
        if field == "_version":
            return "_version"
        if self._promoted_fields and field in self._promoted_fields:
            return field
        return f"json_extract(data, '$.{field}')"

    def _build_grouped_aggregate_query(
        self, func: str, field: Optional[str] = None
    ) -> tuple[str, list[Any]]:
        """Build a grouped aggregate query.

        Returns:
            tuple[str, list[Any]]: SQL string and parameter list.
        """
        group_exprs = [self._field_expr(f) for f in self._group_by_fields]

        if field is None:
            agg = f"{func}(*)"
        else:
            if field != "_id":
                validate_field_name(field)
            agg = f"{func}({self._field_expr(field)})"

        select_parts = group_exprs + [agg]
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        group_by = "GROUP BY " + ", ".join(group_exprs)

        having_clause = ""
        if self._having:
            having_sql = self._having.sql
            # Replace pseudo-fields like _count, _sum etc with aggregate expressions
            having_sql = re.sub(
                r"json_extract\(data,\s*'\$\._count'\)",
                "COUNT(*)",
                having_sql,
                flags=re.IGNORECASE,
            )
            having_sql = re.sub(
                r"json_extract\(data,\s*'\$\._sum'\)",
                agg if func == "SUM" else "SUM(*)",
                having_sql,
                flags=re.IGNORECASE,
            )
            having_sql = re.sub(
                r"json_extract\(data,\s*'\$\._avg'\)",
                agg if func == "AVG" else "AVG(*)",
                having_sql,
                flags=re.IGNORECASE,
            )
            having_sql = re.sub(
                r"json_extract\(data,\s*'\$\._min'\)",
                agg if func == "MIN" else "MIN(*)",
                having_sql,
                flags=re.IGNORECASE,
            )
            having_sql = re.sub(
                r"json_extract\(data,\s*'\$\._max'\)",
                agg if func == "MAX" else "MAX(*)",
                having_sql,
                flags=re.IGNORECASE,
            )
            having_clause = f"HAVING {having_sql}"

        sql = f"SELECT {', '.join(select_parts)} FROM {self._table} {where} {group_by} {having_clause}".strip()
        sql = " ".join(sql.split())
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)

        params = list(self._expression.params) if self._expression else []
        if self._having:
            params.extend(self._having.params)
        return sql, params

    def _execute_grouped_aggregate(
        self, func: str, field: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Execute a grouped aggregate and return list[dict]."""
        sql, params = self._build_grouped_aggregate_query(func, field)
        cur = self._adapter.execute(sql, params)
        rows = cur.fetchall()
        agg_key = func.lower()
        results: list[dict[str, Any]] = []
        for row in rows:
            d: dict[str, Any] = {}
            for i, gf in enumerate(self._group_by_fields):
                d[gf] = row[i]
            d[agg_key] = row[len(self._group_by_fields)]
            results.append(d)
        return results

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
            # SQLite requires LIMIT with OFFSET, use -1 for unlimited
            limit_offset = f"LIMIT -1 OFFSET {self._offset}"

        distinct_kw = "DISTINCT " if self._distinct else ""
        if include_id:
            select = "_id, data" + (
                ", _version" if (include_version or self._include_version) else ""
            )
            if self._promoted_fields:
                select += ", " + ", ".join(self._promoted_fields)
        else:
            select = "data"
        sql = f"SELECT {distinct_kw}{select} FROM {self._table} {where} {order} {limit_offset}".strip()
        sql = " ".join(sql.split())  # collapse double spaces
        # Rewrite meta columns (_id, _version) from json_extract to direct column
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)
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
        if field is not None and field != "_id":
            validate_field_name(field)
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
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)
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

    def count(self):
        """Return the count of matching rows.

        When ``group_by`` is active, returns ``list[dict]`` with counts per group.
        Otherwise returns ``int``.

        Raises:
            NoAdapterError: If the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return self._execute_grouped_aggregate("COUNT")
        sql, params = self._build_aggregate_query("COUNT")
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def sum(self, field: str):
        """Return the sum of values for the specified field.

        When ``group_by`` is active, returns ``list[dict]`` with sums per group.
        Otherwise returns ``float | None``.

        Args:
            field: JSON field path to sum.

        Raises:
            NoAdapterError: If the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return self._execute_grouped_aggregate("SUM", field)
        sql, params = self._build_aggregate_query("SUM", field)
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def avg(self, field: str):
        """Return the average of values for the specified field.

        When ``group_by`` is active, returns ``list[dict]`` with averages per group.
        Otherwise returns ``float | None``.

        Args:
            field: JSON field path to average.

        Raises:
            NoAdapterError: If the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return self._execute_grouped_aggregate("AVG", field)
        sql, params = self._build_aggregate_query("AVG", field)
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def min(self, field: str):
        """Return the minimum value for the specified field.

        When ``group_by`` is active, returns ``list[dict]`` with minimums per group.
        Otherwise returns ``Any | None``.

        Args:
            field: JSON field path to find minimum.

        Raises:
            NoAdapterError: If the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return self._execute_grouped_aggregate("MIN", field)
        sql, params = self._build_aggregate_query("MIN", field)
        cur = self._adapter.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None

    def max(self, field: str):
        """Return the maximum value for the specified field.

        When ``group_by`` is active, returns ``list[dict]`` with maximums per group.
        Otherwise returns ``Any | None``.

        Args:
            field: JSON field path to find maximum.

        Raises:
            NoAdapterError: If the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return self._execute_grouped_aggregate("MAX", field)
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
        validate_field_name(field)
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        where = f"WHERE {self._expression.sql}" if self._expression else ""
        sql = f"SELECT DISTINCT json_extract(data, '$.{field}') FROM {self._table} {where}".strip()
        sql = " ".join(sql.split())
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)
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
        # Calculate where promoted columns appear in the row tuple
        # Layout: _id(0), data(1), [_version(2)], [promoted...]
        ver_offset = 1 if self._include_version else 0
        promoted_start = 2 + ver_offset  # after _id, data, optional _version

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

            # Merge promoted column values from the row
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

    def iter_dicts(self) -> Iterator[dict[str, Any]]:
        """Yield parsed dicts one at a time (memory-efficient streaming).

        Unlike ``all_dicts()`` which loads all rows into memory, this method
        iterates over the cursor row by row.

        Raises:
            NoAdapterError: If the query has no adapter.

        Yields:
            dict[str, Any]: One document per row with ``_id`` included.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        import json

        sql, params = self._build_query(include_id=True, include_version=False)
        cur = self._adapter.execute(sql, params)
        ver_offset = 1 if self._include_version else 0
        promoted_start = 2 + ver_offset

        try:
            for row in cur:
                try:
                    _id, data_json = row[0], row[1]
                    ver = row[2] if self._include_version and len(row) > 2 else None
                except (IndexError, TypeError) as e:
                    import warnings

                    warnings.warn(f"Skipping malformed row in {self._table}: {e}", RuntimeWarning)
                    continue
                if data_json is None:
                    raise InvariantViolationError(f"Row {_id} in {self._table} has NULL data JSON")
                obj = json.loads(data_json)

                if self._promoted_fields:
                    for i, col_name in enumerate(self._promoted_fields):
                        idx = promoted_start + i
                        if idx < len(row):
                            obj[col_name] = row[idx]

                if self._select_fields:
                    obj = {k: obj.get(k) for k in self._select_fields if k in obj}

                obj["_id"] = _id
                if ver is not None:
                    obj["_version"] = ver
                yield obj
        finally:
            cur.close()

    def update(self, **fields) -> int:
        """Update matching rows with the given field values.

        Supports F-expressions for atomic operations::

            query.update(count=F("count") + 1)  # atomic increment

        Args:
            **fields: Field names and values (or SQLerUpdateExpression) to update.

        Raises:
            NoAdapterError: If the query has no adapter.
            ValueError: If no fields provided.

        Returns:
            int: Number of rows updated.
        """
        from sqler.query.field import SQLerUpdateExpression

        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if not fields:
            raise ValueError("No fields to update")
        for key in fields:
            validate_field_name(key)

        import json

        # Separate promoted column updates from JSON updates
        promoted_set: list[str] = []
        promoted_params: list[Any] = []
        json_set_parts: list[str] = []
        json_set_params: list[Any] = []

        for field, value in fields.items():
            is_promoted = field in self._promoted_fields
            if isinstance(value, SQLerUpdateExpression):
                expr_sql = value.sql_expr
                expr_sql = _rewrite_promoted_refs(expr_sql, _META_COLUMNS)
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

        # Build SET clause
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
        if where:
            where = _rewrite_promoted_refs(where, _META_COLUMNS)
        if self._promoted_fields and where:
            where = _rewrite_promoted_refs(where, self._promoted_fields)

        sql = f"UPDATE {self._table} SET {', '.join(set_clauses)} {where}".strip()
        sql = " ".join(sql.split())

        cur = self._adapter.execute(sql, all_set_params + where_params)
        self._adapter.commit()
        return getattr(cur, "rowcount", 0)

    def update_one(self, **fields) -> Optional[dict[str, Any]]:
        """Atomically update one matching row and return it.

        Uses ``UPDATE ... WHERE _id = (SELECT _id ... LIMIT 1) RETURNING ...``
        for race-free claiming. The query's WHERE and ORDER BY determine which
        row is selected.

        When ``include_version`` is set (SafeModel querysets), ``_version`` is
        auto-bumped.

        Args:
            **fields: Field names and values (or SQLerUpdateExpression) to update.

        Returns:
            dict | None: The updated row as a dict with ``_id`` (and ``_version``
            if applicable), or None if no row matched.
        """
        from sqler.query.field import SQLerUpdateExpression

        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if not fields:
            raise ValueError("No fields to update")
        for key in fields:
            validate_field_name(key)

        import json

        # Build the SET clause (same logic as update())
        promoted_set: list[str] = []
        promoted_params: list[Any] = []
        json_set_parts: list[str] = []
        json_set_params: list[Any] = []

        for field, value in fields.items():
            is_promoted = field in self._promoted_fields
            if isinstance(value, SQLerUpdateExpression):
                expr_sql = value.sql_expr
                expr_sql = _rewrite_promoted_refs(expr_sql, _META_COLUMNS)
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

        # Auto-bump _version for SafeModel querysets
        if self._include_version:
            set_clauses.append("_version = _version + 1")

        # Build inner SELECT for the subquery
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

        # Rewrite meta columns and promoted refs in inner query
        if inner_where:
            inner_where = _rewrite_promoted_refs(inner_where, _META_COLUMNS)
        if inner_order:
            inner_order = _rewrite_promoted_refs(inner_order, _META_COLUMNS)
        if self._promoted_fields:
            if inner_where:
                inner_where = _rewrite_promoted_refs(inner_where, self._promoted_fields)
            if inner_order:
                inner_order = _rewrite_promoted_refs(inner_order, self._promoted_fields)

        inner_select = f"SELECT _id FROM {self._table} {inner_where} {inner_order} LIMIT 1"
        inner_select = " ".join(inner_select.split())

        # Build RETURNING clause
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
        cur = self._adapter.execute(sql, all_params)
        row = cur.fetchone()
        self._adapter.commit()

        if row is None:
            return None

        # Parse the RETURNING row
        obj = json.loads(row[1])  # data column
        obj["_id"] = row[0]

        col_offset = 2
        if self._include_version:
            obj["_version"] = row[col_offset]
            col_offset += 1
        if self._promoted_fields:
            for i, col_name in enumerate(self._promoted_fields):
                obj[col_name] = row[col_offset + i]

        return obj

    def update_n(self, n: int, **fields) -> list[dict[str, Any]]:
        """Atomically update up to *n* matching rows and return them.

        Uses ``UPDATE ... WHERE _id IN (SELECT _id ... LIMIT n) RETURNING ...``
        for race-free batch claiming.

        Args:
            n: Maximum number of rows to update.
            **fields: Field names and values (or SQLerUpdateExpression) to update.

        Returns:
            list[dict]: Updated rows as dicts with ``_id``, or empty list.
        """
        from sqler.query.field import SQLerUpdateExpression

        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if not fields:
            raise ValueError("No fields to update")
        if n < 1:
            raise ValueError("n must be >= 1")
        for key in fields:
            validate_field_name(key)

        import json

        promoted_set: list[str] = []
        promoted_params: list[Any] = []
        json_set_parts: list[str] = []
        json_set_params: list[Any] = []

        for field, value in fields.items():
            is_promoted = field in self._promoted_fields
            if isinstance(value, SQLerUpdateExpression):
                expr_sql = value.sql_expr
                expr_sql = _rewrite_promoted_refs(expr_sql, _META_COLUMNS)
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

        if inner_where:
            inner_where = _rewrite_promoted_refs(inner_where, _META_COLUMNS)
        if inner_order:
            inner_order = _rewrite_promoted_refs(inner_order, _META_COLUMNS)
        if self._promoted_fields:
            if inner_where:
                inner_where = _rewrite_promoted_refs(inner_where, self._promoted_fields)
            if inner_order:
                inner_order = _rewrite_promoted_refs(inner_order, self._promoted_fields)

        inner_select = f"SELECT _id FROM {self._table} {inner_where} {inner_order} LIMIT {int(n)}"
        inner_select = " ".join(inner_select.split())

        returning_cols = ["_id", "data"]
        if self._include_version:
            returning_cols.append("_version")
        if self._promoted_fields:
            returning_cols.extend(self._promoted_fields)

        sql = (
            f"UPDATE {self._table} SET {', '.join(set_clauses)} "
            f"WHERE _id IN ({inner_select}) "
            f"RETURNING {', '.join(returning_cols)}"
        )
        sql = " ".join(sql.split())

        all_params = all_set_params + inner_where_params
        cur = self._adapter.execute(sql, all_params)
        rows = cur.fetchall()
        self._adapter.commit()

        if not rows:
            return []

        results: list[dict[str, Any]] = []
        for row in rows:
            obj = json.loads(row[1])
            obj["_id"] = row[0]

            col_offset = 2
            if self._include_version:
                obj["_version"] = row[col_offset]
                col_offset += 1
            if self._promoted_fields:
                for i, col_name in enumerate(self._promoted_fields):
                    obj[col_name] = row[col_offset + i]

            results.append(obj)

        return results

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
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)

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
