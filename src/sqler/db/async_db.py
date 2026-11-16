import asyncio
import contextlib
import sqlite3
from contextvars import ContextVar
from typing import Any, Optional

from sqler import cache
from sqler.adapter.asynchronous import DEFAULT_ACQUIRE_TIMEOUT_S, AsyncSQLiteAdapter, PoolStats
from sqler.db.sqler_db import (
    _INDEX_EXISTS_SQL,
    _and_fields,
    _bulk_upserts,
    _checked_promotion,
    _columns_sql,
    _create_index_sql,
    _create_sql,
    _delete_sql,
    _drop_index_sql,
    _ended_while_leaving,
    _fields_update,
    _hand_ids,
    _id_params,
    _in_order,
    _index_rows,
    _indexes_sql,
    _inserts,
    _keep_word,
    _layout,
    _Level,
    _moved,
    _promoted_in,
    _promotion,
    _read_only,
    _record,
    _refuse_unwritten,
    _row_columns,
    _row_documents,
    _row_there_sql,
    _rows_sql,
    _settled,
    _sql_results,
    _TableMemory,
    _unwritable,
    _upsert_sql,
    _versioning,
)
from sqler.exceptions import StaleVersionError
from sqler.query.query import _has_version, aend_failed, is_missing_table
from sqler.utils import (
    validate_column_name,
    validate_identifier,
    validate_table_name,
)


async def _afetch_all(adapter: Any, sql: str, params: list, send: Any = None) -> list:
    """Every row of ``sql``, sent with ``send`` (the adapter's ``execute`` by default)."""
    cur = await (send or adapter.execute)(sql, params)
    try:
        return await cur.fetchall()
    finally:
        await cur.close()


async def _aselect_rows(adapter: Any, table: str, columns: tuple[str, ...], ids: list) -> Optional[list]:
    """Async twin of :func:`sqler.db.sqler_db._select_rows`."""
    n, params = _id_params(ids)
    sql = _rows_sql(table, columns, n)
    try:
        try:
            return await _afetch_all(adapter, sql, params, adapter._execute_judged)
        except Exception as exc:
            await aend_failed(adapter, exc, table, sql)
    except sqlite3.OperationalError as exc:
        if is_missing_table(exc, table):
            return None
        raise


