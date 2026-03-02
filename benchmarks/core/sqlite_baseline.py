"""Raw sqlite3 baseline operations matching sqler's internal SQL patterns.

v1.2: Matched PRAGMAs, matched SQL, matched serialization.
Provides apples-to-apples comparison: same DDL, same JSON storage, same config, no ORM overhead.
"""

from __future__ import annotations

import json
import sqlite3


# ---------------------------------------------------------------------------
# PRAGMA helpers — exact copies of sqler's adapter PRAGMAs
# ---------------------------------------------------------------------------

def apply_in_memory_pragmas(conn: sqlite3.Connection) -> None:
    """Apply sqler's in-memory PRAGMAs to a raw sqlite3 connection.

    Mirrors sqler.adapter.synchronous.SyncAdapter.in_memory():
      - foreign_keys ON
      - synchronous OFF      (no fsync needed for :memory:)
      - journal_mode MEMORY  (no WAL for :memory:)
      - temp_store MEMORY
      - cache_size -32000    (32 MB)
      - locking_mode EXCLUSIVE
    """
    for pragma in [
        "PRAGMA foreign_keys = ON",
        "PRAGMA synchronous = OFF",
        "PRAGMA journal_mode = MEMORY",
        "PRAGMA temp_store = MEMORY",
        "PRAGMA cache_size = -32000",
        "PRAGMA locking_mode = EXCLUSIVE",
    ]:
        conn.execute(pragma)
    conn.commit()


def apply_on_disk_pragmas(conn: sqlite3.Connection) -> None:
    """Apply sqler's on-disk PRAGMAs to a raw sqlite3 connection.

    Mirrors sqler.adapter.synchronous.SyncAdapter.on_disk():
      - foreign_keys ON
      - busy_timeout 5000    (5 s lock wait)
      - journal_mode WAL
      - synchronous NORMAL   (safe with WAL)
      - cache_size -64000    (64 MB)
      - wal_autocheckpoint 1000
      - mmap_size 256 MB
      - temp_store MEMORY
    """
    for pragma in [
        "PRAGMA foreign_keys = ON",
        "PRAGMA busy_timeout = 5000",
        "PRAGMA journal_mode = WAL",
        "PRAGMA synchronous = NORMAL",
        "PRAGMA cache_size = -64000",
        "PRAGMA wal_autocheckpoint = 1000",
        "PRAGMA mmap_size = 268435456",
        "PRAGMA temp_store = MEMORY",
    ]:
        conn.execute(pragma)
    conn.commit()


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def create_conn(path: str = ":memory:", storage: str = "memory") -> sqlite3.Connection:
    """Create a baseline connection with matched PRAGMAs and row_factory.

    Args:
        path: Database path (":memory:" for in-memory, file path for on-disk).
        storage: "memory" or "disk" — selects which PRAGMA set to apply.

    Returns:
        Configured sqlite3.Connection matching sqler's configuration.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if storage == "memory":
        apply_in_memory_pragmas(conn)
    else:
        apply_on_disk_pragmas(conn)
    return conn


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def fetch_as_strings(cursor) -> list[str]:
    """Fetch all rows as raw JSON strings, matching sqler's .all() behavior.

    sqler's db.query().all() returns list[str] — the raw JSON from the 'data'
    column WITHOUT calling json.loads(). This function does the same for
    baseline connections, ensuring both arms pay the same extraction cost.
    """
    return [row["data"] for row in cursor.fetchall()]


def fetch_as_dicts(cursor) -> list[dict]:
    """Fetch all rows and deserialize JSON into dicts.

    Use this when comparing against sqler paths that DO deserialize
    (e.g., Model.query().all() which returns model instances).
    """
    return [json.loads(row["data"]) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# DDL + DML
# ---------------------------------------------------------------------------

def create_table(conn: sqlite3.Connection, table: str) -> None:
    """Create table with same schema sqler uses internally."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS [{table}] ("
        f"_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        f"data JSON NOT NULL)"
    )
    conn.commit()


def insert_many(conn: sqlite3.Connection, table: str, docs: list[dict]) -> None:
    """Batch insert via executemany — best-case raw performance."""
    conn.executemany(
        f"INSERT INTO [{table}] (data) VALUES (json(?))",
        [(json.dumps(d),) for d in docs],
    )
    conn.commit()


