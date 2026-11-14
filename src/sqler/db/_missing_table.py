"""A read of a table nobody created gives what an empty table gives, and creates nothing.

``SQLerDB.query`` and ``AsyncSQLerDB.query`` return a query bound to the adapter. The query
code runs its statements on that adapter, and SQLite says "no such table" for a table that
does not exist. Bound to :class:`MissingAsEmpty` (or :class:`AsyncMissingAsEmpty`) instead,
the same statement of a missing table runs once against a scratch, empty copy of it, so
``all()`` is empty, ``count()`` is 0 and ``update()`` changes no row, as on an empty table,
and the database is not touched.
"""

import sqlite3
from typing import Any, Iterable, Mapping, Optional

# the failing statement's own words, as SQLite words them
_MISSING = "no such table: "


def is_missing_table(exc: BaseException, table: str) -> bool:
    """True when ``exc`` is SQLite's "no such table" for ``table`` itself.

    "no such table" for any other table (a join, a subquery) and every other error are not this.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    return str(exc) in (f"{_MISSING}{table}", f"{_MISSING}main.{table}")


class EmptyCursor:
    """The finished cursor of a statement run on an empty table: its rows are already read."""

    def __init__(self, rows: list, description: Any, rowcount: int, lastrowid: Optional[int]):
        self._rows = list(rows)
        self.description = description
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.arraysize = 1

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list:
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, size: Optional[int] = None) -> list:
        n = self.arraysize if size is None else size
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows

    def close(self) -> None:
        self._rows = []

    def __iter__(self):
        while self._rows:
            yield self._rows.pop(0)


class AsyncEmptyCursor(EmptyCursor):
    """:class:`EmptyCursor` with the awaitable methods of an ``aiosqlite`` cursor."""

    async def fetchone(self):  # type: ignore[override]
        return EmptyCursor.fetchone(self)

    async def fetchall(self) -> list:  # type: ignore[override]
        return EmptyCursor.fetchall(self)

    async def fetchmany(self, size: Optional[int] = None) -> list:  # type: ignore[override]
        return EmptyCursor.fetchmany(self, size)

    async def close(self) -> None:  # type: ignore[override]
        EmptyCursor.close(self)

    async def __aiter__(self):  # type: ignore[override]
        while self._rows:
            yield self._rows.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        EmptyCursor.close(self)


def _run_on_empty_table(
    table: str,
    promoted: Iterable[str],
    query: str,
    params: Any,
    *,
    row_factory: Any,
    cursor_cls: type,
):
    """Run ``query`` on a private in-memory copy of ``table`` with no rows."""
    columns = ["_id INTEGER PRIMARY KEY", "data JSON", "_version INTEGER NOT NULL DEFAULT 0", *promoted]
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.row_factory = row_factory
        scratch.execute(f'CREATE TABLE "{table}" ({", ".join(columns)})')
        cur = scratch.execute(query, tuple(params) if params else ())
        rows = cur.fetchall()
        return cursor_cls(rows, cur.description, cur.rowcount, None)
    finally:
        scratch.close()


class MissingAsEmpty:
    """The adapter a ``SQLerDB.query`` is bound to: everything is the adapter's, but a statement of
    a missing ``table`` runs on an empty copy of it. Nothing is created in the database."""

    def __init__(self, adapter: Any, table: str, promoted: Mapping[str, list]):
        self._adapter = adapter
        self._table = table
        self._promoted = promoted

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def execute(self, query: str, params: Optional[list] = None):
        try:
            return self._adapter.execute(query, params)
        except sqlite3.OperationalError as exc:
            if not is_missing_table(exc, self._table):
                raise
        return _run_on_empty_table(
            self._table,
            self._promoted.get(self._table, []),
            query,
            params,
            row_factory=sqlite3.Row,
            cursor_cls=EmptyCursor,
        )


class AsyncMissingAsEmpty(MissingAsEmpty):
    """:class:`MissingAsEmpty` for the async adapter."""

    async def execute(self, query: str, params: Optional[list] = None):  # type: ignore[override]
        try:
            return await self._adapter.execute(query, params)
        except sqlite3.OperationalError as exc:
            if not is_missing_table(exc, self._table):
                raise
        return _run_on_empty_table(
            self._table,
            self._promoted.get(self._table, []),
            query,
            params,
            row_factory=None,
            cursor_cls=AsyncEmptyCursor,
        )
