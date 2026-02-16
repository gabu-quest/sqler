"""Database operations router.

Demonstrates: health_check, get_stats, vacuum, checkpoint, schema
日本語: ヘルスチェック、統計取得、vacuum、チェックポイント、スキーマ
"""

from typing import Any

from sqler.ops import checkpoint, get_stats, health_check, vacuum

from fastapi import APIRouter

from ..db import get_db, seed_db
from ..utils import db_call

router = APIRouter(prefix="/api/db", tags=["Database Operations"])


@router.get("/health")
async def db_health():
    """Get database health status.

    日本語: データベースのヘルスステータスを取得。
    """
    status = await db_call(lambda: health_check(get_db()))
    journal_mode = status.details.get("journal_mode", "unknown")
    return {
        "healthy": status.healthy,
        "latency_ms": status.latency_ms,
        "message": status.message,
        "timestamp": status.timestamp.isoformat(),
        "journal_mode": journal_mode,
        "wal_mode": journal_mode.lower() == "wal",
    }


@router.get("/stats")
async def db_stats():
    """Get database statistics.

    日本語: データベースの統計情報を取得。
    """
    stats = await db_call(lambda: get_stats(get_db()))
    return {
        "path": stats.path,
        "size_bytes": stats.size_bytes,
        "page_count": stats.page_count,
        "page_size": stats.page_size,
        "wal_size_bytes": stats.wal_size_bytes,
        "table_count": stats.table_count,
        "index_count": stats.index_count,
        "freelist_count": stats.freelist_count,
        "timestamp": stats.timestamp.isoformat(),
    }


@router.post("/vacuum")
async def db_vacuum():
    """Run VACUUM to reclaim disk space.

    日本語: VACUUMを実行してディスク領域を回収。
    """
    duration_ms = await db_call(lambda: vacuum(get_db()))
    return {"success": True, "duration_ms": duration_ms}


@router.post("/checkpoint")
async def db_checkpoint():
    """Run WAL checkpoint.

    日本語: WALチェックポイントを実行。
    """
    result = await db_call(lambda: checkpoint(get_db()))
    return {"success": True, "result": result}


def _get_schema() -> dict[str, Any]:
    """Query sqlite_master and pragmas to build schema information.

    日本語: sqlite_masterとpragmaをクエリしてスキーマ情報を構築。
    """
    db = get_db()

    # Get all tables (excluding sqlite internal tables)
    tables_result = db.execute_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    table_names = [row["name"] for row in tables_result]

    # Categorize tables and get column info
    tables = []
    for table_name in table_names:
        # Determine category based on naming convention
        if table_name.endswith("_audit"):
            category = "audit"
        elif "_fts" in table_name:
            category = "fts"
        elif table_name.startswith("sqler_"):
            category = "system"
        else:
            category = "data"

        # Get column info using adapter.execute for PRAGMA
        columns = []
        pragma_result = db.adapter.execute(f"PRAGMA table_info({table_name})")
        for col in pragma_result:
            # col: (cid, name, type, notnull, default, pk)
            columns.append({
                "name": col[1],
                "type": col[2] or "BLOB",
                "pk": bool(col[5]),
                "nullable": not bool(col[3]),
            })

        # Get row count (skip for FTS internal tables)
        row_count = 0
        if not table_name.endswith(("_content", "_data", "_docsize", "_idx", "_config")):
            try:
                count_result = db.execute_sql(f"SELECT COUNT(*) as cnt FROM {table_name}")
                row_count = count_result[0]["cnt"] if count_result else 0
            except Exception:
                row_count = 0

        tables.append({
            "name": table_name,
            "category": category,
            "columns": columns,
            "row_count": row_count,
        })

    # Get indices - include expression-based indices
    indices_result = db.execute_sql(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY tbl_name, name"
    )
    indices = []
    for row in indices_result:
        index_name, table_name, sql = row["name"], row["tbl_name"], row.get("sql", "")
        if not sql:
            continue

        # Extract column info from SQL for expression-based indices
        # Pattern: json_extract(data, '$.field') or json_extract(data, '$.ref._id')
        import re
        json_pattern = r"json_extract\s*\(\s*data\s*,\s*'\$\.([^']+)'\s*\)"
        matches = re.findall(json_pattern, sql)
        columns = matches if matches else []

        # Also try PRAGMA for regular indices
        if not columns:
            index_info = list(db.adapter.execute(f"PRAGMA index_info({index_name})"))
            columns = [col[2] for col in index_info if col[2]]

        if columns:
            indices.append({
                "name": index_name,
                "table": table_name,
                "columns": columns,
            })

    # Build relationships from foreign keys
    relationships = []
    for table_name in table_names:
        fk_result = list(db.adapter.execute(f"PRAGMA foreign_key_list({table_name})"))
        for fk in fk_result:
            # fk: (id, seq, table, from, to, on_update, on_delete, match)
            relationships.append({
                "from_table": table_name,
                "from_column": fk[3],
                "to_table": fk[2],
                "to_column": fk[4],
            })

    # If no explicit foreign keys, infer from SQLer RefField patterns
    # SQLer stores references as nested objects with _id, so look at index columns
    if not relationships:
        data_tables = [t["name"] for t in tables if t["category"] == "data"]
        for idx in indices:
            table_name = idx["table"]
            for col in idx["columns"]:
                # Pattern: ref._id where ref is the reference table name (singular or plural)
                if "._id" in col:
                    ref_name = col.replace("._id", "")
                    # Find matching table (try both singular and plural forms)
                    ref_table = None
                    for dt in data_tables:
                        if dt == ref_name or dt == ref_name + "s" or dt == ref_name + "ies":
                            ref_table = dt
                            break
                        # Handle city -> cities
                        if ref_name.endswith("y") and dt == ref_name[:-1] + "ies":
                            ref_table = dt
                            break
                    if ref_table and table_name != ref_table:
                        relationships.append({
                            "from_table": table_name,
                            "from_column": col,
                            "to_table": ref_table,
                            "to_column": "_id",
                        })

    return {
        "tables": tables,
        "indices": indices,
        "relationships": relationships,
    }


@router.get("/schema")
async def db_schema():
    """Get database schema information.

    Returns tables, indices, and relationships for visualization.

    日本語: データベーススキーマ情報を取得。テーブル、インデックス、関係を可視化用に返す。
    """
    return await db_call(_get_schema)


@router.post("/seed")
async def db_seed():
    """Reset and seed the database with sample data.

    WARNING: This deletes all existing data!

    日本語: データベースをリセットしてサンプルデータでシード。警告：既存データは全て削除されます！
    """
    result = await db_call(seed_db)
    return {
        "success": True,
        "message": "Database seeded successfully",
        "counts": result,
    }
