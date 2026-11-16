import sqlite3
import threading
from typing import Any, Optional

from sqler import cache
from sqler.adapter import SQLiteAdapter
from sqler.db._promotion import _moved, _promotion, _record, _settled, _unwritable, _versioning
from sqler.db._raw_read import _read_only, _sql_results
from sqler.db._rows import (
    _columns_sql,
    _id_params,
    _in_order,
    _layout,
    _promoted_in,
    _row_columns,
    _row_documents,
    _rows_sql,
)
from sqler.db._statements import (
    _INDEX_EXISTS_SQL,
    _checked_promotion,
    _create_index_sql,
    _delete_sql,
    _drop_index_sql,
    _index_rows,
    _indexes_sql,
    _keep_word,
    _row_there_sql,
)
from sqler.db._table_memory import _Level, _TableMemory
from sqler.db._writes import (
    _and_fields,
    _bulk_upserts,
    _create_sql,
    _fields_update,
    _hand_ids,
    _inserts,
    _refuse_unwritten,
    _upsert_sql,
)
from sqler.exceptions import (
    StaleVersionError,
    TransactionStateError,
)
from sqler.query import SQLerQuery
from sqler.query.query import _has_version, end_failed, is_missing_table
from sqler.utils import (
    validate_column_name,
    validate_identifier,
    validate_table_name,
)


