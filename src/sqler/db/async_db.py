import json
import sqlite3
from contextvars import ContextVar
from typing import Any, Optional

from sqler._stored import dumps
from sqler.adapter.asynchronous import DEFAULT_ACQUIRE_TIMEOUT_S, AsyncSQLiteAdapter, PoolStats
from sqler.db.sqler_db import (
    _AT_VERSION,
    _columns_sql,
    _ended_while_leaving,
    _fields_update,
    _id_params,
    _in_order,
    _insert_sql,
    _keep_word,
    _Level,
    _moved,
    _promoted_in,
    _promotion,
    _record,
    _refuse_unwritten,
    _row,
    _row_columns,
    _row_documents,
    _rows_sql,
    _runs,
    _settled,
    _split_promoted,
    _unwritable,
)
from sqler.exceptions import StaleVersionError
from sqler.query.query import is_missing_table
from sqler.utils import (
    validate_check_expression,
    validate_column_def,
    validate_column_name,
    validate_field_name,
    validate_identifier,
    validate_table_name,
)


async def _afetch_all(adapter: Any, sql: str, params: list) -> list:
    cur = await adapter.execute(sql, params)
    try:
        return await cur.fetchall()
    finally:
        await cur.close()


async def _aselect_rows(adapter: Any, table: str, columns: tuple[str, ...], ids: list) -> Optional[list]:
    """Async twin of :func:`sqler.db.sqler_db._select_rows`."""
    n, params = _id_params(ids)
    try:
        return await _afetch_all(adapter, _rows_sql(table, columns, n), params)
    except sqlite3.OperationalError as exc:
        if is_missing_table(exc, table):
            return None
        raise


