from __future__ import annotations

from typing import Optional

from sqler import SQLerDB
from sqler.cache import configure_cache
from sqler.logging import query_logger
from sqler.metrics import metrics

from .models import (
    Article,
    City,
    Country,
    Writer,
)

"""
DB bootstrap utilities for the FastAPI example.

English: Create/close a process-wide SQLerDB and bind models.
日本語: プロセス全体で共有する SQLerDB を生成/破棄し、モデルをバインドします。
"""

_db: Optional[SQLerDB] = None


def init_db(path: str | None = None):
    """Initialize the global DB (on-disk by default for WAL support).

    日本語: グローバル DB を初期化します（デフォルトはWAL対応のためオンディスク）。
    """
    from pathlib import Path

    global _db
    if _db is not None:
        return _db
    # Default to on-disk db in the examples/fastapi folder for WAL support
    if path:
        db_path = path
    else:
        db_path = str(Path(__file__).parent / "sqler_demo.db")
    _db = SQLerDB.on_disk(db_path)

    # Register models
    Country.set_db(_db)
    City.set_db(_db)
    Writer.set_db(_db)
    Article.set_db(_db)

    # Indexes for efficient queries
    Country.ensure_index("code", unique=True)
    City.ensure_index("country._id")
    Writer.ensure_index("city._id")
    Article.ensure_index("writer._id")

    # Initialize FTS index for Article
    Article.create_search_index()

    # Configure global cache (100 entries, 5 min TTL)
    configure_cache(max_size=100, default_ttl_seconds=300)

    # Enable metrics collection (slow query threshold: 100ms)
    metrics.enable(slow_threshold_ms=100)

    # Enable query logging for debugging
    query_logger.enable()

    return _db


def get_db() -> SQLerDB:
    """Return the initialized DB or raise if not yet started.

    日本語: 初期化済み DB を返します（未初期化なら例外）。
    """
    if _db is None:
        raise RuntimeError("DB not initialized. Did you forget to start the app with lifespan?")
    return _db


def close_db() -> None:
    """Close and clear the global DB.

    日本語: グローバル DB をクローズして解放します。
    """
    global _db
    if _db is not None:
        # Disable metrics and logging
        metrics.disable()
        query_logger.disable()

        _db.close()
        _db = None
