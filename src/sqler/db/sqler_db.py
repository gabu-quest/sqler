import json
import threading
from typing import Any, Optional

from sqler.adapter import SQLiteAdapter
from sqler.exceptions import StaleVersionError
from sqler.query import SQLerQuery
from sqler.utils import (
    validate_check_expression,
    validate_column_def,
    validate_column_name,
    validate_field_name,
    validate_identifier,
    validate_table_name,
)


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
        # promoted columns per table: table -> list of column names
        self._promoted_columns: dict[str, list[str]] = {}

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

    def _ensure_table_with_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
    ) -> None:
        """Create or upgrade a table with promoted columns.

        Promoted columns are real SQLite columns that live alongside the JSON
        ``data`` blob. They get CHECK constraints on fresh tables and are
        added via ALTER TABLE on existing ones.

        Args:
            table: Table name.
            promoted: ``{column_name: column_def}`` mapping, e.g.
                ``{"status": "TEXT NOT NULL DEFAULT 'pending'"}``.
            checks: Optional ``{name: expression}`` CHECK constraints.
        """
        import sqlite3 as _sqlite3

        table = validate_table_name(table)
        checks = checks or {}
        for col_name, col_def in promoted.items():
            validate_column_name(col_name)
            validate_column_def(col_def)
        for chk_expr in checks.values():
            validate_check_expression(chk_expr)

        with self._ddl_lock:
            # Check if table exists
            cur = self.adapter.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
                [table],
            )
            exists = cur.fetchone() is not None

            if not exists:
                # Build CREATE TABLE with promoted columns + checks
                col_defs = ["_id INTEGER PRIMARY KEY AUTOINCREMENT", "data JSON NOT NULL"]
                for col_name, col_def in promoted.items():
                    col_defs.append(f"{col_name} {col_def}")
                for chk_name, chk_expr in checks.items():
                    col_defs.append(f"CHECK ({chk_expr})")
                ddl = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(col_defs)});"
                self.adapter.execute(ddl)
                self.adapter.auto_commit()
            else:
                # Get existing columns
                cur = self.adapter.execute(f'PRAGMA table_info("{table}");')
                rows = cur.fetchall()
                existing_cols: set[str] = set()
                for row in rows:
                    try:
                        if isinstance(row, _sqlite3.Row):
                            existing_cols.add(row["name"])
                        else:
                            existing_cols.add(row[1])
                    except (KeyError, IndexError, TypeError):
                        continue

                # Add missing promoted columns
                for col_name, col_def in promoted.items():
                    if col_name not in existing_cols:
                        self.adapter.execute(
                            f'ALTER TABLE "{table}" ADD COLUMN {col_name} {col_def};'
                        )
                self.adapter.auto_commit()

            self._promoted_columns[table] = list(promoted.keys())

    def upsert_document_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        promoted_fields: list[str],
    ) -> int:
        """Insert or update a document, splitting promoted fields into real columns.

        Args:
            table: Table name.
            _id: Existing id to update, or ``None`` to insert.
            doc: Full document payload.
            promoted_fields: Keys to extract from doc into real columns.

        Returns:
            int: The existing or newly assigned ``_id``.
        """
        # Split promoted from JSON
        promoted_vals = {}
        json_doc = {}
        for k, v in doc.items():
            if k in promoted_fields:
                promoted_vals[k] = v
            else:
                json_doc[k] = v

        payload = json.dumps(json_doc)

        if _id is None:
            # INSERT
            cols = ["data"] + list(promoted_vals.keys())
            placeholders = ["json(?)"] + ["?" for _ in promoted_vals]
            params = [payload] + list(promoted_vals.values())
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)});"
            cursor = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
            return cursor.lastrowid
        else:
            # UPDATE
            set_parts = ["data = json(?)"]
            params: list[Any] = [payload]
            for col, val in promoted_vals.items():
                set_parts.append(f"{col} = ?")
                params.append(val)
            params.append(_id)
            sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE _id = ?;"
            self.adapter.execute(sql, params)
            self.adapter.auto_commit()
            return _id

    def upsert_with_version_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        expected_version: Optional[int],
        promoted_fields: list[str],
    ) -> tuple[int, int]:
        """Insert or update with optimistic locking, splitting promoted columns.

        Args:
            table: Table name.
            _id: Existing row id for update; ``None`` to insert.
            doc: Document to write.
            expected_version: Version expected by the caller.
            promoted_fields: Keys to extract into real columns.

        Returns:
            tuple[int, int]: The row id and new version.
        """
        if table not in self._versioned_tables:
            self._ensure_versioned_table(table)

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
            cur = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
            return cur.lastrowid, 0

        if expected_version is None:
            raise ValueError("expected_version required for update")

        # Acquire write lock early
        if not self.adapter.in_transaction:
            try:
                self.adapter.execute("BEGIN IMMEDIATE;")
            except (RuntimeError, ValueError):
                pass

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
        cur = self.adapter.execute(sql, params_list)
        self.adapter.auto_commit()
        rc = getattr(cur, "rowcount", -1)
        if rc <= 0:
            self.adapter.execute(
                f"SELECT _version FROM {table} WHERE _id = ?;", [_id]
            ).fetchone()
            raise StaleVersionError("Stale version: update rejected")
        return _id, expected_version + 1

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

    # ---- batch insert helpers ----

    def insert_many(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
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
        self._ensure_table(table)
        return self._insert_many_chunked(table, docs, cols=["data"], versioned=False)

    def insert_many_promoted(
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
        for field in promoted_fields:
            validate_identifier(field)
        cols = ["data"] + promoted_fields
        return self._insert_many_chunked(table, docs, cols=cols, versioned=False, promoted_fields=promoted_fields)

    def insert_many_with_version(
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
        self._ensure_versioned_table(table)
        ids = self._insert_many_chunked(table, docs, cols=["data"], versioned=True)
        return [(id_, 0) for id_ in ids]

    def insert_many_with_version_promoted(
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
        self._ensure_versioned_table(table)
        cols = ["data"] + promoted_fields
        ids = self._insert_many_chunked(table, docs, cols=cols, versioned=True, promoted_fields=promoted_fields)
        return [(id_, 0) for id_ in ids]

    def _insert_many_chunked(
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
        """
        promoted_fields = promoted_fields or []
        params_per_row = 1 + len(promoted_fields)
        max_rows_per_chunk = max(1, 999 // params_per_row)

        all_ids: list[int] = []
        all_col_names = list(cols)
        if versioned:
            all_col_names = ["data", "_version"] + promoted_fields

        with self.transaction():
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
                        row_placeholders.append("0")

                    for pf in promoted_fields:
                        row_placeholders.append("?")
                        params.append(promoted_vals.get(pf))

                    value_groups.append(f"({', '.join(row_placeholders)})")

                col_list = ", ".join(all_col_names)
                values_sql = ", ".join(value_groups)
                sql = f"INSERT INTO {table} ({col_list}) VALUES {values_sql};"

                cursor = self.adapter.execute(sql, params)
                try:
                    last_id = cursor.lastrowid
                finally:
                    cursor.close()

                # ID range is contiguous because BEGIN IMMEDIATE (from
                # self.transaction()) holds an exclusive write lock, preventing
                # concurrent inserts from interleaving AUTOINCREMENT IDs.
                chunk_size = len(chunk)
                first_id = last_id - chunk_size + 1
                all_ids.extend(range(first_id, last_id + 1))

        return all_ids

    def bulk_upsert(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
        """Upsert multiple documents efficiently.

        New docs (without ``_id``) are inserted and receive ids. Existing docs
        (with ``_id``) are updated.

        Uses chunked multi-row INSERT statements (same pattern as
        ``_insert_many_chunked``) to minimize per-row Python overhead.

        Args:
            table: Table name.
            docs: List of documents. If an element contains ``_id``, it is
                treated as an update; otherwise, an insert.

        Returns:
            list[int]: The ``_id`` for each input document, preserving order.
        """
        self._ensure_table(table)
        if not docs:
            return []

        # Split into inserts (no _id) and updates (has _id), tracking
        # original indices so we can return IDs in input order.
        inserts: list[tuple[int, dict]] = []
        updates: list[tuple[int, dict]] = []
        for i, doc in enumerate(docs):
            if doc.get("_id") is None:
                inserts.append((i, doc))
            else:
                updates.append((i, doc))

        assigned = [0] * len(docs)

        with self.adapter as adapter:
            # Batch inserts: chunked multi-row INSERT, 1 param per row.
            if inserts:
                for offset in range(0, len(inserts), 999):
                    chunk = inserts[offset : offset + 999]
                    params = [
                        json.dumps({k: v for k, v in doc.items() if k != "_id"})
                        for _, doc in chunk
                    ]
                    values_sql = ", ".join("(json(?))" for _ in chunk)
                    sql = f"INSERT INTO {table} (data) VALUES {values_sql};"
                    cursor = adapter.execute(sql, params)
                    last_id = cursor.lastrowid
                    # ID range is contiguous because BEGIN IMMEDIATE (from
                    # self.adapter context) holds an exclusive write lock.
                    first_id = last_id - len(chunk) + 1
                    for j, (orig_idx, doc) in enumerate(chunk):
                        new_id = first_id + j
                        assigned[orig_idx] = new_id
                        doc["_id"] = new_id

            # Batch updates: chunked multi-row INSERT ON CONFLICT, 2 params
            # per row.  Chunk size 499 keeps params under sqlite's 999 limit.
            if updates:
                for offset in range(0, len(updates), 499):
                    chunk = updates[offset : offset + 499]
                    params = []
                    for _, doc in chunk:
                        params.extend([
                            int(doc["_id"]),
                            json.dumps(
                                {k: v for k, v in doc.items() if k != "_id"}
                            ),
                        ])
                    values_sql = ", ".join("(?, json(?))" for _ in chunk)
                    sql = (
                        f"INSERT INTO {table} (_id, data) VALUES {values_sql} "
                        "ON CONFLICT(_id) DO UPDATE SET data = excluded.data;"
                    )
                    adapter.execute(sql, params)
                    for orig_idx, doc in chunk:
                        assigned[orig_idx] = int(doc["_id"])

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
        promoted = self._promoted_columns.get(table, [])
        if promoted:
            extra_cols = ", ".join(promoted)
            cur = self.adapter.execute(
                f"SELECT _id, data, {extra_cols} FROM {table} WHERE _id = ?;", [_id]
            )
        else:
            cur = self.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [_id])
        row = cur.fetchone()
        if not row:
            return None
        obj = json.loads(row[1])
        obj["_id"] = row[0]
        if promoted:
            for i, col_name in enumerate(promoted):
                obj[col_name] = row[2 + i]
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

        Only read-only statements (SELECT, EXPLAIN, PRAGMA, WITH) are allowed.

        When the result set exposes a ``data`` column alongside ``_id``, the
        JSON payload is decoded and merged with ``_id``. For ad-hoc projections
        (e.g. ``SELECT _id``) the method returns simple dicts keyed by the
        selected columns so callers can hydrate with :meth:`SQLerModel.from_id`.

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
        self.adapter.execute(ddl)
        self.adapter.auto_commit()

    def drop_index(self, name: str):
        """Drop an index by name.

        Args:
            name: Index name.
        """
        validate_identifier(name)
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
        promoted = self._promoted_columns.get(table, [])
        if promoted:
            extra_cols = ", " + ", ".join(promoted)
        else:
            extra_cols = ""
        cur = self.adapter.execute(
            f"SELECT _id, data, _version{extra_cols} FROM {table} WHERE _id = ?;", [_id]
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            obj = json.loads(row["data"])  # type: ignore[index]
            obj["_id"] = row["_id"]  # type: ignore[index]
            obj["_version"] = row["_version"]  # type: ignore[index]
            if promoted:
                for col_name in promoted:
                    obj[col_name] = row[col_name]  # type: ignore[index]
        except (KeyError, TypeError):
            obj = json.loads(row[1])
            obj["_id"] = row[0]
            obj["_version"] = row[2]
            if promoted:
                for i, col_name in enumerate(promoted):
                    obj[col_name] = row[3 + i]
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