class SQLerDB(_TableMemory):
    """Document store for JSON blobs on SQLite.

    SQLerDB persists Python dicts as JSON in a table with schema
    ``(_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)``. The API
    is table-agnostic: pass the table name on each call. Tables are created
    on demand as you insert or query.
    """

    @classmethod
    def in_memory(cls, shared: bool = True, *, name: Optional[str] = None, timeout_ms: int = 5000) -> "SQLerDB":
        """Create a SQLerDB backed by an in-memory SQLite database.

        Args:
            shared: When True, use a shared-cache URI so multiple connections
                see the same in-memory database.
            name: Optional shared cache identifier; use the same name to
                connect multiple adapters to a shared in-memory database.
            timeout_ms: How long a statement waits for a lock, in milliseconds
                (see ``SQLiteAdapter``).

        Returns:
            SQLerDB: Connected database instance.
        """
        adapter = SQLiteAdapter.in_memory(shared=shared, name=name, timeout_ms=timeout_ms)
        return cls(adapter)

    @classmethod
    def on_disk(cls, path: str = "sqler.db", *, timeout_ms: int = 5000) -> "SQLerDB":
        """Create a SQLerDB backed by a persistent file on disk.

        Args:
            path: Path to the SQLite database file (created if missing).
            timeout_ms: How long a statement waits for a lock, in milliseconds
                (see ``SQLiteAdapter``).

        Returns:
            SQLerDB: Connected database instance.
        """
        adapter = SQLiteAdapter.on_disk(path, timeout_ms=timeout_ms)
        return cls(adapter)

    def __init__(self, adapter: SQLiteAdapter):
        self.adapter = adapter
        self.adapter.connect()
        # serialize DDL operations (e.g., adding _version) across threads
        self._ddl_lock = threading.RLock()
        # what is remembered about committed tables (``_TableMemory``): versioned tables, and the
        # promoted columns of each table this object has seen settled (:func:`_settled`)
        self._init_memory()
        # what each open level of this object's own transactions learned, innermost last; per
        # thread, like the adapter's depth
        self._open = _OpenLevels()

    def _levels(self) -> list[_Level]:
        return self._open.levels

    def _adapter_depth(self) -> int:
        return self.adapter._txn_depth

    def ensure_table(
        self,
        table: str,
        *,
        promoted: Optional[dict[str, str]] = None,
        checks: Optional[dict[str, str]] = None,
        versioned: bool = False,
    ) -> None:
        """Create a table if it is missing, with promoted columns, CHECK constraints and a version.

        Safe to call again: on an existing table it adds the promoted columns
        and the ``_version`` column that are missing, and changes nothing else
        (CHECK constraints are only written when the table is created). A
        column added to a table with rows takes each row's value out of its
        document, in the same transaction; a row without the key gets the
        column's DEFAULT, or NULL.

        Args:
            table: Table name (``[a-zA-Z_][a-zA-Z0-9_]*``).
            promoted: ``{column: definition}`` for real columns beside ``data``,
                e.g. ``{"status": "TEXT NOT NULL DEFAULT 'pending'"}``.
            checks: ``{name: expression}`` CHECK constraints for a new table.
            versioned: Add the ``_version INTEGER NOT NULL DEFAULT 0`` column
                used by the versioned writes.

        Raises:
            InvalidTableNameError: The table name is not accepted.
            InvalidIdentifierError: A promoted column name is not accepted.
            InvalidColumnDefError: A column definition or CHECK expression is not accepted.
            SchemaError: A promoted column cannot be added and nothing was changed: a row's
                value cannot be kept in it, it is NOT NULL with no DEFAULT, or an earlier
                version left its values in the documents (see :meth:`move_promoted`).
        """
        if promoted or checks:
            self._ensure_table_with_promoted(table, promoted or {}, checks)
        else:
            self._ensure_table(table)
        if versioned:
            self._ensure_versioned_table(table)

    def _ensure_table(self, table: str) -> None:
        """Create the target table if it doesn't exist.

        Args:
            table: Table name to ensure.
        """
        table = validate_table_name(table)
        self.adapter.execute(_create_sql(table))
        self.adapter.auto_commit()

    def _ensure_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
        *,
        model: Any = None,
    ) -> None:
        """Ensure ``table`` for a promoted write; nothing is sent when it is remembered here
        with every column of ``promoted`` and its record written."""
        known = self._promoted_of(table)
        if known is None or any(col not in known for col in promoted) or table in self._unrecorded:
            self._ensure_table_with_promoted(table, promoted, checks, model=model)

    def _ensure_table_with_promoted(
        self,
        table: str,
        promoted: dict[str, str],
        checks: Optional[dict[str, str]] = None,
        *,
        model: Any = None,
    ) -> None:
        """Create or upgrade a table with promoted columns.

        Promoted columns are real SQLite columns that live alongside the JSON
        ``data`` blob. They get CHECK constraints on fresh tables. Added to a
        table that has rows, a column takes each row's value out of its document,
        all in one transaction, and a row without the key gets what it read
        before: the default of ``model``'s field, else the column's DEFAULT.

        Args:
            table: Table name.
            promoted: ``{column_name: column_def}`` mapping, e.g.
                ``{"status": "TEXT NOT NULL DEFAULT 'pending'"}``.
            checks: Optional ``{name: expression}`` CHECK constraints.
            model: The model class whose rows the table holds, if any.

        Raises:
            SchemaError: A row's value cannot be kept, a NOT NULL column has no DEFAULT,
                or a column an earlier version promoted is not settled (``move_promoted``).
        """
        table, checks = _checked_promotion(table, promoted, checks)

        with self._ddl_lock:
            layout, names = self._names(table)
            unsettled = self._run(_settled(table, names))
            unrecorded = False
            if not names or any(col not in names for col in promoted):
                with self:  # one transaction: the column, its values and the record, or nothing
                    columns, layout = self._run(_promotion(table, promoted, checks, model))
            else:
                columns = _promoted_in(names)
                if unsettled:
                    try:
                        with self:
                            self._run(_record(table, unsettled))
                    except sqlite3.OperationalError as exc:
                        if not _unwritable(exc):
                            raise
                        unrecorded = True  # settled all the same: there is nothing to add or move
            self._remember(table, layout, promoted=columns)
            (self._unrecorded.add if unrecorded else self._unrecorded.discard)(table)

    def move_promoted(self, table: str, column: str, *, keep: str) -> None:
        """Settle a promoted column whose values an earlier version of sqler left in the documents.

        That version added the column and left each row's value in its document; since then a
        write may have changed either, and a row does not say which is newer. This takes the
        key out of every document, in one transaction, and records the column as settled.

        Args:
            table: Table name.
            column: The promoted column.
            keep: ``"document"``: a row whose document holds the key gets that value in the
                column. ``"column"``: the column keeps its values.

        Raises:
            ValueError: ``keep`` is neither word.
            TableNotFoundError: The table does not exist.
            SchemaError: The column is not a promoted column of the table, or (``keep="document"``)
                a document's value cannot go into the column; nothing is changed.
        """
        keep = _keep_word(keep)
        table = validate_table_name(table)
        validate_column_name(column)
        with self._ddl_lock, self:
            self._run(_moved(table, column, keep))

    def _run(self, steps: Any) -> Any:
        """Send each statement ``steps`` yields, answer it with its rows or its
        ``sqlite3.OperationalError``, and return what ``steps`` returns. A failure ends what a
        failed statement ends only when ``steps`` raises it (or its own error for it): one it
        reads as an answer (the record of settled columns not there) ends nothing."""
        adapter = self.adapter
        try:
            step = next(steps)
            while True:
                try:
                    rows = adapter._execute_judged(*step).fetchall()
                except sqlite3.OperationalError as exc:
                    try:
                        step = steps.throw(exc)
                    except StopIteration:
                        raise
                    except Exception as raised:
                        adapter._end_failed_statement(raised)
                except Exception as exc:
                    adapter._end_failed_statement(exc)
                else:
                    step = steps.send(rows)
        except StopIteration as done:
            return done.value

    def _names(self, table: str) -> tuple[tuple[str, ...], list[str]]:
        """The names ``SELECT *`` gives for the validated ``table`` and the names of its stored
        columns (:func:`_layout`); none when it is missing."""
        return _layout(self.adapter.execute(_columns_sql(table)).fetchall())

    def _sent(self, table: str, make: Any, *, immediate: bool = False) -> Any:
        """Send the statement ``make()`` builds from what this object remembers of ``table``
        and return its cursor; ``None`` when ``make()`` returns ``None`` (nothing to send).
        ``make()`` gives ``(sql, params, names)``: ``names`` are the remembered names the
        statement was built from, whose guard it carries (:func:`_guard`), ``None`` for none.

        SQLite refuses the statement when the table no longer has those names. When the names
        the table has now differ (:meth:`_has_moved`), nothing has been ended and the statement is
        built and sent again; any other failure ends what a failed statement ends (all that
        was sent since the last commit, outside a transaction) and is raised as it is, but a
        read of a missing ``table``, which ends nothing (:func:`end_failed`).

        ``immediate``: outside a transaction the write lock is taken first with
        ``BEGIN IMMEDIATE`` (less live-lock under contention), and that transaction is ended
        on every path: committed on success, rolled back by the adapter's rule when anything
        raises before the commit, with the error that raised leaving.
        """
        adapter = self.adapter
        while True:
            step = make()
            if step is None:
                return None
            sql, params, names = step
            try:
                if immediate and not adapter.in_transaction:
                    return self._sent_immediate(sql, params)
                return adapter._execute_judged(sql, params)
            except Exception as exc:
                if isinstance(exc, sqlite3.OperationalError) and self._has_moved(table, names):
                    continue
                end_failed(adapter, exc, table, sql)

    def _sent_immediate(self, sql: str, params: list[Any]) -> Any:
        """The statement sent in a transaction of its own begun with ``BEGIN IMMEDIATE`` and committed."""
        try:
            self.adapter.execute("BEGIN IMMEDIATE;")
        except (RuntimeError, ValueError):
            pass  # an adapter that cannot run BEGIN here: the statement runs in its own transaction
        try:
            cursor = self.adapter.execute(sql, params)
            self.adapter.auto_commit()
        except BaseException as exc:
            self.adapter.__exit__(type(exc), exc, exc.__traceback__)  # rolls back what is still open
            raise
        return cursor

    def _has_moved(self, table: str, names: Optional[tuple[str, ...]]) -> bool:
        """Whether ``table`` has other names now than ``names``, those a refused statement was
        built from: the names remembered now when they are others (another statement learned
        the table since), else the names read now (:meth:`_learn`). A remembered table that is
        gone has none now and is forgotten: the statement built again is built from nothing
        remembered, so a plain insert makes its table again (:meth:`_upsert`) and anything else
        is refused for the missing table. A statement built from nothing remembered, or a
        table that cannot be read now, has nothing to build again from."""
        if names is None:
            return False  # SQLite refused it for itself
        now = self._layout_of(table)
        if now is None or now == names:
            try:
                now = self._learn(table)[0]
            except sqlite3.OperationalError:
                return False
        return now != names

    def _memory_of(self, table: str, fields: Any = ()) -> tuple[list[str], Optional[tuple[str, ...]]]:
        """The promoted columns and remembered names of :meth:`_shape_of`."""
        return self._shape_of(table, fields)[:2]

    def _shape_of(self, table: str, fields: Any = ()) -> tuple[list[str], Optional[tuple[str, ...]], bool]:
        """The promoted columns of the validated ``table`` (:meth:`_columns_of`), the names
        remembered beside them, whose guard a statement built from those columns carries
        (``None``: none remembered, as where nothing can be remembered, a transaction that is
        not this object's), and whether the table has ``_version`` (:func:`_has_version`), from
        those names or the names just read.

        A writer splits a document by these columns. ``fields``: the columns a caller says
        the table has (a class's promoted fields); one the table lacks is added
        (:func:`_and_fields`), so its write is refused rather than put into ``data``."""
        if self._open.levels:
            known = self._known(table)
            layout, columns = (None, None) if known is None else known[:2]
        else:  # no transaction of this object's is open: the shared memory, read here (every call comes through)
            layout = self._layouts.get(table)
            columns = None if layout is None else self._promoted_columns.get(table)
        names = layout
        if columns is None:
            names, columns = self._learn(table)
            layout = self._layout_of(table)
        return (_and_fields(columns, fields) if fields else columns), layout, _has_version(names)

    def _columns_of(self, table: str) -> list[str]:
        """The promoted columns of the validated ``table``, which every reader sets over the
        document: remembered, or else checked once as an ensure checks them (:func:`_settled`,
        which writes nothing) and remembered with the table's names (a table without any as
        having none). A missing table has none and is not remembered.

        Raises:
            SchemaError: A document still holds the key of a column, which would hide its value.
        """
        promoted = self._promoted_of(table)
        return self._learn(table)[1] if promoted is None else promoted

    def _learn(self, table: str) -> tuple[tuple[str, ...], list[str]]:
        """Read the names ``SELECT *`` gives for the validated ``table`` and its promoted columns,
        checked as an ensure checks them (:func:`_settled`, which writes nothing), and remember
        them; returns both, none for a missing table, which is forgotten. Where nothing can be
        remembered (a transaction that is not this object's), names other than those
        remembered are forgotten, so no statement is built from them again.

        Raises:
            SchemaError: A document still holds the key of a column, which would hide its value.
        """
        layout, names = self._names(table)
        unsettled = self._run(_settled(table, names))
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
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            int: The existing or newly assigned ``_id``.

        Raises:
            NotFoundError: There is no row ``_id``, or no such table; nothing is written.
        """
        return self._upsert(validate_table_name(table), _id, doc, promoted_fields)

    def _upsert(self, table: str, _id: Optional[int], doc: dict[str, Any], fields: Any = ()) -> int:
        """Write ``doc`` as a new row, or into row ``_id``, each key of the table's columns in
        its column (:meth:`_memory_of`, with the caller's ``fields``), with the guard of what is
        remembered of it (:meth:`_sent`). A new row with no ``fields`` makes its table, for
        which nothing is sent once the table is remembered; a write by ``_id`` makes no table."""

        def make() -> tuple[str, list[Any], Any]:
            if _id is None and not fields and self._layout_of(table) is None:
                self._ensure_table(table)
            columns, layout, has = self._shape_of(table, fields)
            return _upsert_sql(table, _id, doc, columns, layout, has)

        try:
            cursor = self._sent(table, make)
        except sqlite3.OperationalError as exc:
            if _id is None or not is_missing_table(exc, table):
                raise
            _refuse_unwritten(True, table, _id)
        self.adapter.auto_commit()
        if _id is None:
            return cursor.lastrowid
        if cursor.rowcount > 0:
            return _id
        _refuse_unwritten(True, table, _id)

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
            promoted_fields: Columns the table must have. Every key that names a
                column of the table, as it is now, goes into it; one of these the table
                lacks is refused by SQLite.

        Returns:
            tuple[int, int]: The row id and new version.
        """
        return self._upsert_versioned(validate_table_name(table), _id, doc, expected_version, promoted_fields)

    def _upsert_versioned(
        self,
        table: str,
        _id: Optional[int],
        doc: dict[str, Any],
        expected_version: Optional[int],
        fields: Any = (),
    ) -> tuple[int, int]:
        """``upsert_with_version`` of ``doc``, split as :meth:`_upsert` splits it. The table
        gets its ``_version`` column first; a write by ``_id`` makes no table (``NotFoundError``)."""
        if _id is not None and expected_version is None:
            raise ValueError("expected_version required for update")

        def make() -> tuple[str, list[Any], Any]:
            # one look at what is remembered; an unsettled table is refused here, before it is given ``_version``
            columns, layout, has = self._shape_of(table, fields)
            if not has and (_id is None or self._has_version_column(table) is not None):
                self._ensure_versioned_table(table)  # a write by ``_id`` makes no table
                columns, layout, has = self._shape_of(table, fields)
            return _upsert_sql(table, _id, doc, columns, layout, has, expected_version)

        if _id is None:
            cursor = self._sent(table, make)
            self.adapter.auto_commit()
            return cursor.lastrowid, 0
        try:
            cursor = self._sent(table, make, immediate=True)
        except sqlite3.OperationalError as exc:
            if not is_missing_table(exc, table):
                raise
            _refuse_unwritten(False, table, _id)  # a row at another version, as with the table there
        if cursor.rowcount <= 0:
            raise StaleVersionError("Stale version: update rejected")
        assert expected_version is not None  # refused above for a write by ``_id``
        return _id, expected_version + 1

    def insert_document(self, table: str, doc: dict[str, Any]) -> int:
        """Insert a document: a key that names a column beside ``data`` goes into the column, the
        rest into ``data``; a column whose key it does not hold is left out, so it gets its DEFAULT.

        Args:
            table: Table name.
            doc: JSON-serializable dict to persist.

        Returns:
            int: Newly assigned ``_id``.
        """
        return self._upsert(validate_table_name(table), None, doc)

    def upsert_document(self, table: str, _id: Optional[int], doc: dict[str, Any]) -> int:
        """Insert or update a document, as :meth:`insert_document` writes it; an update leaves a
        column whose key the document does not hold as it is.

        Args:
            table: Table name.
            _id: Existing id to update, or ``None`` to insert.
            doc: Document to write.

        Returns:
            int: The existing or newly assigned ``_id``.

        Raises:
            NotFoundError: The table has no row ``_id``, or there is no such table; nothing is
                written and no table is made.
        """
        return self._upsert(validate_table_name(table), _id, doc)

    def _update_fields(
        self,
        table: str,
        _id: int,
        doc: dict[str, Any],
        fields: Any,
        promoted: Any = (),
        expected_version: Optional[int] = None,
    ) -> Optional[int]:
        """Write the ``fields`` of ``doc`` into row ``_id`` and leave its other fields as they
        are, in one statement: a field that names a column beside ``data`` in its column, the
        rest in ``data``. The columns are the table's, with the caller's ``promoted``
        (:meth:`_memory_of`). With ``expected_version`` the row is written only at
        that version, as ``upsert_with_version`` writes it, and the new version is returned.

        Raises:
            NotFoundError: The row is gone, or the table: a write of some fields cannot
                recreate it, and makes no table.
            StaleVersionError: The row is at another version.
        """
        table = validate_table_name(table)

        def make() -> Optional[tuple[str, list[Any], Any]]:
            if expected_version is not None and not self._is_versioned(table) and self._has_version_column(table) is not None:
                self._ensure_versioned_table(table)  # a write by ``_id`` makes no table
            columns, layout, has = self._shape_of(table, promoted)
            return _fields_update(table, _id, doc, fields, columns, expected_version, layout, has)

        try:
            cur = self._sent(table, make, immediate=expected_version is not None)
        except sqlite3.OperationalError as exc:
            if not is_missing_table(exc, table):
                raise
            _refuse_unwritten(True, table, _id)
        if cur is None:
            return None
        if expected_version is None:
            self.adapter.auto_commit()
            if cur.rowcount > 0:
                return None
            _refuse_unwritten(True, table, _id)
        if cur.rowcount > 0:
            return expected_version + 1
        row = self.adapter.execute(_row_there_sql(table), [_id]).fetchone()
        _refuse_unwritten(row is None, table, _id)

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
        table = validate_table_name(table)
        self._ensure_table(table)
        return self._insert_many_chunked(table, docs)

    def insert_many_promoted(
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
        return self._insert_many_chunked(table, docs, promoted_fields=promoted_fields)

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
        table = validate_table_name(table)
        self._columns_of(table)  # an unsettled table is refused before it is given ``_version``
        self._ensure_versioned_table(table)
        ids = self._insert_many_chunked(table, docs)
        return [(id_, 0) for id_ in ids]

    def insert_many_with_version_promoted(
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
        self._ensure_versioned_table(table)
        ids = self._insert_many_chunked(table, docs, promoted_fields=promoted_fields)
        return [(id_, 0) for id_ in ids]

    def _insert_many_chunked(
        self,
        table: str,
        docs: list[dict[str, Any]],
        *,
        promoted_fields: list[str] | None = None,
    ) -> list[int]:
        """Internal: the multi-row INSERTs of ``docs`` into the validated ``table``, in one
        transaction (:func:`_inserts`), split as :meth:`_upsert` splits a document.
        """

        def make() -> Any:
            columns, layout, has = self._shape_of(table, promoted_fields or ())
            return _inserts(table, docs, has, columns, layout)

        with self.transaction():
            return self._sent_all(table, make)

    def _sent_all(self, table: str, make: Any) -> Any:
        """Send the statements of the steps ``make()`` makes (each answered with its cursor's
        ``lastrowid``) and return what they return: the first one, built from what this
        object remembers of ``table``, as :meth:`_sent` sends one (built and sent again when
        the table has other names now), the rest after it in the transaction it is in, whose
        write lock keeps the table as it is."""
        adapter = self.adapter
        while True:
            steps = make()
            sql, params, names = next(steps)
            try:
                cursor = adapter._execute_judged(sql, params)
                break
            except Exception as exc:
                if isinstance(exc, sqlite3.OperationalError) and self._has_moved(table, names):
                    continue
                end_failed(adapter, exc, table, sql)
        execute = adapter.execute
        try:
            while True:
                try:
                    last = cursor.lastrowid
                finally:
                    cursor.close()
                sql, params, _ = steps.send(last)
                cursor = execute(sql, params)
        except StopIteration as done:
            return done.value

    def bulk_upsert(self, table: str, docs: list[dict[str, Any]]) -> list[int]:
        """Upsert multiple documents efficiently.

        New docs (without ``_id``) are inserted and receive ids. Existing docs
        (with ``_id``) are updated.

        Uses chunked multi-row INSERT statements (same pattern as
        ``_insert_many_chunked``) to minimize per-row Python overhead, in one
        transaction. A column whose key a document does not hold is not written: a
        new row gets its DEFAULT, an existing row keeps its value.

        Args:
            table: Table name.
            docs: List of documents. If an element contains ``_id``, it is
                treated as an update; otherwise, an insert.

        Returns:
            list[int]: The ``_id`` for each input document, preserving order.
        """
        table = validate_table_name(table)
        self._ensure_table(table)
        if not docs:
            return []
        def make() -> Any:
            columns, layout, has = self._shape_of(table)
            return _bulk_upserts(table, docs, has, columns, layout)

        with self.transaction():
            ids = self._sent_all(table, make)
        return _hand_ids(docs, ids)

    def find_document(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by id.

        Args:
            table: Table name.
            _id: Row id to fetch.

        Returns:
            dict | None: Decoded document with ``_id`` merged in, or ``None``
            if not found.
        """
        rows, columns = self._read_rows(table, [_id])
        return _row_documents(rows, columns)[0] if rows else None

    def find_documents(self, table: str, ids: list[int]) -> list[dict[str, Any]]:
        """Fetch multiple documents by id list (batch operation).

        Documents are returned in the same order as the input ids. If an id
        is not found, it is omitted from the result (no None placeholder).

        Args:
            table: Table name.
            ids: List of row ids to fetch.

        Returns:
            list[dict]: Decoded documents with ``_id`` and the promoted columns merged
            in, as :meth:`find_document` gives each.
        """
        if not ids:
            return []
        rows, columns = self._read_rows(table, ids)
        return _row_documents(_in_order(rows or [], ids), columns)

    def _read_rows(self, table: str, ids: list, *, versioned: bool = False) -> tuple[Optional[list], tuple[str, ...]]:
        """The by-id read every reader of this database goes through; it writes nothing.

        Returns the stored rows of ``ids`` (stored order) and the columns they were read
        with: the promoted columns of ``table`` (:meth:`_columns_of`), and ``_version`` where
        the table has it (:meth:`_shape_of`: the row's version is its column, never a key
        of its document) or the read is ``versioned``, read with the guard of what is
        remembered (:meth:`_sent`). A missing table gives ``None`` rows and is not created. A
        ``versioned`` read of a table without its ``_version`` column gives its rows at
        version 0 and keeps its schema: the first versioned write adds the column.
        """
        table = validate_table_name(table)
        n, params = _id_params(ids)
        adapter = self.adapter
        columns = _row_columns((), versioned)  # of the rows given
        try:
            while True:  # the loop of :meth:`_sent`, written here: every read comes through
                promoted, layout, has = self._shape_of(table)
                columns = _row_columns(promoted, has or versioned)
                read = columns if has or not versioned else _row_columns(promoted)
                sql = _rows_sql(table, read, n, layout)
                try:
                    cursor = adapter._execute_judged(sql, params)
                    break
                except Exception as exc:
                    if isinstance(exc, sqlite3.OperationalError) and self._has_moved(table, layout):
                        continue
                    end_failed(adapter, exc, table, sql)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if is_missing_table(exc, table):
                return None, columns  # a read creates no table
            raise
        return (rows if read is columns else [(r[0], r[1], 0, *r[2:]) for r in rows]), columns

    def _has_version_column(self, table: str) -> Optional[bool]:
        """Whether the validated ``table`` has its ``_version`` column; writes nothing.

        ``True`` when it has it, ``False`` when the table is there without it, ``None`` when
        there is no such table. Read from the names remembered for it, or else looked up with
        one ``PRAGMA table_xinfo`` and remembered (:meth:`_seen`); a missing table is not
        remembered.
        """
        if self._is_versioned(table):
            return True
        layout = self._layout_of(table)
        if layout is None:
            layout, names = self._names(table)
            if layout:
                self._seen(table, layout, names)
        return _has_version(layout) if layout else None

    def delete_document(self, table: str, _id: int) -> None:
        """Delete a document by id.

        Args:
            table: Table name.
            _id: Row id to delete.
        """
        self._ensure_table(table)
        self.adapter.execute(_delete_sql(table), [_id])
        self.adapter.auto_commit()

    def execute_sql(self, query: str, params: Optional[list[Any]] = None) -> list[dict[str, Any]]:
        """Run a custom SELECT and return lightweight row mappings.

        Only read-only statements (SELECT, EXPLAIN, PRAGMA, WITH) are allowed.

        Each row is read by its column names (:func:`sqler.db._raw_read._sql_results`): with a
        ``data`` column, its JSON document, with ``_id`` set from an ``_id`` column; with
        ``_id`` the only column, ``{"_id": id}``, so callers can hydrate with
        :meth:`SQLerModel.from_id`; else a dict of every column by its name.

        Args:
            query: SQL SELECT statement.
            params: Optional parameter list.

        Returns:
            list[dict[str, Any]]: One dict per row, as above; a ``data`` column whose JSON is not an object, read with an ``_id`` column, raises ``TypeError``.

        Raises:
            ValueError: If the query is not a read-only statement.
        """
        _read_only(query)
        cursor = self.adapter.execute(query, params or [])
        return _sql_results(cursor.description, cursor.fetchall())

    def query(self, table: str) -> SQLerQuery:
        """Return a SQLerQuery that reads ``table`` through this DB, as a model's query set does.

        Args:
            table: Table name.

        Returns:
            SQLerQuery: Query object you can chain and execute.
        """
        return SQLerQuery(table=validate_table_name(table), adapter=self.adapter)._clone(db=self)

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
        return Transaction(self.adapter, self)

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
        self.adapter.execute(_create_index_sql(table, field, unique, name, where))
        self.adapter.auto_commit()

    def drop_index(self, name: str):
        """Drop an index by name.

        Args:
            name: Index name.
        """
        self.adapter.execute(_drop_index_sql(name))
        self.adapter.auto_commit()

    def list_indexes(self, table: Optional[str] = None) -> list[dict[str, Any]]:
        """List indexes in the database.

        Args:
            table: Optional table name to filter by. If None, lists all indexes.

        Returns:
            List of dicts with index info: name, table, sql, unique.
        """
        return _index_rows(self.adapter.execute(*_indexes_sql(table)).fetchall())

    def index_exists(self, name: str) -> bool:
        """Check if an index exists by name.

        Args:
            name: Index name to check.

        Returns:
            True if the index exists, False otherwise.
        """
        return self.adapter.execute(_INDEX_EXISTS_SQL, [name]).fetchone() is not None

    def __enter__(self):
        """Enter context manager; begin transaction."""
        self._begin_level()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager; commit, or roll back while the caller's exception leaves."""
        self._end_level(exc_type is None, leaving=exc_val)
        return False

    def _begin_level(self) -> None:
        self.adapter.begin_transaction()
        self._open.levels.append(_Level(set(), {}, {}, self.adapter._txn_depth, set()))

    def _end_level(self, commit: bool, *, leaving: Optional[BaseException] = None) -> None:
        """End this thread's innermost level; what it learned about tables goes with it.

        A commit moves it to the level above, or at the outermost level to the shared memory
        (``_merge``); a rollback, or a commit that fails, drops it. That runs no statement and
        raises nothing. The tables of cached results written in it are cached again however
        the level ends, or go on being held by the level above (``cache.level_ended``).
        ``leaving``: the caller's exception on its way out (see ``_end_transaction``).
        """
        levels = self._open.levels
        level = levels.pop() if levels else None  # none: begun on another thread; nothing to merge
        committed = False
        try:
            _end_transaction(self.adapter, commit, leaving)
            committed = commit
        finally:
            if level is not None and level.written:
                cache.level_ended(level.written, self._written_level() if committed else None)
        if committed and level is not None:
            self._merge(level)

    # ---- versioned (optimistic locking) helpers ----

    def _ensure_versioned_table(self, table: str) -> None:
        """Ensure the target table exists and has a ``_version`` column.

        This upgrades an existing non-versioned table by adding the column.

        Args:
            table: Table name.
        """
        self._ensure_table(table)
        layout, names = self._names(table)
        added: tuple[str, ...] = ()
        if not _has_version(layout):
            # the look again and the change in one transaction that holds the write lock from
            # its start, so no other thread, connection or process changes the table between them
            with self._ddl_lock, self:
                layout, names, added = self._run(_versioning(table))
        self._seen(table, layout, names, added)

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
        return self._upsert_versioned(validate_table_name(table), _id, doc, expected_version)

    def find_document_with_version(self, table: str, _id: int) -> Optional[dict[str, Any]]:
        """Fetch a document by id including ``_version`` in the result dict.

        Args:
            table: Table name.
            _id: Row id to fetch.

        Returns:
            dict | None: Decoded document with ``_id`` and ``_version`` keys, or None.
        """
        rows, columns = self._read_rows(table, [_id], versioned=True)
        return _row_documents(rows, columns)[0] if rows else None


class _OpenLevels(threading.local):
    """The open levels of a database object's own transactions on this thread."""

    def __init__(self):
        self.levels: list[_Level] = []


def _end_transaction(adapter, commit: bool, leaving: Optional[BaseException]) -> None:
    """End the adapter's innermost transaction.

    While the caller's exception ``leaving`` is on its way out, an error of the end is not
    raised (the adapter closed inside the block, say): that exception is the one that
    leaves, and when the end says the transaction was rolled back
    (``TransactionStateError``) it goes on with that sentence as a note.
    """
    try:
        adapter.end_transaction(commit=commit)
    except Exception as ended:
        _ended_while_leaving(ended, leaving)


def _ended_while_leaving(ended: Exception, leaving: Optional[BaseException]) -> None:
    """The end of a transaction raised ``ended``: raise it, or, while ``leaving`` is on its way
    out, name it on ``leaving`` as a note, once (a nested block it left already may have
    added it). A ``TransactionStateError`` is the sentence of a rolled-back transaction;
    any other error is named with its type and message."""
    if leaving is None:
        raise ended
    if isinstance(ended, TransactionStateError):
        if isinstance(leaving, TransactionStateError):
            return
        note = str(ended)
    else:
        note = f"ending the transaction failed: {type(ended).__name__}: {ended}"
    if note not in getattr(leaving, "__notes__", ()):
        leaving.add_note(note)


class Transaction:
    """Context manager for explicit database transactions.

    When inside a Transaction context, model.save() and other document
    operations will NOT auto-commit. The transaction commits only when
    the context exits successfully, or rolls back on exception.
    """

    def __init__(self, adapter, db: SQLerDB):
        self.adapter = adapter
        self._db = db
        self._active = False

    def _end(self, commit: bool, leaving: Optional[BaseException] = None) -> None:
        self._active = False
        self._db._end_level(commit, leaving=leaving)

    def __enter__(self):
        """Begin the transaction."""
        self._db._begin_level()
        self._active = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Commit on success, rollback on exception.

        When the block raised and the transaction was rolled back for a reason of its own
        (a nested end that failed), the block's exception goes on with that sentence as a note.
        """
        if self._active:
            self._end(exc_type is None, leaving=exc_val)
        return False

    def commit(self):
        """Explicitly commit the transaction."""
        if self._active:
            self._end(True)

    def rollback(self):
        """Explicitly rollback the transaction."""
        if self._active:
            self._end(False)