def insert_loop(conn: sqlite3.Connection, table: str, docs: list[dict]) -> None:
    """One-at-a-time INSERT loop — matches sqler's single-save pattern."""
    for d in docs:
        conn.execute(
            f"INSERT INTO [{table}] (data) VALUES (json(?))",
            (json.dumps(d),),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Query operations
# ---------------------------------------------------------------------------

def query_filter(
    conn: sqlite3.Connection, table: str, field: str, op: str, value: object
) -> list[str]:
    """SELECT data WHERE json_extract(data, '$.field') op ?"""
    sql = (
        f"SELECT data FROM [{table}] "
        f"WHERE json_extract(data, '$.{field}') {op} ?"
    )
    return fetch_as_strings(conn.execute(sql, (value,)))


def query_range(
    conn: sqlite3.Connection, table: str, field: str, lo: object, hi: object
) -> list[str]:
    """Range filter on a JSON field."""
    sql = (
        f"SELECT data FROM [{table}] "
        f"WHERE json_extract(data, '$.{field}') > ? "
        f"AND json_extract(data, '$.{field}') < ?"
    )
    return fetch_as_strings(conn.execute(sql, (lo, hi)))


def query_top_n(
    conn: sqlite3.Connection, table: str, field: str, n: int
) -> list[str]:
    """ORDER BY json_extract + LIMIT."""
    sql = (
        f"SELECT data FROM [{table}] "
        f"ORDER BY json_extract(data, '$.{field}') LIMIT ?"
    )
    return fetch_as_strings(conn.execute(sql, (n,)))


def query_paginate(
    conn: sqlite3.Connection, table: str, field: str, page: int, per_page: int
) -> list[str]:
    """ORDER BY + LIMIT + OFFSET pagination."""
    offset = (page - 1) * per_page
    sql = (
        f"SELECT data FROM [{table}] "
        f"ORDER BY json_extract(data, '$.{field}') LIMIT ? OFFSET ?"
    )
    return fetch_as_strings(conn.execute(sql, (per_page, offset)))


def count_filter(
    conn: sqlite3.Connection, table: str, field: str, op: str, value: object
) -> int:
    """SELECT COUNT(*) with json_extract filter."""
    sql = (
        f"SELECT COUNT(*) FROM [{table}] "
        f"WHERE json_extract(data, '$.{field}') {op} ?"
    )
    return conn.execute(sql, (value,)).fetchone()[0]


def exists_filter(
    conn: sqlite3.Connection, table: str, field: str, op: str, value: object
) -> bool:
    """SELECT EXISTS(...) with json_extract filter."""
    sql = (
        f"SELECT EXISTS(SELECT 1 FROM [{table}] "
        f"WHERE json_extract(data, '$.{field}') {op} ?)"
    )
    return bool(conn.execute(sql, (value,)).fetchone()[0])


def aggregate(
    conn: sqlite3.Connection, table: str, agg_fn: str, field: str
) -> float:
    """SELECT AGG(json_extract(data, '$.field'))."""
    sql = f"SELECT {agg_fn}(json_extract(data, '$.{field}')) FROM [{table}]"
    result = conn.execute(sql).fetchone()[0]
    return float(result) if result is not None else 0.0


def create_index(conn: sqlite3.Connection, table: str, field: str) -> None:
    """CREATE INDEX on json_extract expression."""
    idx_name = f"idx_{table}_{field}"
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS [{idx_name}] ON [{table}] "
        f"(json_extract(data, '$.{field}'))"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Array operations — v1.2: fixed SQL to use json_each(data, '$.path')
#
# v1.1 BUG (H-2): used json_each(json_extract(data, '$.field'))
# which extracts the full array to a string, then re-parses it.
# sqler generates json_each(data, '$.field') which is a direct path
# traversal — faster and correct. Now both arms use the same SQL.
# ---------------------------------------------------------------------------

def any_where_eq(
    conn: sqlite3.Connection,
    table: str,
    array_field: str,
    item_field: str,
    value: object,
) -> list[str]:
    """EXISTS + json_each subquery for array-of-objects equality."""
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(data, '$.{array_field}') AS e "
        f"WHERE json_extract(e.value, '$.{item_field}') = ?)"
    )
    return fetch_as_strings(conn.execute(sql, (value,)))


def any_where_gt(
    conn: sqlite3.Connection,
    table: str,
    array_field: str,
    item_field: str,
    value: object,
) -> list[str]:
    """EXISTS + json_each subquery for array-of-objects comparison."""
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(data, '$.{array_field}') AS e "
        f"WHERE json_extract(e.value, '$.{item_field}') > ?)"
    )
    return fetch_as_strings(conn.execute(sql, (value,)))


def array_contains(
    conn: sqlite3.Connection, table: str, array_field: str, value: object
) -> list[str]:
    """EXISTS + json_each for scalar array membership.

    v1.2: uses json_each(data, '$.field') — direct path, matches sqler's SQL.
    v1.1 bug: used json_each(json_extract(data, '$.field')) — extract+reparse.
    """
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(data, '$.{array_field}') "
        f"WHERE value = ?)"
    )
    return fetch_as_strings(conn.execute(sql, (value,)))


