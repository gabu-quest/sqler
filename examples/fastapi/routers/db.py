"""Database operations router.

Demonstrates: health_check, get_stats, vacuum, checkpoint
日本語: ヘルスチェック、統計取得、vacuum、チェックポイント
"""

from sqler.ops import checkpoint, get_stats, health_check, vacuum

from fastapi import APIRouter

from ..db import get_db
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
