import json
from typing import Any, Optional

from sqler.adapter.asynchronous import AsyncSQLiteAdapter
from sqler.exceptions import StaleVersionError
from sqler.utils import validate_table_name


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

    async def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        await self._ensure_table(table)
        payload = json.dumps(doc)
        cur = await self.adapter.execute(f"INSERT INTO {table} (data) VALUES (json(?));", [payload])
        await self.adapter.auto_commit()
        last_id = cur.lastrowid  # type: ignore[attr-defined]
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
        await self.adapter.auto_commit()
        await cur.close()
        return _id

    async def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        await self._ensure_table(table)
        cur = await self.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [_id])
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
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
        rows = await cur.fetchall()
        await cur.close()
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
        await self.adapter.auto_commit()
        await cur.close()

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
                new_id = int(cur.lastrowid)  # type: ignore[attr-defined]
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

        When the result set exposes a ``data`` column alongside ``_id``, the
        JSON payload is decoded and merged with ``_id``. For ad-hoc projections
        (e.g. ``SELECT _id``) the method returns simple dicts keyed by the
        selected columns so callers can hydrate with :meth:`AsyncSQLerModel.from_id`.

        Args:
            query: SQL SELECT statement.
            params: Optional parameter list.

        Returns:
            list[dict[str, Any]]: Decoded documents with ``_id`` included.
        """
        cur = await self.adapter.execute(query, params or [])
        rows = await cur.fetchall()
        await cur.close()
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
        idx_name = name or f"idx_{table}_{field.replace('.', '_')}"
        unique_sql = "UNIQUE" if unique else ""
        expr = f"json_extract(data, '$.{field}')" if not field.startswith("_") else field
        where_sql = f"WHERE {where}" if where else ""
        ddl = f"CREATE {unique_sql} INDEX IF NOT EXISTS {idx_name} ON {table} ({expr}) {where_sql};"
        cur = await self.adapter.execute(ddl)
        await self.adapter.auto_commit()
        await cur.close()

    async def drop_index(self, name: str) -> None:
        """Drop an index by name.

        Args:
            name: Index name.
        """
        ddl = f"DROP INDEX IF EXISTS {name};"
        cur = await self.adapter.execute(ddl)
        await self.adapter.auto_commit()
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

        rows = await cur.fetchall()
        await cur.close()

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
        row = await cur.fetchone()
        await cur.close()
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
        cols = [row[1] for row in await cur.fetchall()]
        await cur.close()
        if "_version" not in cols:
            cur2 = await self.adapter.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "_version" INTEGER NOT NULL DEFAULT 0;'
            )
            await self.adapter.auto_commit()
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
            await self.adapter.auto_commit()
            last_id = cur.lastrowid  # type: ignore[attr-defined]
            await cur.close()
            return last_id, 0
        if expected_version is None:
            raise ValueError("expected_version required for update")
        cur = await self.adapter.execute(
            f"UPDATE {table} SET data = json(?), _version = _version + 1 "
            f"WHERE _id = ? AND _version = ? AND COALESCE(json_extract(data, '$._version'), ?) = ?;",
            [payload, _id, expected_version, expected_version, expected_version],
        )
        await self.adapter.auto_commit()
        await cur.close()
        # Check changes() to confirm update actually happened
        ch = await self.adapter.execute("SELECT changes();")
        row = await ch.fetchone()
        await ch.close()
        if not row or int(row[0]) == 0:
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

    async def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        await self._ensure_versioned_table(table)
        cur = await self.adapter.execute(
            f"SELECT _id, data, _version FROM {table} WHERE _id = ?;",
            [_id],
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        obj["_version"] = row[2]
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