class AsyncSQLerDB:
    """Async document store for JSON blobs on SQLite."""

    @classmethod
    def in_memory(
        cls,
        shared: bool = True,
        *,
        name: Optional[str] = None,
        acquire_timeout: Optional[float] = DEFAULT_ACQUIRE_TIMEOUT_S,
        timeout_ms: int = 5000,
    ) -> "AsyncSQLerDB":
        """In-memory database; ``acquire_timeout`` and ``timeout_ms`` are the two waits of ``AsyncSQLiteAdapter``."""
        adapter = AsyncSQLiteAdapter.in_memory(
            shared=shared, name=name, acquire_timeout=acquire_timeout, timeout_ms=timeout_ms
        )
        return cls(adapter)

    @classmethod
    def on_disk(
        cls,
        path: str = "sqler.db",
        *,
        acquire_timeout: Optional[float] = DEFAULT_ACQUIRE_TIMEOUT_S,
        timeout_ms: int = 5000,
    ) -> "AsyncSQLerDB":
        """Database file at ``path``; ``acquire_timeout`` and ``timeout_ms`` are the two waits of ``AsyncSQLiteAdapter``."""
        adapter = AsyncSQLiteAdapter.on_disk(path, acquire_timeout=acquire_timeout, timeout_ms=timeout_ms)
        return cls(adapter)

    def __init__(self, adapter: AsyncSQLiteAdapter):
        self.adapter = adapter
        # what is remembered about committed tables: versioned tables, and the promoted columns
        # of each table this object has seen settled (``_settled``)
        self._versioned_tables: set[str] = set()
        self._promoted_columns: dict[str, list[str]] = {}
        # tables seen settled whose record could not be written then: the next ensure writes it
        self._unrecorded: set[str] = set()
        # what each open level of this object's own transactions learned, innermost last;
        # per task, like the adapter's depth (a task started inside a level shares it)
        self._open: ContextVar[tuple[_Level, ...]] = ContextVar(
            "sqler_open_levels", default=()
        )

    async def connect(self) -> None:
        await self.adapter.connect()

    async def close(self) -> None:
        await self.adapter.close()

    def pool_stats(self) -> PoolStats:
        """Return the adapter's connection pool statistics (synchronous, cheap)."""
        return self.adapter.pool_stats()

    async def ensure_table(
        self,
        table: str,
        *,
        promoted: Optional[dict[str, str]] = None,
        checks: Optional[dict[str, str]] = None,
        versioned: bool = False,
    ) -> None:
        """Create a table if it is missing, with promoted columns, CHECK constraints and a version.

        The async twin of ``SQLerDB.ensure_table``; same arguments, same schema.
        """
        if promoted or checks:
            await self._ensure_table_with_promoted(table, promoted or {}, checks)
        else:
            await self._ensure_table(table)
        if versioned:
            await self._ensure_versioned_table(table)

    async def _ensure_table(self, table: str) -> None:
        table = validate_table_name(table)
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            data JSON NOT NULL
        );
        """
        await self.adapter.execute(ddl)
        await self.adapter.auto_commit()

    async def _ensure_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
        *,
        model: Any = None,
    ) -> None:
        """Ensure ``table`` for a promoted write; nothing is sent when it is remembered here
        with every column of ``promoted``."""
        known = self._promoted_of(table)
        if known is None or any(col not in known for col in promoted) or table in self._unrecorded:
            await self._ensure_table_with_promoted(table, promoted, checks, model=model)

    async def _ensure_table_with_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
        *,
        model: Any = None,
    ) -> None:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._ensure_table_with_promoted`."""
        table = validate_table_name(table)
        checks = checks or {}
        for col_name, col_def in promoted.items():
            validate_column_name(col_name)
            validate_column_def(col_def)
        for chk_expr in checks.values():
            validate_check_expression(chk_expr)

        names, unsettled = await self._run(_settled(table))
        unrecorded = False
        if not names or any(col not in names for col in promoted):
            async with self:  # one transaction: the column, its values and the record, or nothing
                columns = await self._run(_promotion(table, promoted, checks, model))
        else:
            columns = _promoted_in(names)
            if unsettled:
                try:
                    async with self:
                        await self._run(_record(table, unsettled))
                except sqlite3.OperationalError as exc:
                    if not _unwritable(exc):
                        raise
                    unrecorded = True  # settled all the same: there is nothing to add or move
        self._remember(table, promoted=columns)
        (self._unrecorded.add if unrecorded else self._unrecorded.discard)(table)

    async def move_promoted(self, table: str, column: str, *, keep: str) -> None:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB.move_promoted`."""
        keep = _keep_word(keep)
        table = validate_table_name(table)
        validate_column_name(column)
        async with self:
            await self._run(_moved(table, column, keep))

    async def _run(self, steps: Any) -> Any:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._run`."""
        try:
            step = next(steps)
            while True:
                try:
                    rows = await _afetch_all(self.adapter, *step)
                except sqlite3.OperationalError as exc:
                    step = steps.throw(exc)
                else:
                    step = steps.send(rows)
        except StopIteration as done:
            return done.value

    def _learned(self) -> Optional[tuple[set[str], dict[str, list[str]]]]:
        """Where an ensure call keeps what it learned about a table.

        The innermost open level of this object's own transactions, if the adapter's depth is
        still that level's; with no level open, the shared memory, at depth 0. Anywhere else
        the transaction is not this object's (the adapter used directly, another thread's on
        the single ``":memory:"`` connection): nowhere. Forgetting costs a repeated
        ``CREATE TABLE IF NOT EXISTS``, remembering wrongly loses a write.
        """
        depth = self.adapter._depth()
        levels = self._open.get()
        if levels:
            level = levels[-1]
            return (level.versioned, level.promoted) if level.depth == depth else None
        return (self._versioned_tables, self._promoted_columns) if depth == 0 else None

    def _remember(self, table: str, *, promoted: Optional[list[str]] = None) -> None:
        """Remember ``table``'s ``promoted`` columns, or without them that it has ``_version``."""
        learned = self._learned()
        if learned is None:
            return
        if promoted is None:
            learned[0].add(table)
        else:
            learned[1][table] = promoted

    def _is_versioned(self, table: str) -> bool:
        """Whether ``table`` is remembered to have its ``_version`` column, in this task now."""
        return table in self._versioned_tables or any(table in level.versioned for level in self._open.get())

    def _promoted_of(self, table: str) -> Optional[list[str]]:
        """The promoted columns remembered for ``table`` in this task now; ``None`` if it is not remembered."""
        for level in reversed(self._open.get()):
            if table in level.promoted:
                return level.promoted[table]
        return self._promoted_columns.get(table)

    async def _columns_of(self, table: str) -> list[str]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._columns_of`."""
        promoted = self._promoted_of(table)
        if promoted is None:
            names, unsettled = await self._run(_settled(table))
            promoted = _promoted_in(names)
            learned = self._learned() if names else None
            if learned is not None:
                # an entry made since the statements above (another task's ensure) is newer
                promoted = learned[1].setdefault(table, promoted)
                if unsettled:
                    self._unrecorded.add(table)
        return promoted

    async def upsert_document_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        promoted_fields: list[str],
    ) -> int:
        """Insert or update, splitting promoted fields into real columns (async)."""
        table = validate_table_name(table)
        json_doc, promoted_vals = _split_promoted(doc, promoted_fields)

        payload = dumps(json_doc)

        if _id is None:
            cols = ["data"] + list(promoted_vals.keys())
            placeholders = ["json(?)"] + ["?" for _ in promoted_vals]
            params = [payload] + list(promoted_vals.values())
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)});"
            cur = await self.adapter.execute(sql, params)
            try:
                await self.adapter.auto_commit()
                last_id = cur.lastrowid
            finally:
                await cur.close()
            return last_id
        else:
            set_parts = ["data = json(?)"]
            params: list[Any] = [payload]
            for col, val in promoted_vals.items():
                set_parts.append(f"{col} = ?")
                params.append(val)
            params.append(_id)
            sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE _id = ?;"
            cur = await self.adapter.execute(sql, params)
            try:
                await self.adapter.auto_commit()
            finally:
                await cur.close()
            return _id

    async def upsert_with_version_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        expected_version: Optional[int],
        promoted_fields: list[str],
    ) -> tuple[int, int]:
        """Insert or update with optimistic locking, splitting promoted columns (async)."""
        table = validate_table_name(table)
        json_doc, promoted_vals = _split_promoted(doc, promoted_fields)
        await self._ensure_versioned_table(table)

        payload = dumps(json_doc)

        if _id is None:
            cols = ["data", "_version"] + list(promoted_vals.keys())
            placeholders = ["json(?)", "0"] + ["?" for _ in promoted_vals]
            params = [payload] + list(promoted_vals.values())
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)});"
            cur = await self.adapter.execute(sql, params)
            try:
                await self.adapter.auto_commit()
                last_id = cur.lastrowid
            finally:
                await cur.close()
            return last_id, 0

        if expected_version is None:
            raise ValueError("expected_version required for update")

        set_parts = ["data = json(?)", "_version = _version + 1"]
        params_list: list[Any] = [payload]
        for col, val in promoted_vals.items():
            set_parts.append(f"{col} = ?")
            params_list.append(val)
        params_list.extend([_id, expected_version, expected_version, expected_version])

        sql = (
            f"UPDATE {table} SET {', '.join(set_parts)} "
            f"WHERE {_AT_VERSION};"
        )
        cur = await self.adapter.execute(sql, params_list)
        try:
            rowcount = cur.rowcount
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if rowcount == 0:
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

    async def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB.insert_document`."""
        table = validate_table_name(table)
        await self._ensure_table(table)
        return await self.upsert_document_promoted(table, None, doc, await self._columns_of(table))

    async def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB.upsert_document`."""
        table = validate_table_name(table)
        await self._ensure_table(table)
        if _id is None:
            return await self.insert_document(table, doc)
        return await self.upsert_document_promoted(table, _id, doc, await self._columns_of(table))

    async def _update_fields(
        self,
        table: str,
        _id: int,
        doc: dict[str, Any],
        fields: Any,
        promoted: Any = (),
        expected_version: Optional[int] = None,
    ) -> Optional[int]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._update_fields`."""
        table = validate_table_name(table)
        if expected_version is None:
            await self._ensure_table(table)
        else:
            await self._ensure_versioned_table(table)
        update = _fields_update(table, _id, doc, fields, await self._columns_of(table), expected_version)
        if update is None:
            return None
        cur = await self.adapter.execute(*update)
        try:
            rowcount = cur.rowcount
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if rowcount > 0:
            return None if expected_version is None else expected_version + 1
        gone = expected_version is None or not await _afetch_all(
            self.adapter, f"SELECT 1 FROM {table} WHERE _id = ?;", [_id]
        )
        _refuse_unwritten(gone, table, _id)

    async def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        rows, columns = await self._read_rows(table, [_id])
        return _row_documents(rows, columns)[0] if rows else None

    async def find_documents(self, table: str, ids: list[int]) -> list[dict[str, Any]]:
        """Fetch multiple documents by id list (batch operation).

        Documents are returned in the same order as the input ids. If an id
        is not found, it is omitted from the result (no None placeholder).

        Args:
            table: Table name.
            ids: List of row ids to fetch.

        Returns:
            list[dict]: Decoded documents with ``_id`` merged in.
        """
        if not ids:
            return []
        rows, columns = await self._read_rows(table, ids)
        return _row_documents(_in_order(rows or [], ids), columns)

    async def _read_rows(self, table: str, ids: list, *, versioned: bool = False) -> tuple[Optional[list], list[str]]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._read_rows`."""
        table = validate_table_name(table)
        has_version = versioned and await self._has_version_column(table)
        promoted = await self._columns_of(table)
        columns = _row_columns(promoted, versioned)
        if versioned and not has_version:
            rows = await _aselect_rows(self.adapter, table, _row_columns(promoted), ids)
            rows = None if rows is None else [(r[0], r[1], 0, *r[2:]) for r in rows]
        else:
            rows = await _aselect_rows(self.adapter, table, columns, ids)
        if rows is None:
            return None, columns  # a read creates no table
        await self.adapter.auto_commit()
        return rows, columns

    async def _has_version_column(self, table: str) -> Optional[bool]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._has_version_column`."""
        if self._is_versioned(table):
            return True
        names = [row[1] for row in await _afetch_all(self.adapter, _columns_sql(table), [])]
        if names and self._promoted_of(table) is None and not _promoted_in(names):
            self._remember(table, promoted=[])
        if "_version" not in names:
            return False if names else None
        self._remember(table)
        return True

    async def delete_document(self, table: str, _id: int) -> None:
        """Delete a document by id.

        Args:
            table: Table name.
            _id: Row id to delete.
        """
        await self._ensure_table(table)
        cur = await self.adapter.execute(f"DELETE FROM {table} WHERE _id = ?;", [_id])
        try:
            await self.adapter.auto_commit()
        finally:
            await cur.close()

    # ---- batch insert helpers ----

    async def insert_many(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
        """Multi-row INSERT for plain JSON documents.

        All documents must be new (no ``_id``). Uses chunked multi-row INSERT
        statements to stay within SQLite's 999-parameter limit.

        Args:
            table: Table name.
            docs: List of JSON-serializable dicts to insert.

        Returns:
            list[int]: Assigned ``_id`` for each document, preserving order.
        """
        if not docs:
            return []
        table = validate_table_name(table)
        await self._ensure_table(table)
        return await self._insert_many_chunked(table, docs, versioned=False, promoted_fields=await self._columns_of(table))

    async def insert_many_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[int]:
        """Multi-row INSERT splitting promoted fields into real columns.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Keys to extract from each doc into real columns.

        Returns:
            list[int]: Assigned ``_id`` for each document, preserving order.
        """
        if not docs:
            return []
        table = validate_table_name(table)
        for field in promoted_fields:
            validate_identifier(field)
        return await self._insert_many_chunked(table, docs, versioned=False, promoted_fields=promoted_fields)

    async def insert_many_with_version(
        self, table: str, docs: list[dict[str, Any]]
    ) -> list[tuple[int, int]]:
        """Multi-row INSERT with ``_version=0`` for versioned tables.

        Args:
            table: Table name.
            docs: List of JSON-serializable dicts.

        Returns:
            list[tuple[int, int]]: ``(id, 0)`` pairs for each document.
        """
        if not docs:
            return []
        table = validate_table_name(table)
        columns = await self._columns_of(table)
        await self._ensure_versioned_table(table)
        ids = await self._insert_many_chunked(table, docs, versioned=True, promoted_fields=columns)
        return [(id_, 0) for id_ in ids]

    async def insert_many_with_version_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[tuple[int, int]]:
        """Multi-row INSERT with ``_version=0`` splitting promoted fields.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Keys to extract into real columns.

        Returns:
            list[tuple[int, int]]: ``(id, 0)`` pairs for each document.
        """
        if not docs:
            return []
        for field in promoted_fields:
            validate_identifier(field)
        await self._ensure_versioned_table(table)
        ids = await self._insert_many_chunked(table, docs, versioned=True, promoted_fields=promoted_fields)
        return [(id_, 0) for id_ in ids]

    async def _insert_many_chunked(
        self,
        table: str,
        docs: list[dict[str, Any]],
        *,
        versioned: bool,
        promoted_fields: list[str] | None = None,
    ) -> list[int]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._insert_many_chunked`."""
        all_ids: list[int] = []
        async with self.transaction():
            for held, rows in _runs(docs, promoted_fields or (), 0):
                params = [p for _, text, values in rows for p in (text, *values)]
                cur = await self.adapter.execute(_insert_sql(table, held, len(rows), versioned=versioned), params)
                try:
                    last_id = cur.lastrowid
                finally:
                    await cur.close()
                # ID range is contiguous because BEGIN IMMEDIATE (from
                # self.transaction()) holds an exclusive write lock, preventing
                # concurrent inserts from interleaving AUTOINCREMENT IDs.
                all_ids.extend(range(last_id - len(rows) + 1, last_id + 1))
        return all_ids

    async def bulk_upsert(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
        """Upsert multiple documents efficiently.

        New docs (without ``_id``) are inserted and receive ids. Existing docs
        (with ``_id``) are updated.

        Args:
            table: Table name.
            docs: List of documents. If an element contains ``_id``, it is
                treated as an update; otherwise, an insert.

        Returns:
            list[int]: The ``_id`` for each input document, preserving order.
        """
        table = validate_table_name(table)
        await self._ensure_table(table)
        columns = await self._columns_of(table) if docs else []
        assigned: list[int] = []
        for doc in docs:
            doc_id = doc.get("_id")
            held, text, values = _row({k: v for k, v in doc.items() if k != "_id"}, columns)
            if doc_id is None:
                cur = await self.adapter.execute(_insert_sql(table, held, 1), [text, *values])
                try:
                    new_id = int(cur.lastrowid)  # type: ignore[attr-defined]
                finally:
                    await cur.close()
                assigned.append(new_id)
                doc["_id"] = new_id
            else:
                cur = await self.adapter.execute(_insert_sql(table, held, 1, upsert=True), [int(doc_id), text, *values])
                await cur.close()
                assigned.append(int(doc_id))
        await self.adapter.auto_commit()
        return assigned

    async def execute_sql(
        self, query: str, params: Optional[list[Any]] = None
    ) -> list[dict[str, Any]]:
        """Run a custom SELECT and return lightweight row mappings.

        Only read-only statements (SELECT, EXPLAIN, PRAGMA, WITH) are allowed.

        When the result set exposes a ``data`` column alongside ``_id``, the
        JSON payload is decoded and merged with ``_id``. For ad-hoc projections
        (e.g. ``SELECT _id``) the method returns simple dicts keyed by the
        selected columns so callers can hydrate with :meth:`AsyncSQLerModel.from_id`.

        Args:
            query: SQL SELECT statement.
            params: Optional parameter list.

        Returns:
            list[dict[str, Any]]: Decoded documents with ``_id`` included.

        Raises:
            ValueError: If the query is not a read-only statement.
        """
        stripped = query.strip()
        first_word = stripped.split()[0].upper() if stripped else ""
        if first_word not in ("SELECT", "EXPLAIN", "PRAGMA", "WITH"):
            raise ValueError(
                "execute_sql only accepts read-only queries (SELECT/EXPLAIN/PRAGMA/WITH)"
            )
        # Reject multi-statement queries (semicolon before the end)
        if ";" in stripped.rstrip(";"):
            raise ValueError(
                "execute_sql does not allow multi-statement queries"
            )
        cur = await self.adapter.execute(query, params or [])
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        docs: list[dict[str, Any]] = []
        for row in rows:
            if len(row) >= 2:
                # Assume (_id, data) or (_id, data, ...)
                try:
                    obj = json.loads(row[1])
                    obj["_id"] = int(row[0])
                    docs.append(obj)
                except (json.JSONDecodeError, TypeError):
                    docs.append({"_id": int(row[0])})
            elif len(row) == 1:
                docs.append({"_id": int(row[0])})
            else:
                docs.append({})
        return docs

    async def create_index(
        self,
        table: str,
        field: str,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Create an index on a JSON field or literal column.

        For JSON paths, pass dotted paths like ``"meta.level"``. These are
        compiled into ``json_extract(data, '$.meta.level')``. Literal columns
        (e.g., ``_id``) should be prefixed with ``_`` and are used as-is.

        Args:
            table: Table name.
            field: Dotted JSON path or literal column.
            unique: Enforce uniqueness of the index.
            name: Optional index name; autogenerated if omitted.
            where: Optional partial-index WHERE clause.
        """
        await self._ensure_table(table)
        validate_field_name(field) if not field.startswith("_") else validate_identifier(field)
        if name is not None:
            validate_identifier(name)
        if where is not None:
            validate_check_expression(where)
        idx_name = name or f"idx_{table}_{field.replace('.', '_')}"
        unique_sql = "UNIQUE" if unique else ""
        expr = f"json_extract(data, '$.{field}')" if not field.startswith("_") else field
        where_sql = f"WHERE {where}" if where else ""
        ddl = f"CREATE {unique_sql} INDEX IF NOT EXISTS {idx_name} ON {table} ({expr}) {where_sql};"
        cur = await self.adapter.execute(ddl)
        try:
            await self.adapter.auto_commit()
        finally:
            await cur.close()

    async def drop_index(self, name: str) -> None:
        """Drop an index by name.

        Args:
            name: Index name.
        """
        validate_identifier(name)
        ddl = f"DROP INDEX IF EXISTS {name};"
        cur = await self.adapter.execute(ddl)
        try:
            await self.adapter.auto_commit()
        finally:
            await cur.close()

    async def list_indexes(self, table: str | None = None) -> list[dict[str, Any]]:
        """List indexes in the database.

        Args:
            table: Optional table name to filter by. If None, lists all indexes.

        Returns:
            List of dicts with index info: name, table, sql, unique.
        """
        if table:
            query = """
                SELECT name, tbl_name, sql
                FROM sqlite_master
                WHERE type = 'index' AND tbl_name = ? AND name NOT LIKE 'sqlite_%'
                ORDER BY name;
            """
            cur = await self.adapter.execute(query, [table])
        else:
            query = """
                SELECT name, tbl_name, sql
                FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
                ORDER BY tbl_name, name;
            """
            cur = await self.adapter.execute(query)

        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        await self.adapter.auto_commit()

        indexes = []
        for row in rows:
            name = row[0]
            tbl_name = row[1]
            sql = row[2] or ""
            indexes.append(
                {
                    "name": name,
                    "table": tbl_name,
                    "sql": sql,
                    "unique": "UNIQUE" in sql.upper() if sql else False,
                }
            )
        return indexes

    async def index_exists(self, name: str) -> bool:
        """Check if an index exists by name.

        Args:
            name: Index name to check.

        Returns:
            True if the index exists, False otherwise.
        """
        cur = await self.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?;", [name]
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        return row is not None

    # ---- versioned (optimistic locking) helpers ----
    async def _ensure_versioned_table(self, table: str) -> None:
        """Ensure the target table exists and has a ``_version`` column.

        This upgrades an existing non-versioned table by adding the column.
        Uses a cache to avoid repeated PRAGMA calls.

        Args:
            table: Table name.
        """
        table = validate_table_name(table)
        if self._is_versioned(table):
            return
        await self._ensure_table(table)
        cols = [row[1] for row in await _afetch_all(self.adapter, _columns_sql(table), [])]
        await self.adapter.auto_commit()
        if self._promoted_of(table) is None and not _promoted_in(cols):
            self._remember(table, promoted=[])  # promoted columns are remembered once checked
        if "_version" not in cols:
            cur2 = await self.adapter.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "_version" INTEGER NOT NULL DEFAULT 0;'
            )
            try:
                await self.adapter.auto_commit()
            finally:
                await cur2.close()
        self._remember(table)

    async def upsert_with_version(
        self, table: str, _id: Optional[int], doc: dict[str, Any], expected_version: Optional[int]
    ) -> tuple[int, int]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB.upsert_with_version`."""
        table = validate_table_name(table)
        return await self.upsert_with_version_promoted(table, _id, doc, expected_version, await self._columns_of(table))

    async def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        rows, columns = await self._read_rows(table, [_id], versioned=True)
        return _row_documents(rows, columns)[0] if rows else None

    async def query(self, table: str):
        """Convenience: return an AsyncSQLerQuery bound to this adapter."""
        from sqler.query.async_query import AsyncSQLerQuery

        return AsyncSQLerQuery(table=validate_table_name(table), adapter=self.adapter)

    def transaction(self) -> "AsyncTransaction":
        """Return a context manager for explicit transactions.

        Usage::

            async with db.transaction():
                await db.insert_document("users", {"name": "Alice"})
                await db.insert_document("users", {"name": "Bob"})
                # commits on exit, rolls back on exception

        Returns:
            AsyncTransaction: Async context manager for transaction scope.
        """
        return AsyncTransaction(self.adapter, self)

    async def __aenter__(self):
        """Enter async context manager; begin transaction."""
        await self._begin_level()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager; commit, or roll back while the caller's exception leaves."""
        await self._end_level(exc_type is None, leaving=exc_val)
        return False

    async def _begin_level(self) -> None:
        await self.adapter.begin_transaction()
        self._open.set((*self._open.get(), _Level(set(), {}, self.adapter._depth())))

    async def _end_level(self, commit: bool, *, leaving: Optional[BaseException] = None) -> None:
        """End this task's innermost level; the async twin of ``SQLerDB._end_level``.

        ``leaving``: the caller's exception on its way out (see ``sqler_db._end_transaction``).
        """
        levels = self._open.get()
        level = levels[-1] if levels else None  # none: begun in another task; nothing to merge
        self._open.set(levels[:-1])
        try:
            await self.adapter.end_transaction(commit=commit)
        except Exception as ended:
            _ended_while_leaving(ended, leaving)
            return
        if commit and level is not None and (above := self._learned()) is not None:
            above[0].update(level.versioned)
            above[1].update(level.promoted)


class AsyncTransaction:
    """Async context manager for explicit database transactions.

    Uses the adapter's transaction tracking to ensure that nested operations
    (like model.save()) respect the transaction boundary and don't auto-commit.
    """

    def __init__(self, adapter, db: AsyncSQLerDB):
        self.adapter = adapter
        self._db = db
        self._active = False

    async def _end(self, commit: bool, leaving: Optional[BaseException] = None) -> None:
        self._active = False
        await self._db._end_level(commit, leaving=leaving)

    async def __aenter__(self):
        """Begin the transaction."""
        await self._db._begin_level()
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Commit on success, rollback on exception.

        When the block raised and the transaction was rolled back for a reason of its own
        (a nested end that failed, or the transaction it was nested in had already ended),
        the block's exception goes on with that sentence as a note.
        """
        if self._active:
            await self._end(exc_type is None, leaving=exc_val)
        return False

    async def commit(self):
        """Explicitly commit the transaction."""
        if self._active:
            await self._end(True)

    async def rollback(self):
        """Explicitly rollback the transaction."""
        if self._active:
            await self._end(False)