class AsyncSQLerDB(_TableMemory):
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
        # what is remembered about committed tables (``_TableMemory``): versioned tables, and the
        # promoted columns of each table this object has seen settled (``_settled``)
        self._init_memory()
        # one look at a table at a time by the tasks outside a transaction (``_has_moved``)
        self._looks: dict[str, asyncio.Lock] = {}
        # what each open level of this object's own transactions learned, innermost last;
        # per task, like the adapter's depth (a task started inside a level shares it)
        self._open: ContextVar[tuple[_Level, ...]] = ContextVar(
            "sqler_open_levels", default=()
        )

    def _levels(self) -> tuple[_Level, ...]:
        return self._open.get()

    def _adapter_depth(self) -> int:
        return self.adapter._depth()

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
        await self.adapter.execute(_create_sql(table))
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
        table, checks = _checked_promotion(table, promoted, checks)

        layout, names = await self._names(table)
        unsettled = await self._run(_settled(table, names))
        unrecorded = False
        if not names or any(col not in names for col in promoted):
            async with self:  # one transaction: the column, its values and the record, or nothing
                columns, layout = await self._run(_promotion(table, promoted, checks, model))
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
        self._remember(table, layout, promoted=columns)
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
        adapter = self.adapter
        try:
            step: tuple[str, list] = next(steps)  # each step is a statement and its parameters
            while True:
                try:
                    rows = await _afetch_all(adapter, *step, adapter._execute_judged)
                except sqlite3.OperationalError as exc:
                    try:
                        step = steps.throw(exc)
                    except StopIteration:
                        raise
                    except Exception as raised:
                        await adapter._end_failed_statement(raised)
                except Exception as exc:
                    await adapter._end_failed_statement(exc)
                else:
                    step = steps.send(rows)
        except StopIteration as done:
            return done.value

    async def _names(self, table: str) -> tuple[tuple[str, ...], list[str]]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._names`."""
        return _layout(await _afetch_all(self.adapter, _columns_sql(table), []))

    async def _sent(self, table: str, make: Any) -> Any:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._sent` (``make`` is awaited), with no
        ``BEGIN IMMEDIATE`` of its own: a statement that is not refused costs the one call to
        the connection's thread that sends it."""
        adapter = self.adapter
        while True:
            step = await make()
            if step is None:
                return None
            sql, params, names = step
            try:
                return await adapter._execute_judged(sql, params)
            except Exception as exc:
                if isinstance(exc, sqlite3.OperationalError) and await self._has_moved(table, names):
                    continue
                await aend_failed(adapter, exc, table, sql)

    async def _has_moved(self, table: str, names: Optional[tuple[str, ...]]) -> bool:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._has_moved`. Outside a transaction
        the tasks refused at once look one at a time: a task that waited finds the names the
        first one read remembered, and reads nothing."""
        if names is None:
            return False  # SQLite refused it for itself
        shared = not self._levels() and self._adapter_depth() == 0
        async with self._looks.setdefault(table, asyncio.Lock()) if shared else contextlib.nullcontext():
            now = self._layout_of(table)
            if now is None or now == names:
                try:
                    now = (await self._learn(table))[0]
                except sqlite3.OperationalError:
                    return False
        return now != names

    async def _sent_all(self, table: str, make: Any) -> Any:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._sent_all` (``make`` is awaited)."""
        steps: Any = None

        async def first() -> tuple[str, list[Any], Any]:
            nonlocal steps
            steps = await make()
            return next(steps)

        cursor = await self._sent(table, first)
        execute = self.adapter.execute
        try:
            while True:
                try:
                    last = cursor.lastrowid
                finally:
                    await cursor.close()
                sql, params, _ = steps.send(last)
                cursor = await execute(sql, params)
        except StopIteration as done:
            return done.value

    async def _memory_of(self, table: str, fields: Any = ()) -> tuple[list[str], Optional[tuple[str, ...]]]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._memory_of`."""
        return (await self._shape_of(table, fields))[:2]

    async def _shape_of(self, table: str, fields: Any = ()) -> tuple[list[str], Optional[tuple[str, ...]], bool]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._shape_of`."""
        known = self._known(table)
        if known is None or known[1] is None:
            names, columns = await self._learn(table)
            layout = self._layout_of(table)
        else:
            columns, layout = known[1], known[0]
            names = layout
        return (_and_fields(columns, fields) if fields else columns), layout, _has_version(names)

    async def _columns_of(self, table: str) -> list[str]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._columns_of`."""
        promoted = self._promoted_of(table)
        return (await self._learn(table))[1] if promoted is None else promoted

    async def _learn(self, table: str) -> tuple[tuple[str, ...], list[str]]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._learn`."""
        layout, names = await self._names(table)
        unsettled = await self._run(_settled(table, names))
        promoted = _promoted_in(names)
        if not names:
            self._forget(table)
        elif self._learned() is not None:
            self._remember(table, layout, promoted=promoted)
            if unsettled:
                self._unrecorded.add(table)
        elif layout != self._layout_of(table):
            self._forget(table)
        return layout, promoted

    async def upsert_document_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        promoted_fields: list[str],
    ) -> int:
        """Insert or update, splitting promoted fields into real columns (async)."""
        return await self._upsert(validate_table_name(table), _id, doc, promoted_fields)

    async def _upsert(self, table: str, _id: Optional[int], doc: dict[str, Any], fields: Any = ()) -> int:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._upsert`."""

        async def make() -> tuple[str, list[Any], Any]:
            if _id is None and not fields and self._layout_of(table) is None:
                await self._ensure_table(table)
            columns, layout, has = await self._shape_of(table, fields)
            return _upsert_sql(table, _id, doc, columns, layout, has)

        try:
            cur = await self._sent(table, make)
        except sqlite3.OperationalError as exc:
            if _id is None or not is_missing_table(exc, table):
                raise
            _refuse_unwritten(True, table, _id)
        try:
            await self.adapter.auto_commit()
            last_id, rowcount = cur.lastrowid, cur.rowcount
        finally:
            await cur.close()
        if _id is None:
            return last_id
        if rowcount > 0:
            return _id
        _refuse_unwritten(True, table, _id)

    async def upsert_with_version_promoted(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        expected_version: Optional[int],
        promoted_fields: list[str],
    ) -> tuple[int, int]:
        """Insert or update with optimistic locking, splitting promoted columns (async)."""
        return await self._upsert_versioned(validate_table_name(table), _id, doc, expected_version, promoted_fields)

    async def _upsert_versioned(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        expected_version: Optional[int],
        fields: Any = (),
    ) -> tuple[int, int]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._upsert_versioned`."""
        if _id is not None and expected_version is None:
            raise ValueError("expected_version required for update")

        async def make() -> tuple[str, list[Any], Any]:
            # one look at what is remembered; an unsettled table is refused here, before it is given ``_version``
            columns, layout, has = await self._shape_of(table, fields)
            if not has and (_id is None or await self._has_version_column(table) is not None):
                await self._ensure_versioned_table(table)  # a write by ``_id`` makes no table
                columns, layout, has = await self._shape_of(table, fields)
            return _upsert_sql(table, _id, doc, columns, layout, has, expected_version)

        try:
            cur = await self._sent(table, make)
        except sqlite3.OperationalError as exc:
            if _id is None or not is_missing_table(exc, table):
                raise
            _refuse_unwritten(False, table, _id)  # a row at another version, as with the table there
        if _id is None:
            try:
                await self.adapter.auto_commit()
                last_id = cur.lastrowid
            finally:
                await cur.close()
            return last_id, 0
        try:
            rowcount = cur.rowcount
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if rowcount == 0:
            raise StaleVersionError("Stale version: update rejected")
        assert expected_version is not None  # refused above for a write by ``_id``
        return _id, expected_version + 1

    async def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB.insert_document`."""
        return await self._upsert(validate_table_name(table), None, doc)

    async def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB.upsert_document`."""
        return await self._upsert(validate_table_name(table), _id, doc)

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

        async def make() -> Optional[tuple[str, list[Any], Any]]:
            if expected_version is not None and not self._is_versioned(table) and await self._has_version_column(table) is not None:
                await self._ensure_versioned_table(table)  # a write by ``_id`` makes no table
            columns, layout, has = await self._shape_of(table, promoted)
            return _fields_update(table, _id, doc, fields, columns, expected_version, layout, has)

        try:
            cur = await self._sent(table, make)
        except sqlite3.OperationalError as exc:
            if not is_missing_table(exc, table):
                raise
            _refuse_unwritten(True, table, _id)
        if cur is None:
            return None
        try:
            rowcount = cur.rowcount
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        if rowcount > 0:
            return None if expected_version is None else expected_version + 1
        gone = expected_version is None or not await _afetch_all(
            self.adapter, _row_there_sql(table), [_id]
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

    async def _read_rows(self, table: str, ids: list, *, versioned: bool = False) -> tuple[Optional[list], tuple[str, ...]]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._read_rows`."""
        table = validate_table_name(table)
        n, params = _id_params(ids)
        columns: Any = _row_columns((), versioned)
        read: Any = columns

        async def make() -> tuple[str, list[Any], Any]:
            nonlocal columns, read
            promoted, layout, has = await self._shape_of(table)
            columns = _row_columns(promoted, has or versioned)
            read = columns if has or not versioned else _row_columns(promoted)
            return _rows_sql(table, read, n, layout), params, layout

        try:
            cur = await self._sent(table, make)
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        except sqlite3.OperationalError as exc:
            if is_missing_table(exc, table):
                return None, columns  # a read creates no table
            raise
        await self.adapter.auto_commit()
        return (rows if read is columns else [(r[0], r[1], 0, *r[2:]) for r in rows]), columns

    async def _has_version_column(self, table: str) -> Optional[bool]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._has_version_column`."""
        if self._is_versioned(table):
            return True
        layout = self._layout_of(table)
        if layout is None:
            layout, names = await self._names(table)
            if layout:
                self._seen(table, layout, names)
        return _has_version(layout) if layout else None

    async def delete_document(self, table: str, _id: int) -> None:
        """Delete a document by id.

        Args:
            table: Table name.
            _id: Row id to delete.
        """
        await self._ensure_table(table)
        cur = await self.adapter.execute(_delete_sql(table), [_id])
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
        return await self._insert_many_chunked(table, docs)

    async def insert_many_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[int]:
        """Multi-row INSERT splitting promoted fields into real columns.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            list[int]: Assigned ``_id`` for each document, preserving order.
        """
        if not docs:
            return []
        table = validate_table_name(table)
        for field in promoted_fields:
            validate_identifier(field)
        return await self._insert_many_chunked(table, docs, promoted_fields=promoted_fields)

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
        await self._columns_of(table)  # an unsettled table is refused before it is given ``_version``
        await self._ensure_versioned_table(table)
        ids = await self._insert_many_chunked(table, docs)
        return [(id_, 0) for id_ in ids]

    async def insert_many_with_version_promoted(
        self, table: str, docs: list[dict[str, Any]], promoted_fields: list[str]
    ) -> list[tuple[int, int]]:
        """Multi-row INSERT with ``_version=0`` splitting promoted fields.

        Args:
            table: Table name.
            docs: List of full document payloads.
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            list[tuple[int, int]]: ``(id, 0)`` pairs for each document.
        """
        if not docs:
            return []
        for field in promoted_fields:
            validate_identifier(field)
        await self._ensure_versioned_table(table)
        ids = await self._insert_many_chunked(table, docs, promoted_fields=promoted_fields)
        return [(id_, 0) for id_ in ids]

    async def _insert_many_chunked(
        self,
        table: str,
        docs: list[dict[str, Any]],
        *,
        promoted_fields: list[str] | None = None,
    ) -> list[int]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB._insert_many_chunked`."""

        async def make() -> Any:
            columns, layout, has = await self._shape_of(table, promoted_fields or ())
            return _inserts(table, docs, has, columns, layout)

        async with self.transaction():
            return await self._sent_all(table, make)

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
        if not docs:
            return []

        async def make() -> Any:
            columns, layout, has = await self._shape_of(table)
            return _bulk_upserts(table, docs, has, columns, layout)

        async with self.transaction():
            ids = await self._sent_all(table, make)
        return _hand_ids(docs, ids)

    async def execute_sql(
        self, query: str, params: Optional[list[Any]] = None
    ) -> list[dict[str, Any]]:
        """Run a custom SELECT and return lightweight row mappings.

        Only read-only statements (SELECT, EXPLAIN, PRAGMA, WITH) are allowed.

        Each row is read by its column names (:func:`sqler.db.sqler_db._sql_results`): with a
        ``data`` column, its JSON document, with ``_id`` set from an ``_id`` column; with
        ``_id`` the only column, ``{"_id": id}``, so callers can hydrate with
        :meth:`AsyncSQLerModel.from_id`; else a dict of every column by its name.

        Args:
            query: SQL SELECT statement.
            params: Optional parameter list.

        Returns:
            list[dict[str, Any]]: One dict per row, as above; a ``data`` column whose JSON is not an object, read with an ``_id`` column, raises ``TypeError``.

        Raises:
            ValueError: If the query is not a read-only statement.
        """
        _read_only(query)
        cur = await self.adapter.execute(query, params or [])
        try:
            description = cur.description
            rows = await cur.fetchall()
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        return _sql_results(description, rows)

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
        cur = await self.adapter.execute(_create_index_sql(table, field, unique, name, where))
        try:
            await self.adapter.auto_commit()
        finally:
            await cur.close()

    async def drop_index(self, name: str) -> None:
        """Drop an index by name.

        Args:
            name: Index name.
        """
        cur = await self.adapter.execute(_drop_index_sql(name))
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
        cur = await self.adapter.execute(*_indexes_sql(table))
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        await self.adapter.auto_commit()
        return _index_rows(rows)

    async def index_exists(self, name: str) -> bool:
        """Check if an index exists by name.

        Args:
            name: Index name to check.

        Returns:
            True if the index exists, False otherwise.
        """
        cur = await self.adapter.execute(_INDEX_EXISTS_SQL, [name])
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
        layout, names = await self._names(table)
        await self.adapter.auto_commit()
        added: tuple[str, ...] = ()
        if not _has_version(layout):
            # the look again and the change in one transaction that holds the write lock from
            # its start, so no other task, connection or process changes the table between them
            async with self:
                layout, names, added = await self._run(_versioning(table))
        self._seen(table, layout, names, added)

    async def upsert_with_version(
        self, table: str, _id: Optional[int], doc: dict[str, Any], expected_version: Optional[int]
    ) -> tuple[int, int]:
        """Async twin of :meth:`sqler.db.sqler_db.SQLerDB.upsert_with_version`."""
        return await self._upsert_versioned(validate_table_name(table), _id, doc, expected_version)

    async def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        rows, columns = await self._read_rows(table, [_id], versioned=True)
        return _row_documents(rows, columns)[0] if rows else None

    async def query(self, table: str):
        """Return an AsyncSQLerQuery that reads ``table`` through this DB, as a model's query set does."""
        from sqler.query.async_query import AsyncSQLerQuery

        return AsyncSQLerQuery(table=validate_table_name(table), adapter=self.adapter)._clone(db=self)

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
        self._open.set((*self._open.get(), _Level(set(), {}, {}, self.adapter._depth(), set())))

    async def _end_level(self, commit: bool, *, leaving: Optional[BaseException] = None) -> None:
        """End this task's innermost level; the async twin of ``SQLerDB._end_level``.

        ``leaving``: the caller's exception on its way out (see ``sqler_db._end_transaction``).
        """
        levels = self._open.get()
        level = levels[-1] if levels else None  # none: begun in another task; nothing to merge
        self._open.set(levels[:-1])
        committed = False
        try:
            await self.adapter.end_transaction(commit=commit)
            committed = commit
        except Exception as ended:
            _ended_while_leaving(ended, leaving)
        finally:
            if level is not None and level.written:
                cache.level_ended(level.written, self._written_level() if committed else None)
        if committed and level is not None:
            self._merge(level)


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
