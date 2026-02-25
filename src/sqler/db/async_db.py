import json
from typing import Any, Optional

from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.exceptions import StaleVersionError
from sqler.utils import validate_field_name, validate_identifier, validate_table_name


class AsyncSQLerDB:
    """Async document store for JSON blobs on SQLite."""

    @classmethod
    def in_memory(cls, shared: bool = True, *, name: Optional[str] = None) -> "AsyncSQLerDB":
        adapter = AsyncSQLiteAdapter.in_memory(shared=shared, name=name)
        return cls(adapter)

    @classmethod
    def on_disk(cls, path: str = "sqler.db") -> "AsyncSQLerDB":
        adapter = AsyncSQLiteAdapter.on_disk(path)
        return cls(adapter)

    def __init__(self, adapter: AsyncSQLiteAdapter):
        self.adapter = adapter
        # Cache of tables already ensured to be versioned
        self._versioned_tables: set[str] = set()
        # promoted columns per table: table -> list of column names
        self._promoted_columns: dict[str, list[str]] = {}

    async def connect(self) -> None:
        await self.adapter.connect()

    async def close(self) -> None:
        await self.adapter.close()

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

    async def _ensure_table_with_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
    ) -> None:
        """Create or upgrade a table with promoted columns (async)."""
        table = validate_table_name(table)
        checks = checks or {}
        for col_name in promoted:
            validate_identifier(col_name)

        # Check if table exists
        cur = await self.adapter.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
            [table],
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        exists = row is not None

        if not exists:
            col_defs = ["_id INTEGER PRIMARY KEY AUTOINCREMENT", "data JSON NOT NULL"]
            for col_name, col_def in promoted.items():
                col_defs.append(f"{col_name} {col_def}")
            for chk_name, chk_expr in checks.items():
                col_defs.append(f"CHECK ({chk_expr})")
            ddl = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(col_defs)});"
            await self.adapter.execute(ddl)
            await self.adapter.auto_commit()
        else:
            cur = await self.adapter.execute(f'PRAGMA table_info("{table}");')
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
            existing_cols = {r[1] for r in rows}
            for col_name, col_def in promoted.items():
                if col_name not in existing_cols:
                    await self.adapter.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN {col_name} {col_def};'
                    )
            await self.adapter.auto_commit()

        self._promoted_columns[table] = list(promoted.keys())

    async def upsert_document_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        promoted_fields: list[str],
    ) -> int:
        """Insert or update, splitting promoted fields into real columns (async)."""
        promoted_vals = {}
        json_doc = {}
        for k, v in doc.items():
            if k in promoted_fields:
                promoted_vals[k] = v
            else:
                json_doc[k] = v

        payload = json.dumps(json_doc)

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
        await self._ensure_versioned_table(table)

        promoted_vals = {}
        json_doc = {}
        for k, v in doc.items():
            if k in promoted_fields:
                promoted_vals[k] = v
            else:
                json_doc[k] = v

        payload = json.dumps(json_doc)

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
            f"WHERE _id = ? AND _version = ? AND COALESCE(json_extract(data, '$._version'), ?) = ?;"
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
        await self._ensure_table(table)
        payload = json.dumps(doc)
        cur = await self.adapter.execute(f"INSERT INTO {table} (data) VALUES (json(?));", [payload])
        try:
            await self.adapter.auto_commit()
            last_id = cur.lastrowid  # type: ignore[attr-defined]
        finally:
            await cur.close()
        return last_id

    async def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        await self._ensure_table(table)
        payload = json.dumps(doc)
        if _id is None:
            return await self.insert_document(table, doc)
        cur = await self.adapter.execute(
            f"UPDATE {table} SET data = json(?) WHERE _id = ?;", [payload, _id]
        )
        try:
            await self.adapter.auto_commit()
        finally:
            await cur.close()
        return _id

    async def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        await self._ensure_table(table)
        promoted = self._promoted_columns.get(table, [])
        if promoted:
            extra_cols = ", ".join(promoted)
            cur = await self.adapter.execute(
                f"SELECT _id, data, {extra_cols} FROM {table} WHERE _id = ?;", [_id]
            )
        else:
            cur = await self.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [_id])
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        if promoted:
            for i, col_name in enumerate(promoted):
                obj[col_name] = row[2 + i]
        return obj

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
        await self._ensure_table(table)
        placeholders = ",".join("?" for _ in ids)
        cur = await self.adapter.execute(
            f"SELECT _id, data FROM {table} WHERE _id IN ({placeholders});",
            list(ids),
        )
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        # Build lookup dict for ordering
        by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            obj = json.loads(row[1])
            obj["_id"] = row[0]
            by_id[row[0]] = obj
        # Return in input order, skipping missing
        return [by_id[i] for i in ids if i in by_id]

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
        await self._ensure_table(table)
        return await self._insert_many_chunked(table, docs, cols=["data"], versioned=False)

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
        cols = ["data"] + promoted_fields
        return await self._insert_many_chunked(table, docs, cols=cols, versioned=False, promoted_fields=promoted_fields)

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
        await self._ensure_versioned_table(table)
        ids = await self._insert_many_chunked(table, docs, cols=["data"], versioned=True)
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
        await self._ensure_versioned_table(table)
        cols = ["data"] + promoted_fields
        ids = await self._insert_many_chunked(table, docs, cols=cols, versioned=True, promoted_fields=promoted_fields)
        return [(id_, 0) for id_ in ids]

    async def _insert_many_chunked(
        self,
        table: str,
        docs: list[dict[str, Any]],
        *,
        cols: list[str],
        versioned: bool,
        promoted_fields: list[str] | None = None,
    ) -> list[int]:
        """Internal: chunked multi-row INSERT with transaction wrapping.

        Builds ``INSERT INTO t (col1, ...) VALUES (...), (...), ...`` statements,
        chunking to stay within SQLite's 999-parameter limit.

        Args:
            table: Validated table name.
            docs: Documents to insert.
            cols: Column names for the INSERT.
            versioned: Whether to include ``_version`` column (literal 0).
            promoted_fields: If set, split these keys from each doc into columns.

        Returns:
            list[int]: Assigned ``_id`` for each document.
        """
        promoted_fields = promoted_fields or []
        # params_per_row: 1 for json(?) + 1 per promoted field
        params_per_row = 1 + len(promoted_fields)
        max_rows_per_chunk = max(1, 999 // params_per_row)

        all_ids: list[int] = []
        all_col_names = list(cols)
        if versioned:
            all_col_names = ["data", "_version"] + promoted_fields

        async with self.transaction():
            for offset in range(0, len(docs), max_rows_per_chunk):
                chunk = docs[offset : offset + max_rows_per_chunk]
                params: list[Any] = []
                value_groups: list[str] = []

                for doc in chunk:
                    if promoted_fields:
                        promoted_vals = {k: doc[k] for k in promoted_fields if k in doc}
                        json_doc = {k: v for k, v in doc.items() if k not in promoted_fields}
                    else:
                        json_doc = doc
                        promoted_vals = {}

                    payload = json.dumps(json_doc)
                    row_placeholders = ["json(?)"]
                    params.append(payload)

                    if versioned:
                        row_placeholders.append("0")  # literal, no param

                    for pf in promoted_fields:
                        row_placeholders.append("?")
                        params.append(promoted_vals.get(pf))

                    value_groups.append(f"({', '.join(row_placeholders)})")

                col_list = ", ".join(all_col_names)
                values_sql = ", ".join(value_groups)
                sql = f"INSERT INTO {table} ({col_list}) VALUES {values_sql};"

                cur = await self.adapter.execute(sql, params)
                try:
                    last_id = cur.lastrowid
                finally:
                    await cur.close()

                chunk_size = len(chunk)
                first_id = last_id - chunk_size + 1
                all_ids.extend(range(first_id, last_id + 1))

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
        await self._ensure_table(table)
        assigned: list[int] = []
        for doc in docs:
            doc_id = doc.get("_id")
            payload_dict = {k: v for k, v in doc.items() if k != "_id"}
            payload = json.dumps(payload_dict)
            if doc_id is None:
                cur = await self.adapter.execute(
                    f"INSERT INTO {table} (data) VALUES (json(?));",
                    [payload],
                )
                try:
                    new_id = int(cur.lastrowid)  # type: ignore[attr-defined]
                finally:
                    await cur.close()
                assigned.append(new_id)
                doc["_id"] = new_id
            else:
                cur = await self.adapter.execute(
                    f"INSERT INTO {table} (_id, data) VALUES (?, json(?)) "
                    "ON CONFLICT(_id) DO UPDATE SET data = excluded.data;",
                    [int(doc_id), payload],
                )
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
        # Fast path: check cache first
        if table in self._versioned_tables:
            return
        await self._ensure_table(table)
        cur = await self.adapter.execute(f'PRAGMA table_info("{table}");')
        try:
            cols = [row[1] for row in await cur.fetchall()]
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if "_version" not in cols:
            cur2 = await self.adapter.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "_version" INTEGER NOT NULL DEFAULT 0;'
            )
            try:
                await self.adapter.auto_commit()
            finally:
                await cur2.close()
        # Update cache
        self._versioned_tables.add(table)

    async def upsert_with_version(
        self, table: str, _id: Optional[int], doc: dict[str, Any], expected_version: Optional[int]
    ) -> tuple[int, int]:
        await self._ensure_versioned_table(table)
        payload = json.dumps(doc)
        if _id is None:
            cur = await self.adapter.execute(
                f"INSERT INTO {table} (data, _version) VALUES (json(?), 0);",
                [payload],
            )
            try:
                await self.adapter.auto_commit()
                last_id = cur.lastrowid  # type: ignore[attr-defined]
            finally:
                await cur.close()
            return last_id, 0
        if expected_version is None:
            raise ValueError("expected_version required for update")
        cur = await self.adapter.execute(
            f"UPDATE {table} SET data = json(?), _version = _version + 1 "
            f"WHERE _id = ? AND _version = ? AND COALESCE(json_extract(data, '$._version'), ?) = ?;",
            [payload, _id, expected_version, expected_version, expected_version],
        )
        try:
            rowcount = cur.rowcount
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if rowcount == 0:
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

    async def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        await self._ensure_versioned_table(table)
        promoted = self._promoted_columns.get(table, [])
        if promoted:
            extra_cols = ", " + ", ".join(promoted)
        else:
            extra_cols = ""
        cur = await self.adapter.execute(
            f"SELECT _id, data, _version{extra_cols} FROM {table} WHERE _id = ?;",
            [_id],
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        obj["_version"] = row[2]
        if promoted:
            for i, col_name in enumerate(promoted):
                obj[col_name] = row[3 + i]
        return obj

    async def query(self, table: str):
        """Convenience: return an AsyncSQLerQuery bound to this adapter."""
        from sqler.query.async_query import AsyncSQLerQuery

        await self._ensure_table(table)
        return AsyncSQLerQuery(table=table, adapter=self.adapter)

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
        return AsyncTransaction(self.adapter)

    async def __aenter__(self):
        """Enter async context manager; begin transaction."""
        await self.adapter.begin_transaction()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager; commit or rollback."""
        await self.adapter.end_transaction(commit=(exc_type is None))
        return False


class AsyncTransaction:
    """Async context manager for explicit database transactions.

    Uses the adapter's transaction tracking to ensure that nested operations
    (like model.save()) respect the transaction boundary and don't auto-commit.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self._active = False

    async def __aenter__(self):
        """Begin the transaction."""
        await self.adapter.begin_transaction()
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Commit on success, rollback on exception."""
        if not self._active:
            return False
        self._active = False
        await self.adapter.end_transaction(commit=(exc_type is None))
        return False

    async def commit(self):
        """Explicitly commit the transaction."""
        if self._active:
            await self.adapter.end_transaction(commit=True)
            self._active = False

    async def rollback(self):
        """Explicitly rollback the transaction."""
        if self._active:
            await self.adapter.end_transaction(commit=False)
            self._active = False