def array_isin(
    conn: sqlite3.Connection, table: str, array_field: str, values: list
) -> list[str]:
    """EXISTS + json_each + IN(...) for multi-value membership.

    v1.2: uses json_each(data, '$.field') — direct path, matches sqler's SQL.
    """
    placeholders = ",".join("?" for _ in values)
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(data, '$.{array_field}') "
        f"WHERE value IN ({placeholders}))"
    )
    return fetch_as_strings(conn.execute(sql, values))


def query_nested(
    conn: sqlite3.Connection, table: str, json_path: str, op: str, value: object
) -> list[str]:
    """Query nested JSON path like '$.level_0.level_1.target'."""
    sql = (
        f"SELECT data FROM [{table}] "
        f"WHERE json_extract(data, '{json_path}') {op} ?"
    )
    return fetch_as_strings(conn.execute(sql, (value,)))


# ---------------------------------------------------------------------------
# Complex filter queries (for S7 ComplexFilterChains)
# ---------------------------------------------------------------------------

def query_complex_filter(conn: sqlite3.Connection, table: str,
                         sql_where: str, params: tuple) -> list[str]:
    """Execute a complex filter query and return raw JSON strings.

    Matches sqler's db.query().all() which returns list[str].
    """
    sql = f"SELECT data FROM [{table}] WHERE {sql_where}"
    return fetch_as_strings(conn.execute(sql, params))


# ---------------------------------------------------------------------------
# FTS5 baseline
# ---------------------------------------------------------------------------

class SQLiteFTSBaseline:
    """Raw FTS5 operations for baseline comparison.

    Uses a standalone FTS5 table (no content= sync) because the source table
    stores data as JSON blobs, not separate columns.
    """

    def __init__(self, conn: sqlite3.Connection, table: str, fields: list[str]):
        self.conn = conn
        self.table = table
        self.fts_table = f"{table}_fts"
        self.fields = fields

    def create(self) -> None:
        """Create standalone FTS5 virtual table and populate from JSON data."""
        cols = ", ".join(self.fields)
        self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS [{self.fts_table}] "
            f"USING fts5({cols})"
        )
        # Populate from JSON source table
        extract_cols = ", ".join(
            f"json_extract(data, '$.{f}')" for f in self.fields
        )
        self.conn.execute(
            f"INSERT INTO [{self.fts_table}] ({cols}) "
            f"SELECT {extract_cols} FROM [{self.table}]"
        )
        self.conn.commit()

    def rebuild(self) -> None:
        """Rebuild FTS index from source data (matched to sqler's rebuild).

        Deletes all FTS rows and repopulates via INSERT...SELECT from the
        source JSON table — the same work sqler's ``FTSIndex.rebuild()``
        does.  The previous implementation used FTS5's internal
        ``VALUES('rebuild')`` command which only merges index segments and
        does NOT re-read source data, creating an apples-to-oranges
        comparison.
        """
        self.conn.execute(f"DELETE FROM [{self.fts_table}]")
        extract_cols = ", ".join(
            f"json_extract(data, '$.{f}')" for f in self.fields
        )
        cols = ", ".join(self.fields)
        self.conn.execute(
            f"INSERT INTO [{self.fts_table}](rowid, {cols}) "
            f"SELECT _id, {extract_cols} FROM [{self.table}]"
        )
        self.conn.commit()

    def search(self, query: str, limit: int = 20) -> list[tuple]:
        """Basic FTS5 search."""
        sql = (
            f"SELECT rowid, * FROM [{self.fts_table}] "
            f"WHERE [{self.fts_table}] MATCH ? LIMIT ?"
        )
        return self.conn.execute(sql, (query, limit)).fetchall()

    def search_ranked(self, query: str, limit: int = 20) -> list[tuple]:
        """FTS5 search with BM25 ranking."""
        sql = (
            f"SELECT rowid, *, rank FROM [{self.fts_table}] "
            f"WHERE [{self.fts_table}] MATCH ? ORDER BY rank LIMIT ?"
        )
        return self.conn.execute(sql, (query, limit)).fetchall()

    def search_with_highlights(self, query: str, limit: int = 20) -> list[tuple]:
        """FTS5 search with highlighted snippets — matches sqler's search_with_highlights().

        v1.2: Added to give baseline parity with sqler's FTS highlight feature.
        v1.1: Missing — sqler highlights had no baseline comparison (M-9).
        """
        highlight_exprs = ", ".join(
            f"highlight([{self.fts_table}], {i}, '<b>', '</b>')"
            for i in range(len(self.fields))
        )
        sql = (
            f"SELECT rowid, {highlight_exprs} FROM [{self.fts_table}] "
            f"WHERE [{self.fts_table}] MATCH ? LIMIT ?"
        )
        return self.conn.execute(sql, (query, limit)).fetchall()
