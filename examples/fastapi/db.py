from __future__ import annotations

from typing import Optional

from sqler import SQLerDB

from .models import Address, Order, User

"""
DB bootstrap utilities for the FastAPI example.

English: Create/close a process-wide SQLerDB and bind models.
日本語: プロセス全体で共有する SQLerDB を生成/破棄し、モデルをバインドします。
"""

_db: Optional[SQLerDB] = None


def init_db(path: str | None = None):
    """Initialize the global DB (on-disk when path is set, otherwise in-memory).

    日本語: グローバル DB を初期化します（path 指定でオンディスク、未指定でインメモリ）。
    """
    global _db
    if _db is not None:
        return _db
    _db = SQLerDB.on_disk(path) if path else SQLerDB.in_memory(shared=False)

    User.set_db(_db)
    Address.set_db(_db)
    Order.set_db(_db)

    User.ensure_index("age")
    User.ensure_index("address._id")
    Address.ensure_index("city")
    Order.ensure_index("total")

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
        _db.close()
        _db = None
