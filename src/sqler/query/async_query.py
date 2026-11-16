import json
import re
import sqlite3
from typing import Any, AsyncIterator, Optional, Self

from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.exceptions import NoAdapterError
from sqler.query.expression import SQLerExpression
from sqler.query.query import (
    _META_COLUMNS,
    _NAMES_SQL,
    _VERSION,
    PaginatedResult,
    _has_version,
    _rewrite_promoted_refs,
    _WriteStatements,
    aend_failed,
    is_missing_table,
)
from sqler.utils import validate_field_name


class AsyncSQLerQuery(_WriteStatements):
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
        clone = self.__class__(
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
        clone._db = kwargs.get("db", self._db)
        clone._use = kwargs.get("use", self._use)
        clone._guard = kwargs.get("guard", self._guard)
        clone._names = kwargs.get("names", self._names)
        return clone

    async def _laid_out(self) -> Self:
        """Async twin of :meth:`sqler.query.query.SQLerQuery._laid_out`."""
        db, table = self._db, self._table
        promoted, names, version = await db._shape_of(table)
        if not version and self._include_version and self._use == "update" and await db._has_version_column(table) is False:
            await db._ensure_versioned_table(table)
            promoted, names, version = await db._shape_of(table)
        key = (names, promoted, version)
        laid = self._laid
        if laid is None or laid[0] != key:
            made = self._clone(
                promoted_fields=promoted, include_version=version, db=None, guard=db._guard_of(table, names), names=names
            )
            laid = self._laid = (key, made)
        return laid[1]

    async def _execute(self, build) -> tuple[Self, Any]:
        """Async twin of :meth:`sqler.query.query.SQLerQuery._execute`."""
        used = self

        async def make() -> tuple[str, list[Any], Any]:
            nonlocal used
            used = await self._laid_out()
            return (*build(used), used._names)

        try:
            if self._db is not None:
                cursor = await self._db._sent(self._table, make)
                return used, cursor
            sql, params = build(self)
            try:
                return self, await self._adapter._execute_judged(sql, params)
            except Exception as exc:
                await aend_failed(self._adapter, exc, self._table, sql)
        except sqlite3.OperationalError as exc:
            if is_missing_table(exc, self._table):
                return used, None
            raise

    async def _updating(self) -> Self:
        """Async twin of :meth:`sqler.query.query.SQLerQuery._updating`."""
        if self._db is not None or self._include_version:
            return self
        cur = await self._adapter.execute(_NAMES_SQL, [self._table])
        try:
            names = tuple(row[0] for row in await cur.fetchall())
        finally:
            await cur.close()
        return self._clone(include_version=True) if _has_version(names) else self

    async def _rows(self, *, include_id: bool = False) -> list[tuple]:
        """Run this query's own SELECT and return every row; ``[]`` when its table is missing."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        _, cur = await self._execute(lambda q: q._build_query(include_id=include_id))
        if cur is None:
            return []
        try:
            return await cur.fetchall()
        finally:
            await cur.close()
            await self._adapter.auto_commit()

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
                raw = f[1:]
                validate_field_name(raw)
                parsed.append((raw, True))
            else:
                validate_field_name(f)
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
        for f in fields:
            validate_field_name(f)
        return self._clone(select_fields=list(fields))

    def with_version(self) -> Self:
        return self._clone(include_version=True)

    def group_by(self, *fields: str) -> Self:
        """Return a new query grouped by the given field(s)."""
        for f in fields:
            validate_field_name(f)
        return self._clone(group_by_fields=list(fields))

    def having(self, expression: SQLerExpression) -> Self:
        """Return a new query with a HAVING clause (requires group_by)."""
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
        """Build a grouped aggregate query."""
        group_exprs = [self._field_expr(f) for f in self._group_by_fields]

        if field is None:
            agg = f"{func}(*)"
        else:
            if field != "_id":
                validate_field_name(field)
            agg = f"{func}({self._field_expr(field)})"

        select_parts = group_exprs + [agg]
        where = self._filter_sql()
        group_by = "GROUP BY " + ", ".join(group_exprs)

        having_clause = ""
        if self._having:
            having_sql = self._having.sql
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
        sql = self._squeezed(sql)
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)

        params = list(self._expression.params) if self._expression else []
        if self._having:
            params.extend(self._having.params)
        return sql, params

    async def _execute_grouped_aggregate(
        self, func: str, field: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Execute a grouped aggregate and return list[dict]."""
        _, cur = await self._execute(lambda q: q._build_grouped_aggregate_query(func, field))
        if cur is None:
            return []
        rows = await cur.fetchall()
        await cur.close()
        await self._adapter.auto_commit()
        agg_key = func.lower()
        results: list[dict[str, Any]] = []
        for row in rows:
            d: dict[str, Any] = {}
            for i, gf in enumerate(self._group_by_fields):
                d[gf] = row[i]
            d[agg_key] = row[len(self._group_by_fields)]
            results.append(d)
        return results

    def _build_query(self, *, include_id: bool = False) -> tuple[str, list[Any]]:
        where = self._filter_sql()
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
            select = "_id, data" + (f", {_VERSION}" if self._include_version else "")
            if self._promoted_fields:
                select += ", " + ", ".join(self._promoted_fields)
        else:
            select = "data"
        sql = f"SELECT {distinct_kw}{select} FROM {self._table} {where} {order} {limit_offset}".strip()
        sql = self._squeezed(sql)
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
        if self._promoted_fields:
            sql = _rewrite_promoted_refs(sql, self._promoted_fields)
        params = self._expression.params if self._expression else []
        return sql, params

    def _build_aggregate_query(
        self, func: str, field: Optional[str] = None
    ) -> tuple[str, list[Any]]:
        """Build an aggregate query (COUNT, SUM, AVG, MIN, MAX)."""
        if field is not None and field != "_id":
            validate_field_name(field)
        where = self._filter_sql()
        if field is None:
            select = f"{func}(*)"
        elif field == "_id":
            select = f"{func}(_id)"
        elif self._promoted_fields and field in self._promoted_fields:
            select = f"{func}({field})"
        else:
            select = f"{func}(json_extract(data, '$.{field}'))"
        sql = f"SELECT {select} FROM {self._table} {where}".strip()
        sql = self._squeezed(sql)
        sql = _rewrite_promoted_refs(sql, _META_COLUMNS)
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
        _, cur = await self._execute(lambda q: q._build_query())
        if cur is None:
            return []
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

    async def count(self):
        """Return count. With group_by returns list[dict], otherwise int."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return await self._execute_grouped_aggregate("COUNT")
        _, cur = await self._execute(lambda q: q._build_aggregate_query("COUNT"))
        if cur is None:
            return 0
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
        return int(row[0]) if row else 0

    async def sum(self, field: str):
        """Return sum. With group_by returns list[dict], otherwise float|None."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return await self._execute_grouped_aggregate("SUM", field)
        _, cur = await self._execute(lambda q: q._build_aggregate_query("SUM", field))
        if cur is None:
            return None
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
        return float(row[0]) if row and row[0] is not None else None

    async def avg(self, field: str):
        """Return average. With group_by returns list[dict], otherwise float|None."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return await self._execute_grouped_aggregate("AVG", field)
        _, cur = await self._execute(lambda q: q._build_aggregate_query("AVG", field))
        if cur is None:
            return None
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
        return float(row[0]) if row and row[0] is not None else None

    async def min(self, field: str):
        """Return minimum. With group_by returns list[dict], otherwise Any|None."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return await self._execute_grouped_aggregate("MIN", field)
        _, cur = await self._execute(lambda q: q._build_aggregate_query("MIN", field))
        if cur is None:
            return None
        row = await cur.fetchone()
        await cur.close()
        await self._adapter.auto_commit()
        return row[0] if row else None

    async def max(self, field: str):
        """Return maximum. With group_by returns list[dict], otherwise Any|None."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        if self._group_by_fields:
            return await self._execute_grouped_aggregate("MAX", field)
        _, cur = await self._execute(lambda q: q._build_aggregate_query("MAX", field))
        if cur is None:
            return None
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
        validate_field_name(field)
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        _, cur = await self._execute(lambda q: q._distinct_sql(field))
        if cur is None:
            return []
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
        items = await self.offset(offset).limit(per_page).all_dicts() if total else []

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
        query, cur = await self._execute(lambda q: q._build_query(include_id=True))
        if cur is None:
            return []
        rows = await cur.fetchall()
        await cur.close()
        await self._adapter.auto_commit()
        return query._documents(rows)

    async def iter_dicts(self) -> AsyncIterator[dict[str, Any]]:
        """Async twin of :meth:`sqler.query.query.SQLerQuery.iter_dicts`: ``async for doc in
        query.iter_dicts()`` gives the documents ``all_dicts()`` gives, one at a time.

        Outside a transaction the statement holds its connection until the iteration ends
        (its rows run out, ``aclose()``, or the event loop closing an iterator that was
        dropped or left by an exception); then it goes back to the pool.

        Raises:
            NoAdapterError: At the first step, if the query has no adapter.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        query, cur = await self._execute(lambda q: q._build_query(include_id=True))
        if cur is None:
            return
        try:
            async for row in cur:
                for doc in query._documents([row]):
                    yield doc
        finally:
            self._adapter._end_read(cur)

    def _documents(self, rows: Any) -> list[dict[str, Any]]:
        """The documents of rows read with :meth:`_build_query`'s ``include_id`` columns."""
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

        Where the table has the ``_version`` column each row's version moves by one, as
        ``update_one()`` and ``update_n()`` move it.

        Args:
            **fields: Field names and values (or SQLerUpdateExpression) to update.

        Returns:
            int: Number of rows updated.
        """
        self._write_checks(fields)
        _, cur = await (await self._updating())._execute(lambda q: q._update_sql(fields, *q._where(), returning=False))
        if cur is None:
            return 0
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
        rows = await self._update_picked(fields, 1, "=")
        return rows[0] if rows else None

    async def update_n(self, n: int, **fields) -> list[dict[str, Any]]:
        """Atomically update up to *n* matching rows and return them.

        Uses ``UPDATE ... WHERE _id IN (SELECT _id ... LIMIT n) RETURNING ...``
        for race-free batch claiming.

        Args:
            n: Maximum number of rows to update.
            **fields: Field names and values (or SQLerUpdateExpression) to update.

        Returns:
            list[dict]: Updated rows as dicts with ``_id``, or empty list.
        """
        return await self._update_picked(fields, n, "IN")

    async def _update_picked(self, fields: dict, n: int, op: str) -> list[dict[str, Any]]:
        self._write_checks(fields, n)
        query, cur = await (await self._updating())._execute(lambda q: q._update_picked_sql(fields, n, op))
        if cur is None:
            return []
        rows = await cur.fetchall()
        await cur.close()
        await self._adapter.auto_commit()
        return [query._returned(row) for row in rows]

    async def _changed(self, fields: dict, n: Optional[int] = None) -> list[tuple[dict, dict]]:
        """Async twin of :meth:`SQLerQuery._changed` (run it inside a transaction)."""
        self._write_checks(fields, n)
        query, cur = await (await self._updating())._execute(lambda q: q._picked(n, q._returning()))
        if cur is None:
            return []
        rows = await cur.fetchall()
        await cur.close()
        before = {doc["_id"]: doc for doc in map(query._returned, rows)}
        if not before:
            return []
        cur = await self._adapter.execute(*query._update_ids_sql(fields, list(before)))
        rows = await cur.fetchall()
        await cur.close()
        after = {doc["_id"]: doc for doc in map(query._returned, rows)}
        return [(doc, after[_id]) for _id, doc in before.items()]

    async def delete(self) -> int:
        """Delete all matching rows.

        Returns:
            int: Number of rows deleted.
        """
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        _, cur = await self._execute(lambda q: q._delete_sql())
        if cur is None:
            return 0
        rowcount = cur.rowcount
        await cur.close()
        await self._adapter.auto_commit()
        return rowcount if rowcount >= 0 else 0

    async def _deleted(self) -> list[dict[str, Any]]:
        """Async twin of :meth:`SQLerQuery._deleted` (run it inside a transaction)."""
        if self._adapter is None:
            raise NoAdapterError("No adapter set for query")
        query, cur = await self._execute(lambda q: q._delete_sql(returning=True))
        if cur is None:
            return []
        rows = await cur.fetchall()
        await cur.close()
        return [query._returned(row) for row in rows]
