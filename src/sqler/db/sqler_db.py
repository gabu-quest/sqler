import json
import threading
from typing import Any, Optional

from sqler.adapter import SQLiteAdapter
from sqler.exceptions import StaleVersionError
from sqler.query import SQLerQuery
from sqler.utils import validate_table_name


class SQLerDB:
    """Document store for JSON blobs on SQLite.

    SQLerDB persists Python dicts as JSON in a table with schema
    ``(_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)``. The API
    is table-agnostic: pass the table name on each call. Tables are created
    on demand as you insert or query.
    """

    @classmethod
    def in_memory(cls, shared: bool = True, *, name: Optional[str] = None) -> "SQLerDB":
        """Create a SQLerDB backed by an in-memory SQLite database.

        Args:
            shared: When True, use a shared-cache URI so multiple connections
                see the same in-memory database.
            name: Optional shared cache identifier; use the same name to
                connect multiple adapters to a shared in-memory database.

        Returns:
            SQLerDB: Connected database instance.
        """
        adapter = SQLiteAdapter.in_memory(shared=shared, name=name)
        return cls(adapter)

    @classmethod
    def on_disk(cls, path: str = "sqler.db") -> "SQLerDB":
        """Create a SQLerDB backed by a persistent file on disk.

        Args:
            path: Path to the SQLite database file (created if missing).

        Returns:
            SQLerDB: Connected database instance.
        """
        adapter = SQLiteAdapter.on_disk(path)
        return cls(adapter)

    def __init__(self, adapter: SQLiteAdapter):
        self.adapter = adapter
        self.adapter.connect()
        # serialize DDL operations (e.g., adding _version) across threads
        self._ddl_lock = threading.RLock()
        # cache of tables already ensured to be versioned
        self._versioned_tables: set[str] = set()

    def _ensure_table(self, table: str) -> None:
        """Create the target table if it doesn't exist.

        Args:
            table: Table name to ensure.
        """
        table = validate_table_name(table)
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            data JSON NOT NULL
        );
        """
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        """Insert a document.

        Args:
            table: Table name.
            doc: JSON-serializable dict to persist.

        Returns:
            int: Newly assigned ``_id``.
        """
        self._ensure_table(table)
        payload = json.dumps(doc)
        cursor = self.adapter.execute(f"INSERT INTO {table} (data) VALUES (json(?));", [payload])
        self.adapter.auto_commit()
        return cursor.lastrowid

    def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        """Insert or update a document.

        Args:
            table: Table name.
            _id: Existing id to update, or ``None`` to insert.
            doc: Document to write.

        Returns:
            int: The existing or newly assigned ``_id``.
        """
        self._ensure_table(table)
        payload = json.dumps(doc)
        if _id is None:
            return self.insert_document(table, doc)
        self.adapter.execute(f"UPDATE {table} SET data = json(?) WHERE _id = ?;", [payload, _id])
        self.adapter.auto_commit()
        return _id

    def bulk_upsert(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
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
        self._ensure_table(table)
        assigned: list[int] = []
        with self.adapter as adapter:
            for doc in docs:
                doc_id = doc.get("_id")
                payload_dict = {k: v for k, v in doc.items() if k != "_id"}
                payload = json.dumps(payload_dict)
                if doc_id is None:
                    cursor = adapter.execute(
                        f"INSERT INTO {table} (data) VALUES (json(?));",
                        [payload],
                    )
                    new_id = int(cursor.lastrowid)
                    assigned.append(new_id)
                    doc["_id"] = new_id
                else:
                    adapter.execute(
                        f"INSERT INTO {table} (_id, data) VALUES (?, json(?)) "
                        "ON CONFLICT(_id) DO UPDATE SET data = excluded.data;",
                        [int(doc_id), payload],
                    )
                    assigned.append(int(doc_id))
        return assigned

    def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by id.

        Args:
            table: Table name.
            _id: Row id to fetch.

        Returns:
            dict | None: Decoded document with ``_id`` merged in, or ``None``
            if not found.
        """
        self._ensure_table(table)
        cur = self.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [_id])
        row = cur.fetchone()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        return obj

    def find_documents(self, table: str, ids: list[int]) -> list[dict[str, Any]]:
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
        self._ensure_table(table)
        placeholders = ",".join("?" for _ in ids)
        cur = self.adapter.execute(
            f"SELECT _id, data FROM {table} WHERE _id IN ({placeholders});",
            list(ids),
        )
        rows = cur.fetchall()
        # Build lookup dict for ordering
        by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            obj = json.loads(row[1])
            obj["_id"] = row[0]
            by_id[row[0]] = obj
        # Return in input order, skipping missing
        return [by_id[i] for i in ids if i in by_id]

    def delete_document(self, table: str, _id: int) -> None:
        """Delete a document by id.

        Args:
            table: Table name.
            _id: Row id to delete.
        """
        self._ensure_table(table)
        self.adapter.execute(f"DELETE FROM {table} WHERE _id = ?;", [_id])
        self.adapter.auto_commit()

    def execute_sql(self, query: str, params: Optional[list[Any]] = None) -> list[dict[str, Any]]:
        """Run a custom SELECT and return lightweight row mappings.

        When the result set exposes a ``data`` column alongside ``_id``, the
        JSON payload is decoded and merged with ``_id``. For ad-hoc projections
        (e.g. ``SELECT _id``) the method returns simple dicts keyed by the
        selected columns so callers can hydrate with :meth:`SQLerModel.from_id`.

        Args:
            query: SQL SELECT statement.
            params: Optional parameter list.

        Returns:
            list[dict[str, Any]]: Decoded documents with ``_id`` included.
        """
        cursor = self.adapter.execute(query, params or [])
        rows = cursor.fetchall()
        docs: list[dict[str, Any]] = []
        for row in rows:
            mapping = None
            try:
                mapping = row.keys()  # type: ignore[attr-defined]
            except AttributeError:
                mapping = None  # Row doesn't support dict-like access
            if mapping:
                keys = list(mapping)
                if "data" in keys:
                    raw = row["data"]
                    obj = json.loads(raw)
                    if "_id" in keys:
                        obj["_id"] = int(row["_id"])
                    docs.append(obj)
                    continue
                if keys == ["_id"]:
                    docs.append({"_id": int(row["_id"])})
                    continue
                docs.append({k: row[k] for k in keys})
                continue
            if len(row) >= 2:
                obj = json.loads(row[1])
                obj["_id"] = int(row[0])
                docs.append(obj)
            elif len(row) == 1:
                docs.append({"_id": int(row[0])})
            else:
                docs.append({})
        return docs

    def query(self, table: str) -> SQLerQuery:
        """Return a SQLerQuery bound to this DB's adapter.

        Args:
            table: Table name.

        Returns:
            SQLerQuery: Query object you can chain and execute.
        """
        self._ensure_table(table)
        return SQLerQuery(table=table, adapter=self.adapter)

    def close(self):
        """Close the underlying adapter connection."""
        self.adapter.close()

    def connect(self):
        """Connect the underlying adapter if not already connected."""
        self.adapter.connect()

    def transaction(self) -> "Transaction":
        """Return a context manager for explicit transactions.

        Usage::

            with db.transaction():
                db.insert_document("users", {"name": "Alice"})
                db.insert_document("users", {"name": "Bob"})
                # commits on exit, rolls back on exception

        Returns:
            Transaction: Context manager for transaction scope.
        """
        return Transaction(self.adapter)

    def create_index(
        self,
        table: str,
        field: str,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ):
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
        self._ensure_table(table)
        idx_name = name or f"idx_{table}_{field.replace('.', '_')}"
        unique_sql = "UNIQUE" if unique else ""
        expr = f"json_extract(data, '$.{field}')" if not field.startswith("_") else field
        where_sql = f"WHERE {where}" if where else ""
        ddl = f"CREATE {unique_sql} INDEX IF NOT EXISTS {idx_name} ON {table} ({expr}) {where_sql};"
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def drop_index(self, name: str):
        """Drop an index by name.

        Args:
            name: Index name.
        """
        ddl = f"DROP INDEX IF EXISTS {name};"
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def list_indexes(self, table: Optional[str] = None) -> list[dict[str, Any]]:
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
            cur = self.adapter.execute(query, [table])
        else:
            query = """
                SELECT name, tbl_name, sql
                FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
                ORDER BY tbl_name, name;
            """
            cur = self.adapter.execute(query)

        indexes = []
        for row in cur.fetchall():
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

    def index_exists(self, name: str) -> bool:
        """Check if an index exists by name.

        Args:
            name: Index name to check.

        Returns:
            True if the index exists, False otherwise.
        """
        cur = self.adapter.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?;", [name]
        )
        return cur.fetchone() is not None

    def __enter__(self):
        """Enter context manager; begin transaction."""
        self.adapter.begin_transaction()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager; commit or rollback."""
        self.adapter.end_transaction(commit=(exc_type is None))
        return False

    # ---- versioned (optimistic locking) helpers ----

    def _ensure_versioned_table(self, table: str) -> None:
        """Ensure the target table exists and has a ``_version`` column.

        This upgrades an existing non-versioned table by adding the column.

        Args:
            table: Table name.
        """
        self._ensure_table(table)
        # fast path: check without lock
        import sqlite3 as _sqlite3

        cur = self.adapter.execute(f'PRAGMA table_info("{table}");')
        rows = cur.fetchall()
        cols = set()
        for row in rows:
            try:
                if isinstance(row, _sqlite3.Row):
                    name = row["name"]
                else:
                    name = row[1]
            except (KeyError, IndexError, TypeError):
                # Skip malformed rows
                continue
            cols.add(name)
        if "_version" in cols:
            # mark cache and return
            self._versioned_tables.add(table)
            return
        # serialize DDL; re-check inside lock
        with self._ddl_lock:
            cur = self.adapter.execute(f'PRAGMA table_info("{table}");')
            rows = cur.fetchall()
            cols = set()
            for row in rows:
                try:
                    if isinstance(row, _sqlite3.Row):
                        name = row["name"]
                    else:
                        name = row[1]
                except (KeyError, IndexError, TypeError):
                    # Skip malformed rows
                    continue
                cols.add(name)
            if "_version" not in cols:
                self.adapter.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "_version" INTEGER NOT NULL DEFAULT 0;'
                )
                self.adapter.auto_commit()
            # update cache regardless
            self._versioned_tables.add(table)

    def upsert_with_version(
        self, table: str, _id: Optional[int], doc: dict[str, Any], expected_version: Optional[int]
    ) -> tuple[int, int]:
        """Insert or update a document with optimistic locking.

        On insert, ``_version`` is set to 0. On update, the row is updated only
        if the stored version matches ``expected_version``; on success, ``_version``
        is incremented by 1.

        Args:
            table: Table name.
            _id: Existing row id for update; ``None`` to insert.
            doc: Document to write.
            expected_version: Version expected by the caller for update; ignored on insert.

        Returns:
            tuple[int, int]: The row id and the new version after the operation.

        Raises:
            ValueError: If updating with ``_id`` but ``expected_version`` is None.
            RuntimeError: On stale version conflicts (no rows updated).
        """
        if table not in self._versioned_tables:
            self._ensure_versioned_table(table)
        payload = json.dumps(doc)
        if _id is None:
            cur = self.adapter.execute(
                f"INSERT INTO {table} (data, _version) VALUES (json(?), 0);",
                [payload],
            )
            self.adapter.auto_commit()
            return cur.lastrowid, 0
        if expected_version is None:
            raise ValueError("expected_version required for update")
        # Acquire write lock early to reduce live-lock under contention (only if not in transaction)
        if not self.adapter.in_transaction:
            try:
                self.adapter.execute("BEGIN IMMEDIATE;")
            except (RuntimeError, ValueError):
                # tolerate if already in a transaction
                pass
        cur = self.adapter.execute(
            f"UPDATE {table} SET data = json(?), _version = _version + 1 "
            f"WHERE _id = ? AND _version = ? AND COALESCE(json_extract(data, '$._version'), ?) = ?;",
            [payload, _id, expected_version, expected_version, expected_version],
        )
        self.adapter.auto_commit()
        rc = getattr(cur, "rowcount", -1)
        if rc <= 0:
            # treat non-positive as conflict
            # double-check via select
            _ = self.adapter.execute(
                f"SELECT _version FROM {table} WHERE _id = ?;", [_id]
            ).fetchone()
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

    def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by id including ``_version`` in the result dict.

        Args:
            table: Table name.
            _id: Row id to fetch.

        Returns:
            dict | None: Decoded document with ``_id`` and ``_version`` keys, or None.
        """
        self._ensure_versioned_table(table)
        cur = self.adapter.execute(f"SELECT _id, data, _version FROM {table} WHERE _id = ?;", [_id])
        row = cur.fetchone()
        if not row:
            return None
        try:
            # Prefer name-based access for stability
            obj = json.loads(row["data"])  # type: ignore[index]
            obj["_id"] = row["_id"]  # type: ignore[index]
            obj["_version"] = row["_version"]  # type: ignore[index]
        except (KeyError, TypeError):
            # Fallback to index-based access
            obj = json.loads(row[1])
            obj["_id"] = row[0]
            obj["_version"] = row[2]
        return obj


class Transaction:
    """Context manager for explicit database transactions.

    When inside a Transaction context, model.save() and other document
    operations will NOT auto-commit. The transaction commits only when
    the context exits successfully, or rolls back on exception.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self._active = False

    def __enter__(self):
        """Begin the transaction."""
        self.adapter.begin_transaction()
        self._active = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Commit on success, rollback on exception."""
        if not self._active:
            return False
        self._active = False
        self.adapter.end_transaction(commit=(exc_type is None))
        return False

    def commit(self):
        """Explicitly commit the transaction."""
        if self._active:
            self.adapter.end_transaction(commit=True)
            self._active = False

    def rollback(self):
        """Explicitly rollback the transaction."""
        if self._active:
            self.adapter.end_transaction(commit=False)
            self._active = False
