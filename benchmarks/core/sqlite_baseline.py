"""Raw sqlite3 baseline operations matching sqler's internal SQL patterns.

Provides apples-to-apples comparison: same DDL, same JSON storage, no ORM overhead.
"""

from __future__ import annotations

import json
import sqlite3


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


def query_filter(
    conn: sqlite3.Connection, table: str, field: str, op: str, value: object
) -> list[dict]:
    """SELECT data WHERE json_extract(data, '$.field') op ?"""
    sql = (
        f"SELECT data FROM [{table}] "
        f"WHERE json_extract(data, '$.{field}') {op} ?"
    )
    rows = conn.execute(sql, (value,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def query_range(
    conn: sqlite3.Connection, table: str, field: str, lo: object, hi: object
) -> list[dict]:
    """Range filter on a JSON field."""
    sql = (
        f"SELECT data FROM [{table}] "
        f"WHERE json_extract(data, '$.{field}') > ? "
        f"AND json_extract(data, '$.{field}') < ?"
    )
    rows = conn.execute(sql, (lo, hi)).fetchall()
    return [json.loads(r[0]) for r in rows]


def query_top_n(
    conn: sqlite3.Connection, table: str, field: str, n: int
) -> list[dict]:
    """ORDER BY json_extract + LIMIT."""
    sql = (
        f"SELECT data FROM [{table}] "
        f"ORDER BY json_extract(data, '$.{field}') LIMIT ?"
    )
    rows = conn.execute(sql, (n,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def query_paginate(
    conn: sqlite3.Connection, table: str, field: str, page: int, per_page: int
) -> list[dict]:
    """ORDER BY + LIMIT + OFFSET pagination."""
    offset = (page - 1) * per_page
    sql = (
        f"SELECT data FROM [{table}] "
        f"ORDER BY json_extract(data, '$.{field}') LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, (per_page, offset)).fetchall()
    return [json.loads(r[0]) for r in rows]


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


def any_where_eq(
    conn: sqlite3.Connection,
    table: str,
    array_field: str,
    item_field: str,
    value: object,
) -> list[dict]:
    """EXISTS + json_each subquery for array-of-objects equality."""
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(json_extract(data, '$.{array_field}')) AS e "
        f"WHERE json_extract(e.value, '$.{item_field}') = ?)"
    )
    rows = conn.execute(sql, (value,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def any_where_gt(
    conn: sqlite3.Connection,
    table: str,
    array_field: str,
    item_field: str,
    value: object,
) -> list[dict]:
    """EXISTS + json_each subquery for array-of-objects comparison."""
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(json_extract(data, '$.{array_field}')) AS e "
        f"WHERE json_extract(e.value, '$.{item_field}') > ?)"
    )
    rows = conn.execute(sql, (value,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def array_contains(
    conn: sqlite3.Connection, table: str, array_field: str, value: object
) -> list[dict]:
    """EXISTS + json_each for scalar array membership."""
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(json_extract(data, '$.{array_field}')) "
        f"WHERE value = ?)"
    )
    rows = conn.execute(sql, (value,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def array_isin(
    conn: sqlite3.Connection, table: str, array_field: str, values: list
) -> list[dict]:
    """EXISTS + json_each + IN(...) for multi-value membership."""
    placeholders = ",".join("?" for _ in values)
    sql = (
        f"SELECT data FROM [{table}] WHERE EXISTS("
        f"SELECT 1 FROM json_each(json_extract(data, '$.{array_field}')) "
        f"WHERE value IN ({placeholders}))"
    )
    rows = conn.execute(sql, values).fetchall()
    return [json.loads(r[0]) for r in rows]


def query_nested(
    conn: sqlite3.Connection, table: str, json_path: str, op: str, value: object
) -> list[dict]:
    """Query nested JSON path like '$.level_0.level_1.target'."""
    sql = (
        f"SELECT data FROM [{table}] "
        f"WHERE json_extract(data, '{json_path}') {op} ?"
    )
    rows = conn.execute(sql, (value,)).fetchall()
    return [json.loads(r[0]) for r in rows]


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
        """Rebuild the FTS index."""
        self.conn.execute(
            f"INSERT INTO [{self.fts_table}]([{self.fts_table}]) VALUES('rebuild')"
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
